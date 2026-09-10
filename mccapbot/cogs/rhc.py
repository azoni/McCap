"""/rh: Robinhood Chain wallets, DEX trading, and the chain's trending board.

Trading (buy/sell) needs the trading flag and the allowlist; getting your own
funds OUT (export, withdraw) needs only your wallet, so a kill switch never
strands anyone. Money moves only after a confirm button bound to the caller.

Visibility: the confirm prompts, refusals, the withdraw flow and the key export
are always visible only to the caller. Trade results, the trending board and
the group stats post to the channel (``RHC_PUBLIC_REPLIES=0`` makes them
private too; ``private:True`` does it for one call). Holdings, history and
profit are yours by default; ``public:True`` shows them to the channel. See
``mccapbot/rhc`` for the execution path and its guards.
"""

import asyncio
import io
import os
import time
from typing import Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from .. import rhchain, storage, views
from ..config import (
    FEED_ENABLE,
    RHCHAIN_CACHE_SECONDS,
    RHC_AUTO_BUY_TTL,
    RHC_AUTO_MAX_PER_USER,
    RHC_AUTO_MAX_TOTAL,
    RHC_AUTO_MAX_TTL,
    RHC_AUTO_SELL_TTL,
    RHC_CONFIRM_TIMEOUT,
    RHC_DEFAULT_SLIPPAGE_BPS,
    RHC_GUILD_IDS,
    RHC_MAX_DAILY_USD,
    RHC_MAX_SLIPPAGE_BPS,
    RHC_MAX_TRADE_USD,
    RHC_PUBLIC_REPLIES,
    RHC_TRADER_IDS,
    RHC_TRADING_ENABLE,
)
from ..dex import token_summary
from ..helpers import (
    NEUTRAL,
    SEP,
    UNKNOWN,
    age,
    colour_for,
    eth_str,
    fit_lines,
    footer,
    is_manager,
    mult,
    pct,
    plural,
    qty,
    short_ca,
    usd as usd_str,   # ``usd`` is also the buy command's dollar option
    when,
)
from ..logging_setup import log
from ..models import AutoOrder
from ..rhc import chain, kyber, ledger, pnl, portfolio, swap, trade, tutorial, wallets
from ..tables import add_table_fields
from ..views import ConfirmOrder

THIN_POOL_USD = trade.THIN_POOL_USD
# One wording, shown at wallet creation and under the tutorial's safety topic.
CUSTODY_WARNING = tutorial.CUSTODY_WARNING

# Visibility of trade results, balances, the trending board and group stats.
# Prompts, refusals, the withdraw flow and the key export never use this: they
# are always private.
PRIVATE = not RHC_PUBLIC_REPLIES

# Which GeckoTerminal trending duration backs each short board window.
TRENDING_DURATIONS = {"m5": "5m", "m15": "1h", "m30": "1h", "h1": "1h"}

# The tutorial's chapters; mccapbot/rhc/tutorial.py renders them.
TUTORIAL_TOPICS = tutorial.TOPICS

# Shown once, privately, after someone's first buy lands.
FIRST_BUY_TIP = (
    f"💡 First buy{SEP}the buttons under the receipt sell part or all of it (each still asks you to confirm)"
    f"{SEP}**TP / SL** arms a take-profit and stop-loss that fire once without asking again"
    f"{SEP}`/rh holdings` any time{SEP}`/rh tutorial` for the rest"
)


def _gate() -> Optional[str]:
    """Why trading is unavailable, or None if it is armed."""
    if not RHC_TRADING_ENABLE:
        return "Robinhood Chain trading is disabled (`RHC_TRADING_ENABLE=0`)."
    if not RHC_TRADER_IDS:
        return "Nobody is on the trader allowlist (`RHC_TRADER_IDS`). An unset list never means everyone."
    vault = _vault_problem()
    if vault:
        return vault
    return None


def _vault_problem() -> Optional[str]:
    if not wallets.loaded():
        return "The wallet vault did not load. An operator has to look at the logs before wallets can be used."
    if not wallets.vault_ready():
        return "The wallet vault has no `RHC_WALLET_SECRET`, so wallets cannot be created or opened."
    if not wallets.unlockable():
        return "The wallet vault cannot be opened with the current secret. An operator has to fix this."
    return None


def allowed(user_id: int) -> bool:
    return user_id in RHC_TRADER_IDS


def guild_ok(guild_id: Optional[int]) -> bool:
    """With a server allowlist set, trading happens only there, DMs included."""
    if not RHC_GUILD_IDS:
        return True
    return guild_id is not None and guild_id in RHC_GUILD_IDS


def clamp_slippage(bps: Optional[int]) -> int:
    v = int(bps) if bps is not None else RHC_DEFAULT_SLIPPAGE_BPS
    return max(10, min(v, RHC_MAX_SLIPPAGE_BPS))


# The money path lives in rhc/trade.py; these names stay for callers and tests.
_eth = trade.eth
_entry_mc = trade.entry_mc


def deny_reason(user_id: int, guild_id: Optional[int]) -> Optional[Tuple[str, str]]:
    """Why this person may not trade here: (kind, text) or None. Allowlist first,
    so a stranger learns nothing about the vault's state. Kinds: ``allowlist``,
    ``guild``, ``gate`` (kill switch or vault; may pass later)."""
    if not allowed(user_id):
        return "allowlist", "You are not on the trader allowlist."
    if not guild_ok(guild_id):
        return "guild", "Trading commands are not enabled here."
    reason = _gate()
    if reason:
        return "gate", reason
    return None


class RhcCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await wallets.load()
        swap.restore_pending()
        # Buttons under receipts, boards and alerts keep working across restarts:
        # their state lives in the custom_id, and the classes are registered once.
        items = getattr(views, "DYNAMIC_ITEMS", ())
        if self.bot is not None and items:
            self.bot.add_dynamic_items(*items)
        log.info(
            "Robinhood Chain wallet vault: %d wallet(s), DATA_DIR %s is%s a mount point (mount check %s)",
            wallets.count(), wallets.DATA_DIR, "" if os.path.ismount(str(wallets.DATA_DIR)) else " NOT",
            "required" if wallets.RHC_REQUIRE_MOUNTED_DATA_DIR else "not required",
        )
        if RHC_TRADING_ENABLE:
            reason = _gate()
            if reason:
                log.warning("Robinhood Chain trading flag is on but blocked: %s", reason)
            else:
                log.info("Robinhood Chain trading armed for %d allowlisted user(s)", len(RHC_TRADER_IDS))

    async def cog_unload(self):
        items = getattr(views, "DYNAMIC_ITEMS", ())
        if self.bot is not None and items:
            try:
                self.bot.remove_dynamic_items(*items)
            except Exception:
                log.debug("Could not remove dynamic items", exc_info=True)

    # ---------------- surface for the buttons and the auto-order engine ----------------
    # views.py and rhc/orders.py never import this module (discord.py purges an
    # unloaded extension from sys.modules), so they reach these through the cog.

    @staticmethod
    def deny(user_id: int, guild_id: Optional[int]) -> Optional[Tuple[str, str]]:
        return deny_reason(user_id, guild_id)

    @staticmethod
    def gate_reason() -> Optional[str]:
        return _gate()

    @staticmethod
    def default_slippage() -> int:
        return clamp_slippage(None)

    @property
    def results_private(self) -> bool:
        return PRIVATE

    @staticmethod
    def is_allowed(user_id: int) -> bool:
        return allowed(user_id)

    @staticmethod
    def _view(name: str, *args):
        """A button row from views.py, or None when it cannot be built: a view
        bug must never take a receipt or an alert down with it."""
        maker = getattr(views, name, None)
        if maker is None:
            return None
        try:
            return maker(*args)
        except Exception:
            log.exception("Could not build view %s", name)
            return None

    # ---------------- gates ----------------

    @staticmethod
    async def _send_private(inter: discord.Interaction, text: str, view: Optional[discord.ui.View] = None) -> None:
        """An always-private message, whether or not the interaction was deferred yet."""
        kw = {"ephemeral": True}
        if view is not None:
            kw["view"] = view
        if inter.response.is_done():
            await inter.followup.send(text, **kw)
        else:
            await inter.response.send_message(text, **kw)

    async def _deny_trade(self, inter: discord.Interaction) -> bool:
        """Reply and return True when the caller may not trade here."""
        why = deny_reason(inter.user.id, inter.guild_id)
        if why is None:
            return False
        kind, text = why
        if kind == "allowlist":
            log.warning("Rejected /rh trade command from non-allowlisted user %s", inter.user.id)
            text += f"{SEP}`/rh tutorial` explains how trading works"
        await self._send_private(inter, f"🔒 {text}")
        return True

    async def _no_wallet(self, inter: discord.Interaction) -> None:
        """The refusal a newcomer meets first, so it points at the way in."""
        await self._send_private(
            inter, f"You have no wallet yet.{SEP}`/rh wallet create` makes one, or press the button{SEP}"
                   f"`/rh tutorial` explains the rest",
            view=self._view("wallet_nudge") if allowed(inter.user.id) else None,
        )

    async def _deny_own_funds(self, inter: discord.Interaction) -> bool:
        """Export and withdraw: yours if you have a wallet, regardless of the kill switch."""
        if wallets.get(inter.user.id) is None:
            problem = _vault_problem() if not wallets.loaded() else None
            await self._send_private(inter, problem or "You have no wallet yet.")
            return True
        problem = _vault_problem()
        if problem:
            await self._send_private(inter, f"🔒 {problem}")
            return True
        return False

    async def _resolve_token(self, text: str) -> Tuple[str, str, int]:
        """(address, symbol, decimals) from an address or a symbol seen on the chain's busiest pools."""
        q = (text or "").strip()
        if chain.is_address(q):
            sym, dec = await chain.erc20_meta(q)
            return chain.to_checksum(q), sym, dec
        # Busiest pools first, then the newest, then the short-window trending
        # list, then what the feed has posted: a symbol seen anywhere McCap
        # shows tokens should resolve.
        found = {}
        for t in await self._known_tokens():
            if t.symbol.upper() == q.upper():
                found.setdefault(t.address.lower(), t)
        matches = list(found.values())
        if len(matches) == 1:
            t = matches[0]
            decimals = getattr(t, "decimals", 0)
            if decimals:
                return chain.to_checksum(t.address), t.symbol, int(decimals)
            sym, dec = await chain.erc20_meta(t.address)
            return chain.to_checksum(t.address), sym or t.symbol, dec
        if len(matches) > 1:
            raise ValueError(f"Several tokens use the symbol {q}; pass the contract address instead.")
        raise ValueError(f"Unknown token {q!r}. Pass a contract address (see /rh trending for the busy ones).")

    @staticmethod
    async def _known_tokens() -> list:
        """Every token McCap has shown lately: busiest pools, newest pools, the 5m
        trending list, and the feed's posts. Each source is best effort."""
        from .. import discovery
        out = []
        for getter in (rhchain.top_pools, rhchain.new_pools, lambda: rhchain.trending_pools("5m")):
            try:
                out.extend(rhchain.aggregate(await getter()))
            except Exception:
                log.debug("Token source failed during symbol resolution", exc_info=True)
        try:
            for ca, (symbol, decimals) in discovery.recent_tokens().items():
                out.append(rhchain.TokenActivity(symbol=symbol, name=symbol, address=ca))
                out[-1].decimals = decimals
        except Exception:
            log.debug("Feed tokens unavailable during symbol resolution", exc_info=True)
        return out

    async def _eth_usd(self) -> Optional[float]:
        return await trade.eth_usd()

    # ---------------- groups ----------------

    # Guild-installed only (the bot posts results into channels), usable from a
    # server or a DM with the bot. Declared here so tree defaults cannot change it.
    rhc = app_commands.Group(
        name="rh", description="Robinhood Chain: your wallet, DEX trades, and what's trending",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
        allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=False),
    )
    wallet = app_commands.Group(name="wallet", description="Your Robinhood Chain wallet", parent=rhc)

    # ---------------- wallet ----------------

    @wallet.command(name="create", description="Create your Robinhood Chain wallet (McCap holds the key)")
    async def wallet_create(self, inter: discord.Interaction):
        priv = PRIVATE
        # Gates before the defer, so a refusal stays private even when results are public.
        if await self._deny_trade(inter):
            return
        await inter.response.defer(thinking=True, ephemeral=priv)
        await self._create_wallet(inter, priv)

    async def _create_wallet(self, inter: discord.Interaction, priv: bool) -> None:
        """After the defer: mint the wallet and say what to do next. Shared with the Create wallet button."""
        try:
            w = await wallets.create(inter.user.id, label=inter.user.display_name)
        except wallets.VaultError as e:
            await inter.followup.send(f"❌ {e}", ephemeral=priv)
            return
        except Exception:
            log.exception("Wallet creation failed for user %s", inter.user.id)
            await inter.followup.send("❌ Wallet creation failed on the server; it's in the logs.", ephemeral=priv)
            return
        await inter.followup.send(
            f"✅ **{inter.user.display_name}**'s Robinhood Chain wallet\n`{w.address}`\n"
            f"Fund it by withdrawing **ETH on Robinhood Chain** from the Robinhood app to that address. "
            f"Gas is paid in ETH.\n\n{CUSTODY_WARNING}\n\n"
            f"Next{SEP}fund it, then `/rh buy <token> usd:5` or pick a token from `/rh trending`{SEP}"
            f"`/rh tutorial` walks through it\n[Explorer]({chain.explorer_address(w.address)})",
            ephemeral=priv, suppress_embeds=True, **self._view_kw(self._view("after_create_row")),
        )

    @staticmethod
    def _view_kw(view) -> dict:
        return {"view": view} if view is not None else {}

    @wallet.command(name="show", description="Your address and ETH balance")
    @app_commands.describe(private="Reply only to you")
    async def wallet_show(self, inter: discord.Interaction, private: bool = False):
        priv = PRIVATE or private
        w = wallets.get(inter.user.id)
        if w is None:
            problem = _vault_problem() if not wallets.loaded() else None
            if problem:
                await self._send_private(inter, problem)
            else:
                await self._no_wallet(inter)
            return
        await inter.response.defer(thinking=True, ephemeral=priv)
        await inter.followup.send(await self._wallet_text(inter, w), ephemeral=priv, suppress_embeds=True)

    async def _wallet_text(self, inter: discord.Interaction, w: wallets.Wallet) -> str:
        try:
            bal = await chain.native_balance(w.address)
        except chain.ChainError:
            bal = None
        eth_usd = await self._eth_usd()
        if bal is None:
            balance = "balance unavailable (RPC)"
        else:
            balance = f"**{_eth(bal)} ETH**"
            if eth_usd:
                balance += f" ({usd_str(bal / 1e18 * eth_usd)})"
        budget = f"{usd_str(ledger.remaining(inter.user.id))} of today's {usd_str(RHC_MAX_DAILY_USD)} buy budget left"
        return (f"**{inter.user.display_name}**{SEP}`{w.address}`\n{balance}{SEP}{budget}\n"
                f"[Explorer]({chain.explorer_address(w.address)})")

    async def button_wallet_show(self, inter: discord.Interaction) -> None:
        """The My wallet button: always private."""
        w = wallets.get(inter.user.id)
        if w is None:
            await self._no_wallet(inter)
            return
        await inter.response.defer(thinking=True, ephemeral=True)
        await inter.followup.send(await self._wallet_text(inter, w), ephemeral=True, suppress_embeds=True)

    @wallet.command(name="export", description="Reveal your private key (only you can see it)")
    async def wallet_export(self, inter: discord.Interaction):
        await inter.response.defer(thinking=True, ephemeral=True)
        if await self._deny_own_funds(inter):
            return
        view = ConfirmOrder(inter.user.id, RHC_CONFIRM_TIMEOUT)
        await inter.followup.send(
            "This will show your private key in an ephemeral message. Anyone who sees it controls the wallet. "
            "Import it somewhere safe, then consider moving funds out of McCap's custody.",
            view=view, ephemeral=True,
        )
        await view.wait()
        if not view.value:
            await inter.followup.send("Cancelled.", ephemeral=True)
            return
        try:
            key = wallets.export_hex(inter.user.id)
        except wallets.VaultError as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return
        log.warning("Private key exported for user %s", inter.user.id)
        await inter.followup.send(f"||`{key}`||\nDelete this message when you are done.", ephemeral=True)

    @wallet.command(name="withdraw", description="Send ETH from your wallet to an address")
    @app_commands.describe(to="Destination address (0x...)", eth="Amount of ETH, e.g. 0.05")
    async def wallet_withdraw(self, inter: discord.Interaction, to: str, eth: str):
        await inter.response.defer(thinking=True, ephemeral=True)
        if await self._deny_own_funds(inter):
            return
        to = to.strip()
        if not chain.is_address(to) or to.lower() == chain.ZERO:
            await inter.followup.send("❌ That is not a usable address.", ephemeral=True)
            return
        try:
            amount = chain.to_units(eth, 18)
        except ValueError as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return
        if amount <= 0:
            await inter.followup.send("❌ Amount must be greater than zero.", ephemeral=True)
            return
        warning = ""
        try:
            if await chain.code_size(to) > 0:
                warning = "\n⚠️ **The destination is a contract**, not a plain wallet. Be sure it can hold ETH."
        except chain.ChainError:
            warning = "\n⚠️ Could not check whether the destination is a contract."
        # Balance and fee check BEFORE the prompt, and say the most that can go.
        w = wallets.get(inter.user.id)
        try:
            bal = await chain.native_balance(w.address)
            gas = await chain.estimate_gas({"from": chain.to_checksum(w.address), "to": chain.to_checksum(to),
                                            "value": 0, "data": "0x"})
            gas_price = await chain.gas_price()
        except chain.ChainError as e:
            await inter.followup.send(f"❌ {swap.describe_error(e)}", ephemeral=True)
            return
        fee = (gas + gas * swap.GAS_BUFFER_PCT // 100) * gas_price * swap.FEE_MULTIPLIER
        if bal < amount + fee:
            max_send = max(0, bal - fee)
            await inter.followup.send(
                f"🚫 Not enough ETH: you have {_eth(bal)} ETH and gas needs about {_eth(fee)} ETH, "
                f"so the most you can send is **{_eth(max_send)} ETH**.", ephemeral=True,
            )
            return
        eth_usd = await self._eth_usd()
        dollars = f" ({usd_str(amount / 1e18 * eth_usd)})" if eth_usd else ""
        view = ConfirmOrder(inter.user.id, RHC_CONFIRM_TIMEOUT)
        await inter.followup.send(
            f"Send **{_eth(amount)} ETH**{dollars} to `{chain.to_checksum(to)}`?{warning}\n"
            f"Gas ≈ {_eth(fee)} ETH{SEP}cannot be reversed{SEP}expires {when(time.time() + RHC_CONFIRM_TIMEOUT)}",
            view=view, ephemeral=True,
        )
        await view.wait()
        if not view.value:
            await inter.followup.send("Cancelled. Nothing was sent.", ephemeral=True)
            return
        try:
            res = await swap.send_native(inter.user.id, to, amount)
        except Exception:
            log.exception("Withdraw failed for user %s", inter.user.id)
            await self._reply(inter, "❌ The withdrawal hit an internal error. Check your balance and the explorer "
                                     "before retrying.", True)
            return
        await self._reply(inter, self._describe(res, f"Sent {_eth(amount)} ETH"), True)

    # ---------------- trades ----------------

    @rhc.command(name="buy", description="Buy a token with ETH or dollars from your wallet (shows the quote, asks to confirm)")
    @app_commands.describe(
        token="Contract address or a symbol from /rh trending",
        eth="ETH to spend, e.g. 0.01 (or use usd)",
        usd="Dollars to spend instead of ETH, e.g. 5",
        slippage_bps="Max slippage in basis points (default 200 = 2%)", private="Keep the result to yourself too",
    )
    async def buy(self, inter: discord.Interaction, token: str, eth: Optional[str] = None,
                  usd: Optional[float] = None, slippage_bps: Optional[int] = None, private: bool = False):
        priv = PRIVATE or private          # the result; the prompt is always private
        if await self._deny_trade(inter):
            return
        w = wallets.get(inter.user.id)
        if w is None:
            await self._no_wallet(inter)
            return
        if (eth is None) == (usd is None):
            await self._send_private(inter, "Give either `eth` or `usd`, e.g. `/rh buy PONS eth:0.01` or `/rh buy PONS usd:5`.")
            return
        await inter.response.defer(thinking=True, ephemeral=True)
        await self._run_buy(inter, w, token, eth=eth, usd=usd, bps=clamp_slippage(slippage_bps), priv=priv, source="slash")

    async def _run_buy(self, inter: discord.Interaction, w: wallets.Wallet, token: str, *, eth: Optional[str],
                       usd: Optional[float], bps: int, priv: bool, source: str) -> None:
        """From a deferred, private interaction to a receipt: quote, Confirm,
        settle. The slash command and every Buy button end up here, so the
        guards in rhc/trade.py are the guards each of them gets."""
        uid = inter.user.id
        eth_usd = await self._eth_usd()
        try:
            addr, sym, dec = await self._resolve_token(token)
            if usd is not None:
                if usd <= 0:
                    raise ValueError("usd must be greater than zero")
                if not eth_usd:
                    raise ValueError("Could not get the ETH price to convert dollars; pass eth instead.")
                amount = int(usd / eth_usd * 1e18)
            else:
                amount = chain.to_units(eth, 18)
            plan = await trade.plan_buy(uid, w.address, addr, sym, dec, amount, bps, eth_usd)
        except trade.Refusal as r:
            await inter.followup.send(str(r), ephemeral=True)
            return
        except (ValueError, kyber.KyberError, chain.ChainError) as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return

        text = trade.quote_text(plan.rt, addr, sym, dec, plan.back, False, plan.liq)
        if plan.thin_pool:
            text += "\n" + trade.thin_pool_line(bps)
        text += f"\nSlippage {pct(bps / 100, signed=False)}{SEP}expires {when(time.time() + RHC_CONFIRM_TIMEOUT)}"
        view = ConfirmOrder(uid, RHC_CONFIRM_TIMEOUT)
        await inter.followup.send(text, view=view, ephemeral=True)
        await view.wait()
        if not view.value:
            await inter.followup.send("⏲️ Expired, nothing was bought. Run it again and press Confirm."
                                      if view.value is None else "Cancelled, nothing was bought.", ephemeral=True)
            return

        # The confirmed numbers are the deal: the fresh quote taken at settle
        # time may not undercut the floor the user agreed to.
        floor = kyber.min_out(plan.rt.amount_out, bps)
        first = not ledger.history(uid)
        extra = {"decimals": dec, "eth_usd": eth_usd, "mc_usd": (plan.info or {}).get("mc"),
                 "price_usd": (plan.info or {}).get("price"), "source": source}
        try:
            res, built, _basis = await trade.settle_buy(uid, w.address, plan, confirmed_floor=floor, extra=extra)
        except trade.Refusal as r:
            await inter.followup.send(str(r), ephemeral=True)
            return
        except kyber.KyberError as e:
            await self._reply(inter, f"❌ {e}", priv)
            return
        except Exception:
            log.exception("Buy failed for user %s", uid)
            await self._reply(inter, "❌ internal error; nothing should have been sent, but check the explorer", priv)
            return
        if first and (res.ok or res.pending):
            await inter.followup.send(FIRST_BUY_TIP, ephemeral=True)
        await self._reply(inter, trade.describe(res, trade.buy_success(res, built, plan)), priv,
                          view=self._receipt_view(uid, addr) if (res.ok or res.pending) else None)

    @rhc.command(name="sell", description="Sell a percentage of a token you hold for ETH (asks to confirm)")
    @app_commands.describe(
        token="Contract address or a symbol from /rh trending", percent="1 to 100",
        slippage_bps="Max slippage in basis points (default 200 = 2%)", private="Keep the result to yourself too",
    )
    async def sell(self, inter: discord.Interaction, token: str, percent: int, slippage_bps: Optional[int] = None,
                   private: bool = False):
        priv = PRIVATE or private          # the result; the prompt is always private
        if await self._deny_trade(inter):
            return
        w = wallets.get(inter.user.id)
        if w is None:
            await self._no_wallet(inter)
            return
        await inter.response.defer(thinking=True, ephemeral=True)
        await self._run_sell(inter, w, token, percent, bps=clamp_slippage(slippage_bps), priv=priv, source="slash")

    async def _run_sell(self, inter: discord.Interaction, w: wallets.Wallet, token: str, percent: int, *,
                        bps: int, priv: bool, source: str) -> None:
        """Deferred, private interaction to a receipt; the slash command and every Sell button end up here."""
        uid = inter.user.id
        try:
            addr, sym, dec = await self._resolve_token(token)
            plan = await trade.plan_sell(uid, w.address, addr, sym, dec, percent, bps)
        except trade.Refusal as r:
            await inter.followup.send(str(r).strip(), ephemeral=True)
            return
        except (ValueError, kyber.KyberError, chain.ChainError) as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return

        view = ConfirmOrder(uid, RHC_CONFIRM_TIMEOUT)
        await inter.followup.send(
            f"Sell **{chain.fmt_units(plan.amount, dec)} {sym}** ({plan.pct}% of {chain.fmt_units(plan.have, dec)}) for "
            f"**≈ {_eth(plan.rt.amount_out)} ETH ({usd_str(plan.rt.amount_out_usd)})**?\n`{addr}`\n"
            f"Slippage {pct(bps / 100, signed=False)}{SEP}gas ≈ {usd_str(plan.rt.gas_usd)}{SEP}"
            f"expires {when(time.time() + RHC_CONFIRM_TIMEOUT)}",
            view=view, ephemeral=True,
        )
        await view.wait()
        if not view.value:
            await inter.followup.send("⏲️ Expired, nothing was sold. Run it again and press Confirm."
                                      if view.value is None else "Cancelled, nothing was sold.", ephemeral=True)
            return
        floor = kyber.min_out(plan.rt.amount_out, bps)
        info = await trade.summary(addr)
        extra = {**trade.sell_extra(uid, addr, dec, info), "source": source}
        try:
            res, built = await trade.settle_sell(uid, w.address, plan, confirmed_floor=floor, extra=extra)
        except trade.Refusal as r:
            await inter.followup.send(str(r), ephemeral=True)
            return
        except kyber.KyberError as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return
        except Exception:
            log.exception("Sell failed for user %s", uid)
            await inter.followup.send("❌ Internal error during the sell. Check your balances and the explorer "
                                      "before retrying.", ephemeral=True)
            return
        panel = self._receipt_view(uid, addr, partial=True) if (res.ok and plan.pct < 100) else None
        await self._reply(inter, trade.describe(res, trade.sell_success(res, built, plan, extra)), priv, view=panel)

    @staticmethod
    def _receipt_view(uid: int, addr: str, partial: bool = False):
        """The buttons under a receipt, or None until the button layer exists."""
        maker = getattr(views, "partial_sell_row" if partial else "receipt_row", None)
        return maker(uid, addr) if maker else None

    # ---------------- your book ----------------

    @rhc.command(name="holdings", description="What your wallet holds: cost, worth now, multiple, total")
    @app_commands.describe(public="Show it to the channel instead of just you")
    async def holdings(self, inter: discord.Interaction, public: bool = False):
        priv = PRIVATE or not public
        w = wallets.get(inter.user.id)
        if w is None:
            await self._no_wallet(inter)
            return
        await inter.response.defer(thinking=True, ephemeral=priv)
        u = await pnl.user_pnl(inter.user.id, w.address)
        eth_part = f"{eth_str(u.eth)} ETH" + (f" ({usd_str(u.eth_value_usd)})" if u.eth_value_usd is not None else "")
        embed = discord.Embed(
            title=f"{inter.user.display_name}'s wallet",
            colour=colour_for(u.pnl_usd if u.positions else None),
            description=(
                f"**{usd_str(u.total_usd)}** total{SEP}{eth_part}{SEP}tokens {usd_str(u.tokens_worth_usd)}\n"
                f"`{w.address}`"
            ),
        )
        lines = [
            f"**{p.symbol}**{SEP}{qty(p.balance)}{SEP}**{usd_str(p.worth_usd)}** now{SEP}"
            f"cost {usd_str(p.open_cost_usd)}{SEP}**{mult(p.multiple_now)}**"
            for p in u.open_positions
        ]
        embed.add_field(
            name="Open positions",
            value=fit_lines(lines, 1024) if lines else "No open positions. `/rh buy` opens one.",
            inline=False,
        )
        if u.positions:
            embed.add_field(
                name="Profit",
                value=(f"Net **{usd_str(u.pnl_usd, signed=True)}**{SEP}unrealized {usd_str(u.unrealized_usd, signed=True)}"
                       f"{SEP}realized {usd_str(u.realized_usd, signed=True)}{SEP}gas {usd_str(u.gas_usd)}"),
                inline=False,
            )
        embed.set_footer(text=footer("Prices from DexScreener", "/rh pnl for the chart"))
        await inter.followup.send(embed=embed, ephemeral=priv)

    @rhc.command(name="history", description="Your recent trades and withdrawals")
    @app_commands.describe(count="How many to show (default 10, max 25)", public="Show it to the channel instead of just you")
    async def history(self, inter: discord.Interaction, count: Optional[int] = 10, public: bool = False):
        priv = PRIVATE or not public
        w = wallets.get(inter.user.id)
        if w is None:
            await self._no_wallet(inter)
            return
        await inter.response.defer(thinking=True, ephemeral=priv)
        n = max(1, min(int(count or 10), 25))
        rows = ledger.history(inter.user.id)[-n:][::-1]
        if not rows:
            await inter.followup.send(f"**{inter.user.display_name}** has no trades yet.", ephemeral=priv)
            return
        icons = {"confirmed": "✅", "reverted": "❌", "dropped": "🚫", "submitted": "⏳", "pending": "⏳"}
        lines = []
        for e in rows:
            icon = icons.get(e.get("final_status"), "❔")
            if e.get("source") == "auto":
                icon = f"🤖 {icon}"
            stamp = when(float(e.get("ts") or 0))
            dec = int(e.get("decimals") or 18)
            tx = e.get("tx") or ""
            link = f" [tx]({chain.explorer_tx(tx)})" if tx else ""
            if e.get("kind") == "buy":
                got = int(float(e.get("actual_out_estimate") or e.get("quoted_out") or 0))
                lines.append(f"{icon} {stamp} Bought {chain.fmt_units(got, dec)} {e.get('symbol')} for "
                             f"{_eth(int(float(e.get('amount_in') or 0)))} ETH ({usd_str(pnl._f(e.get('usd_in')))}){link}")
            elif e.get("kind") == "sell":
                out = int(float(e.get("actual_out_estimate") or e.get("quoted_out") or 0))
                x = f"{SEP}{mult(float(e['multiple']))}" if e.get("multiple") else ""
                lines.append(f"{icon} {stamp} Sold {chain.fmt_units(int(float(e.get('amount_in') or 0)), dec)} "
                             f"{e.get('symbol')} for {_eth(out)} ETH ({usd_str(pnl._f(e.get('usd_out')))}){x}{link}")
            else:
                lines.append(f"{icon} {stamp} Sent {_eth(int(float(e.get('amount') or 0)))} ETH to "
                             f"`{short_ca(e.get('to') or '')}`{link}")
        embed = discord.Embed(title=f"{inter.user.display_name}'s last {plural(len(rows), 'transaction')}",
                              colour=NEUTRAL, description=fit_lines(lines, 4000))
        await inter.followup.send(embed=embed, ephemeral=priv)

    @rhc.command(name="pnl", description="Your profit per token, gas spent, and a chart")
    @app_commands.describe(public="Show it to the channel instead of just you")
    async def pnl_cmd(self, inter: discord.Interaction, public: bool = False):
        priv = PRIVATE or not public
        w = wallets.get(inter.user.id)
        if w is None:
            await self._no_wallet(inter)
            return
        await inter.response.defer(thinking=True, ephemeral=priv)
        u = await pnl.user_pnl(inter.user.id, w.address)
        if not u.positions:
            await inter.followup.send(f"**{inter.user.display_name}** has no trades yet.", ephemeral=priv)
            return
        embed = discord.Embed(
            title=f"{inter.user.display_name}'s profit",
            colour=colour_for(u.pnl_usd),
            description=(f"**Net {usd_str(u.pnl_usd, signed=True)}**{SEP}bought {usd_str(u.cost_usd)} of tokens{SEP}"
                         f"realized {usd_str(u.realized_usd, signed=True)}{SEP}unrealized {usd_str(u.unrealized_usd, signed=True)}"
                         f"{SEP}gas {usd_str(u.gas_usd)}"),
        )
        lines = [
            f"**{p.symbol}**{SEP}net **{usd_str(p.pnl_usd, signed=True)}**{SEP}{mult(p.multiple_now)}{SEP}cost {usd_str(p.cost_usd)}"
            for p in u.positions
        ]
        embed.add_field(name="By token", value=fit_lines(lines, 1024), inline=False)
        png = await asyncio.to_thread(pnl.render_chart, u, f"{inter.user.display_name}'s profit by token")
        files = []
        if png:
            files = [discord.File(io.BytesIO(png), filename="pnl.png")]
            embed.set_image(url="attachment://pnl.png")
        await inter.followup.send(embed=embed, files=files, ephemeral=priv)

    @rhc.command(name="stats", description="Group metrics: trades, volume, gas spent, realized profit, best multiple")
    @app_commands.describe(private="Reply only to you")
    async def stats(self, inter: discord.Interaction, private: bool = False):
        priv = PRIVATE or private
        await inter.response.defer(thinking=True, ephemeral=priv)
        g = pnl.group_stats()
        eth_usd = await self._eth_usd()
        summary = await portfolio.summary()
        gas = usd_str(g.gas_wei / 1e18 * eth_usd) if eth_usd else f"{eth_str(g.gas_wei / 1e18)} ETH"
        best = f"{mult(g.best_multiple)} {g.best_symbol}".strip() if g.best_multiple else UNKNOWN
        holding = f"{eth_str(summary.eth)} ETH"
        if summary.positions:
            holding += f" + {plural(len(summary.positions), 'token')}"
        embed = discord.Embed(
            title=f"Group stats{SEP}Robinhood Chain",
            colour=NEUTRAL,
            description=(
                f"**{plural(g.wallets, 'wallet')}**, {g.traders} trading{SEP}holding **{usd_str(summary.total_usd)}** ({holding})\n"
                f"{plural(g.buys, 'buy')}, {plural(g.sells, 'sell')}{SEP}{usd_str(g.volume_usd)} traded{SEP}gas {gas}\n"
                f"Realized **{usd_str(g.realized_usd, signed=True)}**{SEP}best sell {best}"
            ),
        )
        await inter.followup.send(embed=embed, ephemeral=priv)

    # ---------------- auto-orders ----------------
    # A rule is confirmed once, here, and then fires without asking again (see
    # rhc/orders.py). Creation runs every check a manual trade would, so a rule
    # that could never fill is refused now rather than failing later.

    auto = app_commands.Group(name="auto", description="Sells and buys that fire once, without asking again", parent=rhc)

    @auto.command(name="sell", description="Take-profit or stop-loss: sell a percentage when the market cap reaches a level")
    @app_commands.describe(
        token="Contract address or a symbol from /rh trending", percent="How much of the holding to sell, 1 to 100",
        at="2x, +50%, -30%, or a market cap like 500k", anchor="What 2x / -30% are measured from (default: your entry)",
        expires="How long the rule stays armed, e.g. 12h or 3d (default 7d, max 30d)",
        slippage_bps="Max slippage in basis points (default 200 = 2%)", private="Report the fill by DM instead of the channel",
    )
    @app_commands.choices(anchor=[
        app_commands.Choice(name="my entry (default)", value="entry"),
        app_commands.Choice(name="the market cap now", value="now"),
    ])
    async def auto_sell(self, inter: discord.Interaction, token: str, percent: int, at: str,
                        anchor: Optional[app_commands.Choice[str]] = None, expires: Optional[str] = None,
                        slippage_bps: Optional[int] = None, private: bool = False):
        await self._arm(inter, side="sell", token=token, size=percent, at=at, anchor=anchor.value if anchor else "entry",
                        expires=expires, slippage_bps=slippage_bps, private=private)

    @auto.command(name="buy", description="Buy once when the market cap or 1h volume reaches a level (no confirm at that moment)")
    @app_commands.describe(
        token="Contract address or a symbol from /rh trending", usd="Dollars to spend when it fires",
        condition="What to wait for", value="The level, e.g. 200k or 1.5m",
        expires="How long the rule stays armed, e.g. 6h or 2d (default 24h, max 30d)",
        slippage_bps="Max slippage in basis points (default 200 = 2%)", private="Report the fill by DM instead of the channel",
    )
    @app_commands.choices(condition=[
        app_commands.Choice(name="Market cap at or below", value="mc_below"),
        app_commands.Choice(name="Market cap at or above", value="mc_above"),
        app_commands.Choice(name="1h volume at or above", value="vol1h_above"),
    ])
    async def auto_buy(self, inter: discord.Interaction, token: str, usd: float, condition: app_commands.Choice[str],
                       value: str, expires: Optional[str] = None, slippage_bps: Optional[int] = None, private: bool = False):
        await self._arm(inter, side="buy", token=token, size=usd, condition=condition.value, value=value,
                        expires=expires, slippage_bps=slippage_bps, private=private)

    @auto.command(name="list", description="Your armed auto-orders")
    @app_commands.describe(public="Show it to the channel instead of just you")
    async def auto_list(self, inter: discord.Interaction, public: bool = False):
        from ..rhc import orders
        priv = PRIVATE or not public
        await inter.response.defer(thinking=True, ephemeral=priv)
        mine = storage.orders_for(inter.user.id)
        if not mine:
            await inter.followup.send(f"**{inter.user.display_name}** has no auto-orders.{SEP}`/rh auto sell` or the "
                                      f"**TP / SL** button under a receipt arms one.", ephemeral=priv)
            return
        engine = getattr(self.bot, "auto_orders", None)
        held = engine.held_reason() if engine is not None else None
        icons = {"sell": "🎯", "buy": "🛒"}
        lines = []
        for o in mine:
            snap = storage.cache_snapshot(o.ca)
            now_val = orders.rule_value(o, snap=snap, now=time.time()) if o.metric in ("mc", "vol1h") else None
            icon = "⏳" if o.status == "pending" else icons.get(o.side, "🤖")
            waiting = engine.held_for(o.id) if engine is not None else None
            lines.append(footer(f"`{o.id}` {icon} {orders.describe_rule(o)}", f"now {usd_str(now_val)}",
                                f"high {usd_str(o.high_mc)}" if o.trail_pct and o.high_mc else "",
                                f"expires {when(o.expires_ts)}",
                                f"⏸ held {when(waiting[1])}: {waiting[0]}" if waiting else ""))
        embed = discord.Embed(title=f"{inter.user.display_name}'s auto-orders", colour=NEUTRAL,
                              description=(f"⏸ on hold: {held}\n" if held else "") + fit_lines(lines, 3800))
        embed.set_footer(text=footer(plural(len(mine), "rule"), "/rh auto cancel <id> removes one",
                                     "each fires once, without asking again"))
        await inter.followup.send(embed=embed, ephemeral=priv)

    async def _cancel_autocomplete(self, inter: discord.Interaction, current: str):
        from ..rhc import orders
        q = (current or "").lower()
        out = []
        for o in storage.orders_for(inter.user.id):
            label = f"{o.id}{SEP}{orders.describe_rule(o)}"
            if q and q not in label.lower():
                continue
            out.append(app_commands.Choice(name=label[:100], value=o.id))
        return out[:25]

    @auto.command(name="cancel", description="Remove one of your auto-orders")
    @app_commands.describe(id="The rule id from /rh auto list")
    @app_commands.autocomplete(id=_cancel_autocomplete)
    async def auto_cancel(self, inter: discord.Interaction, id: str):
        from ..rhc import orders
        o = storage.find_order(id.strip().lower(), inter.user.id)
        if o is None:
            await self._send_private(inter, f"No auto-order `{id}` of yours here.{SEP}`/rh auto list` shows them.")
            return
        if o.status == "firing":
            await self._send_private(inter, "This rule is executing right now; its result will post here in a moment.")
            return
        if o.status == "pending":
            await self._send_private(inter, "This rule already fired; waiting for the chain to confirm it.")
            return
        try:
            storage.auto_orders.remove(o)
        except ValueError:
            await self._send_private(inter, "That rule is already gone.")
            return
        await storage.save_orders()
        await self._send_private(inter, f"Cancelled `{o.id}`{SEP}{orders.describe_rule(o)}")

    async def _arm(self, inter: discord.Interaction, *, side: str, token: str, size: float, at: Optional[str] = None,
                   anchor: str = "entry", condition: Optional[str] = None, value: Optional[str] = None,
                   expires: Optional[str] = None, slippage_bps: Optional[int] = None, private: bool = False) -> None:
        """Shared creation flow for /rh auto sell and /rh auto buy."""
        from ..rhc import orders
        uid = inter.user.id
        if await self._deny_trade(inter):
            return
        w = wallets.get(uid)
        if w is None:
            await self._no_wallet(inter)
            return
        if await self._auto_limits(inter):
            return
        await inter.response.defer(thinking=True, ephemeral=True)
        try:
            addr, sym, dec = await self._resolve_token(token)
            bps = clamp_slippage(slippage_bps)
            expires_ts = orders.parse_expiry(expires, RHC_AUTO_SELL_TTL if side == "sell" else RHC_AUTO_BUY_TTL, RHC_AUTO_MAX_TTL)
        except (ValueError, chain.ChainError) as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return
        info = await trade.summary(addr)
        if info is None:
            await inter.followup.send("❌ No DexScreener data for that token yet; try again in a few minutes.", ephemeral=True)
            return
        if side == "sell":
            prepared = await self._prepare_auto_sell(inter, w, addr, sym, dec, int(size), at or "", anchor, bps, info,
                                                     expires_ts, private)
        else:
            prepared = await self._prepare_auto_buy(inter, w, addr, sym, dec, float(size), condition or "", value or "",
                                                    bps, info, expires_ts, private)
        if prepared is None:
            return
        order, prompt = prepared
        await self._confirm_and_arm(inter, [order], prompt, info, PRIVATE or private)

    async def _auto_limits(self, inter: discord.Interaction, n: int = 1) -> bool:
        """Reply and return True when ``n`` more rules may not be added."""
        from ..rhc import orders
        ok, why = orders.room_for(inter.user.id, n)
        if ok:
            return False
        await self._send_private(inter, why)
        return True

    async def _prepare_auto_sell(self, inter: discord.Interaction, w: wallets.Wallet, addr: str, sym: str, dec: int,
                                 pct_sold: int, at: str, anchor: str, bps: int, info: dict, expires_ts: float,
                                 private: bool) -> Optional[Tuple[AutoOrder, str]]:
        """Validate a take-profit / stop-loss and build it with its prompt; replies and returns None on refusal."""
        from ..rhc import orders
        uid = inter.user.id
        pct_sold = max(1, min(int(pct_sold), 100))
        try:
            have = await chain.erc20_balance(addr, w.address)
        except chain.ChainError as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return None
        if have <= 0:
            await inter.followup.send(f"You hold no {sym}.", ephemeral=True)
            return None
        mc_now = info.get("mc")
        anchor_used, anchor_mc = anchor, None
        if anchor == "entry":
            entry = pnl.entry_for(uid, addr)
            if entry is not None:
                entry.price_now, entry.mc_now = info.get("price"), mc_now
                anchor_mc = entry.entry_mc
            if anchor_mc is None:
                anchor_used = "now"        # McCap has no entry for this wallet; say so in the prompt
        if anchor_used == "now":
            anchor_mc = mc_now
        try:
            rule = orders.sell_rule(at, anchor_mc, mc_now, anchor_used)
        except ValueError as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return None
        if orders.already_met(rule.direction, mc_now, rule.target):
            await inter.followup.send(f"🚫 {sym} is already at {usd_str(mc_now)}, past your {usd_str(rule.target)}; "
                                      f"use `/rh sell` instead.", ephemeral=True)
            return None
        o = AutoOrder(ca=addr, symbol=sym, decimals=dec, side="sell", metric="mc", direction=rule.direction,
                      target=rule.target, size=float(pct_sold), slippage_bps=bps, user_id=uid, guild_id=inter.guild_id or 0,
                      channel_id=inter.channel_id or 0, expires_ts=expires_ts, spec=rule.spec, anchor_mc=rule.anchor_mc,
                      anchor=anchor_used, private=private,
                      trail_pct=float(getattr(rule, "trail_pct", 0.0) or 0.0),
                      high_mc=float(mc_now or 0.0) if getattr(rule, "trail_pct", 0.0) else 0.0)
        arrow = "≥" if rule.direction == "above" else "≤"
        if o.trail_pct:
            basis = f"trail {o.trail_pct:g}% below its high ({usd_str(mc_now)} now); the stop rises with the price, never falls"
        elif rule.spec and anchor_used == "entry":
            basis = f"{rule.spec} from your {usd_str(anchor_mc)} entry"
        elif rule.spec:
            basis = f"{rule.spec} from {usd_str(anchor_mc)} now" + ("" if anchor == "now" else " (McCap has no entry for you)")
        else:
            basis = ""
        prompt = (
            f"Arm **sell {pct_sold}% of {sym}** when MC {arrow} **{usd_str(rule.target)}**?\n"
            + footer(basis, f"now {usd_str(mc_now)}", f"slippage {pct(bps / 100, signed=False)}", "checked every 10–60s",
                     f"expires {when(expires_ts)}")
            + f"\n`{addr}`\n⚠️ Fires **without asking again**, once. Needs gas ETH in your wallet then. A sell that cannot "
              f"meet its slippage is retried about every minute and dropped after {orders.MAX_ATTEMPTS} failures; it never "
              f"widens. `/rh auto cancel` any time."
        )
        return o, prompt

    async def _prepare_auto_buy(self, inter: discord.Interaction, w: wallets.Wallet, addr: str, sym: str, dec: int,
                                size_usd: float, condition: str, value: str, bps: int, info: dict, expires_ts: float,
                                private: bool) -> Optional[Tuple[AutoOrder, str]]:
        """Validate a one-shot buy the way /rh buy would today, then build it with its prompt."""
        from ..rhc import orders
        uid = inter.user.id
        if size_usd <= 0 or size_usd > RHC_MAX_TRADE_USD:
            await inter.followup.send(f"🚫 Auto-buys are between $0 and {usd_str(RHC_MAX_TRADE_USD)} each.", ephemeral=True)
            return None
        ok, why = ledger.check(uid, size_usd)
        if not ok:
            await inter.followup.send(f"🚫 {why}", ephemeral=True)
            return None
        try:
            rule = orders.buy_rule(condition, value)
        except ValueError as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return None
        current = info.get("mc") if rule.metric == "mc" else info.get("vol1h")
        metric_label = "MC" if rule.metric == "mc" else "1h volume"
        if orders.already_met(rule.direction, current, rule.target):
            await inter.followup.send(f"🚫 {sym}'s {metric_label} is already {usd_str(current)}, past your "
                                      f"{usd_str(rule.target)}; use `/rh buy` instead.", ephemeral=True)
            return None
        eth_usd = await self._eth_usd()
        if not eth_usd:
            await inter.followup.send("❌ Could not get the ETH price to size the buy; try again shortly.", ephemeral=True)
            return None
        amount = int(size_usd / eth_usd * 1e18)
        try:
            rt = await kyber.route(chain.NATIVE, addr, amount)
        except kyber.NoRoute as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return None
        except kyber.KyberError:
            await inter.followup.send("❌ KyberSwap is not answering; try again shortly.", ephemeral=True)
            return None
        # The cap is checked against the larger of the typed size and Kyber's
        # own valuation of the ETH leg, the way the fire will check it.
        basis = max(size_usd, rt.amount_in_usd or 0.0)
        ok, why = ledger.check(uid, basis)
        if not ok:
            await inter.followup.send(f"🚫 KyberSwap values that at {usd_str(basis)} right now: {why} Size it a little "
                                      f"under the cap.", ephemeral=True)
            return None
        back, unavailable = await trade.round_trip(addr, rt.amount_out)
        if unavailable:
            await inter.followup.send("❌ KyberSwap is not answering; the sell-back check cannot run. Try again shortly.",
                                      ephemeral=True)
            return None
        if back is None:
            await inter.followup.send(f"🚫 **{sym}** cannot be sold back for ETH (honeypot). No rule armed.", ephemeral=True)
            return None
        liq = info.get("liq")
        rt_pct = (back.amount_out / rt.amount_in - 1.0) * 100.0 if rt.amount_in else None
        o = AutoOrder(ca=addr, symbol=sym, decimals=dec, side="buy", metric=rule.metric, direction=rule.direction,
                      target=rule.target, size=float(size_usd), slippage_bps=bps, user_id=uid, guild_id=inter.guild_id or 0,
                      channel_id=inter.channel_id or 0, expires_ts=expires_ts, private=private)
        arrow = "≥" if rule.direction == "above" else "≤"
        prompt = (
            f"Arm **buy {usd_str(size_usd)} of {sym}** when {metric_label} {arrow} **{usd_str(rule.target)}**?\n"
            + footer(f"now {usd_str(current)}",
                     f"sells straight back for {_eth(back.amount_out)} ETH ({pct(rt_pct)} round trip)" if rt_pct is not None else "",
                     f"liquidity {usd_str(liq)}" if liq is not None else "", f"slippage {pct(bps / 100, signed=False)}",
                     f"expires {when(expires_ts)}")
            + ("\n" + trade.thin_pool_line(bps) if (liq is not None and liq < THIN_POOL_USD and bps < trade.THIN_POOL_MIN_BPS) else "")
            + f"\n`{addr}`\n⚠️ Fires **without asking again**, once; the daily cap and the sell-back check run again when it does."
        )
        return o, prompt

    async def _confirm_and_arm(self, inter: discord.Interaction, new_orders: list, prompt: str, info: dict, priv: bool) -> None:
        """One private Confirm for one or two rules, then persist and announce them."""
        from ..rhc import orders
        uid = inter.user.id
        view = ConfirmOrder(uid, RHC_CONFIRM_TIMEOUT)
        await inter.followup.send(prompt, view=view, ephemeral=True)
        await view.wait()
        if not view.value:
            await inter.followup.send("⏲️ Expired, nothing was armed." if view.value is None else "Cancelled, nothing was armed.",
                                      ephemeral=True)
            return
        # Limits again: another rule may have been armed while the prompt sat there.
        ok, _why = orders.room_for(uid, len(new_orders))
        if not ok:
            await inter.followup.send("🚫 The auto-order limit was reached while you were confirming; nothing was armed.",
                                      ephemeral=True)
            return
        storage.auto_orders.extend(new_orders)
        await storage.save_orders()
        mc_now = (info or {}).get("mc")
        for o in new_orders:
            await self._reply(inter, f"🤖 Armed `{o.id}`{SEP}{orders.describe_rule(o)}{SEP}now {usd_str(mc_now)}{SEP}"
                                     f"expires {when(o.expires_ts)}", priv)

    async def modal_tpsl(self, inter: discord.Interaction, uid: int, token: str, tp_at: str, tp_pct: str,
                         sl_at: str, sl_pct: str) -> None:
        """The TP / SL modal under a receipt: up to two sell rules behind one Confirm. Runs after the modal's defer."""
        if inter.user.id != uid:
            await inter.followup.send("This isn't your position.", ephemeral=True)
            return
        if await self._deny_trade(inter):
            return
        w = wallets.get(uid)
        if w is None:
            await self._no_wallet(inter)
            return
        if await self._auto_limits(inter):
            return
        from ..rhc import orders
        legs = [(tp_at, tp_pct), (sl_at, sl_pct)]
        legs = [(a.strip(), p.strip()) for a, p in legs if (a or "").strip()]
        if not legs:
            await inter.followup.send("Nothing to arm: both levels were blank.", ephemeral=True)
            return
        try:
            addr, sym, dec = await self._resolve_token(token)
            expires_ts = orders.parse_expiry(None, RHC_AUTO_SELL_TTL, RHC_AUTO_MAX_TTL)
            pcts = [max(1, min(int(float(p or "100")), 100)) for _a, p in legs]
        except (ValueError, chain.ChainError) as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return
        info = await trade.summary(addr)
        if info is None:
            await inter.followup.send("❌ No DexScreener data for that token yet; try again in a few minutes.", ephemeral=True)
            return
        bps = clamp_slippage(None)
        built, prompts = [], []
        for (at, _p), pct_sold in zip(legs, pcts):
            prepared = await self._prepare_auto_sell(inter, w, addr, sym, dec, pct_sold, at, "entry", bps, info,
                                                     expires_ts, PRIVATE)
            if prepared is None:
                return
            o, prompt = prepared
            built.append(o)
            prompts.append(prompt.split("\n⚠️")[0])
        tail = ("\n⚠️ Fires **without asking again**, once each. Needs gas ETH in your wallet then. A sell that cannot meet "
                f"its slippage is retried about every minute and dropped after {orders.MAX_ATTEMPTS} failures; it never "
                "widens. `/rh auto cancel` any time.")
        await self._confirm_and_arm(inter, built, "\n\n".join(prompts) + tail, info, PRIVATE)

    async def modal_buy(self, inter: discord.Interaction, token: str, usd_text: str, eth_text: str) -> None:
        """The Other amount modal: exactly one of USD / ETH, then the ordinary buy flow. Runs after the modal's defer."""
        usd_text, eth_text = (usd_text or "").strip(), (eth_text or "").strip()
        if bool(usd_text) == bool(eth_text):
            await inter.followup.send("Give either USD or ETH, not both and not neither.", ephemeral=True)
            return
        if await self._deny_trade(inter):
            return
        w = wallets.get(inter.user.id)
        if w is None:
            await self._no_wallet(inter)
            return
        usd_val = None
        if usd_text:
            try:
                usd_val = float(usd_text.replace("$", "").replace(",", ""))
            except ValueError:
                await inter.followup.send("❌ USD must be a number, e.g. 7.50", ephemeral=True)
                return
        await self._run_buy(inter, w, token, eth=eth_text or None, usd=usd_val, bps=clamp_slippage(None), priv=PRIVATE,
                            source="button")

    # ---------------- tutorial ----------------

    @rhc.command(name="tutorial", description="How trading with McCap works, step by step")
    @app_commands.describe(topic="Which part (default: getting started)", public="Show it to the channel instead of just you")
    @app_commands.choices(topic=[app_commands.Choice(name=label, value=value) for value, label in TUTORIAL_TOPICS])
    async def tutorial(self, inter: discord.Interaction, topic: Optional[app_commands.Choice[str]] = None,
                       public: bool = False):
        # Deferred first: the personal checklist reads a balance from the RPC.
        await inter.response.defer(thinking=True, ephemeral=not public)
        await self._send_tutorial(inter, topic.value if topic else "start", public)

    async def button_tutorial(self, inter: discord.Interaction) -> None:
        await inter.response.defer(thinking=True, ephemeral=True)
        await self._send_tutorial(inter, "start", False)

    async def _send_tutorial(self, inter: discord.Interaction, topic: str, public: bool) -> None:
        uid = inter.user.id
        w = wallets.get(uid)
        balance = eth_usd = None
        if w is not None and not public:
            try:
                balance = await asyncio.wait_for(chain.native_balance(w.address), 3)
            except Exception:
                balance = None
            if balance:
                eth_usd = await self._eth_usd()
        state = tutorial.TutorialState(
            display_name=inter.user.display_name, allowed=allowed(uid), has_wallet=w is not None,
            address=w.address if w else "", balance_wei=balance, eth_usd=eth_usd, traded=bool(ledger.history(uid)),
            gate_reason=_gate(), remaining_usd=ledger.remaining(uid), personal=not public,
        )
        embed = tutorial.build(topic, state)
        view = self._view("tutorial_row", tutorial.row_state(state))
        await inter.followup.send(embed=embed, ephemeral=not public, **self._view_kw(view))

    # ---------------- discovery feed ----------------
    # The bot finds it: new pairs, volume spikes and movers on Robinhood Chain,
    # posted to one channel per server with the trade buttons. Manager-only,
    # because it posts on its own. See mccapbot/discovery.py.

    feed = app_commands.Group(name="feed", description="Robinhood Chain discovery feed: new pairs, spikes and movers",
                              parent=rhc)

    @staticmethod
    async def _deny_manager(inter: discord.Interaction) -> bool:
        if inter.guild_id is None:
            await inter.response.send_message("The feed is a server setting; run this in the server.", ephemeral=True)
            return True
        if not is_manager(inter.user):
            await inter.response.send_message("🔒 Only server managers can change the feed.", ephemeral=True)
            return True
        return False

    @feed.command(name="on", description="Post new pairs, volume spikes and movers to a channel (server managers)")
    @app_commands.describe(
        channel="Where to post (default: this channel)", new_pairs="Post brand-new pairs that pass a second look",
        spikes="Post 5-minute volume spikes", movers="Post 5-minute price movers",
        min_liquidity="Ignore pools under this many dollars of liquidity (default 5000)",
        min_buyers="Ignore tokens with fewer distinct buyers in 5 minutes (default 8)",
        pace="Spike threshold: 5m volume at this many times the hour's pace (default 3)",
        move_pct="Mover threshold: 5m price change in percent (default 25)",
        max_per_hour="Most posts per hour, strongest first (default 10)",
    )
    async def feed_on(self, inter: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                      new_pairs: bool = True, spikes: bool = True, movers: bool = True,
                      min_liquidity: Optional[int] = 5000, min_buyers: Optional[int] = 8, pace: Optional[float] = 3.0,
                      move_pct: Optional[float] = 25.0, max_per_hour: Optional[int] = 10):
        from .. import discovery
        if await self._deny_manager(inter):
            return
        if not FEED_ENABLE:
            await inter.response.send_message("🔒 The feed is switched off on this deployment (`FEED_ENABLE=0`).", ephemeral=True)
            return
        target = channel.id if channel is not None else inter.channel_id
        cfg = discovery.FeedConfig(
            guild_id=inter.guild_id, channel_id=target, enabled=True,
            new_pairs=bool(new_pairs), spikes=bool(spikes), movers=bool(movers),
            min_liq=float(max(0, min_liquidity if min_liquidity is not None else 5000)),
            min_buyers=int(max(0, min_buyers if min_buyers is not None else 8)),
            pace=float(max(1.0, pace if pace is not None else 3.0)),
            move_pct=float(max(1.0, move_pct if move_pct is not None else 25.0)),
            max_per_hour=int(max(1, min(60, max_per_hour if max_per_hour is not None else 10))),
        )
        discovery.set_config(cfg)          # keeps whatever this server had muted
        await discovery.save_feed()
        kinds = [k for k, on in (("new pairs", cfg.new_pairs), ("spikes", cfg.spikes), ("movers", cfg.movers)) if on]
        await inter.response.send_message(
            f"📡 Feed → <#{target}>{SEP}**on**\n"
            + footer(", ".join(kinds) or "nothing selected", f"liq ≥ {usd_str(cfg.min_liq)}",
                     f"buyers ≥ {cfg.min_buyers}", f"pace ≥ {cfg.pace:g}x", f"move ≥ {pct(cfg.move_pct, signed=False)}",
                     f"cap {plural(cfg.max_per_hour, 'post')}/hour")
            + f"\nEvery post carries the trade buttons; each opens the usual private quote and Confirm. "
              f"`/rh feed status` shows how the calls did.",
        )

    @feed.command(name="off", description="Stop the discovery feed in this server (server managers)")
    async def feed_off(self, inter: discord.Interaction):
        from .. import discovery
        if await self._deny_manager(inter):
            return
        cfg = discovery.config_for(inter.guild_id)
        if cfg is None or not cfg.enabled:
            await inter.response.send_message("The feed is not on here.", ephemeral=True)
            return
        cfg.enabled = False
        await discovery.save_feed()
        await inter.response.send_message("📡 Feed **off**. `/rh feed on` starts it again with the same settings.")

    @feed.command(name="status", description="What the feed is posting and how its calls did")
    async def feed_status(self, inter: discord.Interaction):
        from .. import discovery
        if inter.guild_id is None:
            await inter.response.send_message("The feed is a server setting; run this in the server.", ephemeral=True)
            return
        # The feed writes its own status text (mccapbot/discovery.py): one place
        # decides what "stale", "capped" or "paused" reads like. A server whose
        # feed task never started still gets the settings and the grading.
        engine = getattr(self.bot, "feed", None) or discovery.Feed(self.bot)
        await inter.response.send_message(engine.status(inter.guild_id)["text"])

    @feed.command(name="mute", description="Stop the feed posting one token for a while (server managers)")
    @app_commands.describe(token="Contract address or a symbol the feed has posted", for_="How long (default 1d)")
    @app_commands.rename(for_="for")
    @app_commands.choices(for_=[
        app_commands.Choice(name="1 hour", value="1h"), app_commands.Choice(name="6 hours", value="6h"),
        app_commands.Choice(name="1 day", value="1d"), app_commands.Choice(name="7 days", value="7d"),
    ])
    async def feed_mute(self, inter: discord.Interaction, token: str, for_: Optional[app_commands.Choice[str]] = None):
        from .. import discovery
        if await self._deny_manager(inter):
            return
        cfg = discovery.config_for(inter.guild_id)
        if cfg is None:
            await inter.response.send_message("No feed here yet.", ephemeral=True)
            return
        await inter.response.defer(thinking=True)
        try:
            addr, sym, _dec = await self._resolve_token(token)
        except (ValueError, chain.ChainError) as e:
            await inter.followup.send(f"❌ {e}")
            return
        secs = {"1h": 3600, "6h": 21600, "1d": 86400, "7d": 604800}[for_.value if for_ else "1d"]
        cfg.muted[addr.lower()] = time.time() + secs
        await discovery.save_feed()
        await inter.followup.send(f"🔇 The feed will not post **{sym}** until {when(time.time() + secs)}.")

    # ---------------- trending ----------------

    WINDOW_CHOICES = [
        app_commands.Choice(name="5 minutes", value="m5"),
        app_commands.Choice(name="15 minutes", value="m15"),
        app_commands.Choice(name="30 minutes", value="m30"),
        app_commands.Choice(name="1 hour", value="h1"),
        app_commands.Choice(name="6 hours", value="h6"),
        app_commands.Choice(name="24 hours", value="h24"),
    ]

    @staticmethod
    def _addresses_field(embed: discord.Embed, tokens) -> None:
        """Addresses are what /rh buy needs, and a table cell is not copyable."""
        lines = [f"{t.symbol[:10]:<10} {t.address}" for t in tokens[:15]]
        if lines:
            embed.add_field(name="Addresses (for /rh buy and /mc)", value="```\n" + "\n".join(lines) + "\n```", inline=False)
        # Whole links only: cutting the field at 1024 characters left a broken URL.
        links = []
        for t in tokens[:10]:
            piece = f"[{t.symbol[:10]}]({t.deepest.url()})"
            if len(SEP.join(links + [piece])) > 1024:
                break
            links.append(piece)
        if links:
            embed.add_field(name="Charts", value=SEP.join(links), inline=False)

    @rhc.command(name="trending", description="Busiest and fastest-moving tokens on Robinhood Chain")
    @app_commands.describe(
        window="Volume and market-cap change over 5m to 24h (default 24h)",
        sort="volume (default), gainers, losers, newest pools, or active (most buyers)",
        count="How many to show (default 10, max 25)",
        include_majors="Also show WETH / USDG / stablecoin pools (hidden by default)",
        private="Reply only to you",
    )
    @app_commands.choices(
        window=WINDOW_CHOICES,
        sort=[
            app_commands.Choice(name="volume", value="volume"),
            app_commands.Choice(name="gainers", value="gainers"),
            app_commands.Choice(name="losers", value="losers"),
            app_commands.Choice(name="new", value="new"),
            app_commands.Choice(name="active", value="active"),
        ],
    )
    async def trending(
        self,
        inter: discord.Interaction,
        window: Optional[app_commands.Choice[str]] = None,
        sort: Optional[app_commands.Choice[str]] = None,
        count: Optional[int] = 10,
        include_majors: bool = False,
        private: bool = False,
    ):
        # Public market data: no allowlist, no gate.
        priv = PRIVATE or private
        await inter.response.defer(thinking=True, ephemeral=priv)
        w = window.value if window else "h24"
        mode = sort.value if sort else "volume"
        n = max(1, min(int(count or 10), 25))

        embed, top, problem = await self._board(w, mode, n, include_majors)
        if embed is None:
            await inter.followup.send(problem, ephemeral=priv)
            return
        await inter.followup.send(embed=embed, ephemeral=priv, **self._view_kw(self._view("board_view", "trending", top)))

    async def button_trending(self, inter: discord.Interaction) -> None:
        """The Trending now button: the default board, privately, with its picker."""
        await inter.response.defer(thinking=True, ephemeral=True)
        embed, top, problem = await self._board("h24", "volume", 10, False)
        if embed is None:
            await inter.followup.send(problem, ephemeral=True)
            return
        await inter.followup.send(embed=embed, ephemeral=True, **self._view_kw(self._view("board_view", "trending", top)))

    async def _board(self, w: str, mode: str, n: int, include_majors: bool):
        """(embed, top tokens, None) for the trending board, or (None, None, why).

        Short windows also pull GeckoTerminal's own trending list for that
        window, so a token that just started moving is not confined to the
        40 busiest pools by 24h volume."""
        label = rhchain.WINDOW_LABELS[w]
        pools = list(await rhchain.top_pools())
        if not pools:
            return None, None, "Couldn't reach GeckoTerminal for Robinhood Chain pools. Try again shortly."
        if w in TRENDING_DURATIONS:
            try:
                extra = await rhchain.trending_pools(TRENDING_DURATIONS[w])
            except Exception:
                extra = []
            seen = {p.address for p in pools}
            pools += [p for p in extra if p.address not in seen]
        tokens = rhchain.aggregate(pools)
        top = rhchain.rank(tokens, w, mode, include_majors=include_majors, n=n)
        if not top:
            return None, None, "Nothing to show for that filter."

        def mc(v: Optional[float]) -> str:
            return usd_str(v) if v else UNKNOWN

        def name(t) -> str:
            # Money moving much faster than the hour's pace is the thing to notice.
            pace = t.volume_pace("m5", "h1") if w in ("h1", "h6", "h24") else t.volume_pace(w, "h1")
            tag = f" ⚡{pace:.1f}x" if (mode == "volume" and pace is not None and pace >= 2 and w != "h1") else ""
            return f"{t.symbol[:10]}{tag}"

        rows = [[
            name(t),
            usd_str(t.volume(w)),
            pct(t.change(w)),
            str(t.buyers(w)) if t.buyers(w) else UNKNOWN,
            mc(t.mc_usd),
        ] for t in top]

        shown_tokens = [t for t in tokens if include_majors or t.symbol.upper() not in rhchain.CHAIN_MAJORS]
        total_vol = sum(t.volume(w) for t in shown_tokens)
        venues = sorted({p.dex for t in shown_tokens for p in t.pools})
        titles = {"volume": f"busiest by {label} volume", "gainers": f"{label} gainers",
                  "losers": f"{label} losers", "new": "newest of the busy pools", "active": f"most buyers in {label}"}
        desc = (f"**{usd_str(total_vol)}** traded in {label} across {plural(len(shown_tokens), 'token')}"
                + (f"{SEP}{', '.join(venues)}" if venues else ""))
        if mode in ("gainers", "losers"):
            # "$800K to $1.1M inside the window" is the number people want for
            # a mover, not just a percentage; say it for the top few, with who
            # is actually trading it.
            movers = [
                footer(f"{t.symbol[:10]}{SEP}MC {mc(t.mc_before(w))} → **{mc(t.mc_usd)}** ({pct(t.change(w))})",
                       f"{plural(t.buyers(w), 'buyer')}" if t.buyers(w) else "",
                       f"{t.buys(w)}/{t.sells(w)} buys/sells" if (t.buys(w) or t.sells(w)) else "")
                for t in top[:3] if t.mc_usd and t.mc_before(w)
            ]
            if movers:
                desc += "\n" + "\n".join(movers)
        embed = discord.Embed(title=f"Robinhood Chain{SEP}{titles[mode]}", colour=NEUTRAL, description=desc)
        shown, total = add_table_fields(
            embed, "Tokens",
            ["Token", f"Vol {label}", label, "Buyers", "MC"], rows,
            ["l", "r", "r", "r", "r"], max_fields=3,
        )
        self._addresses_field(embed, top)
        embed.set_footer(text=footer(
            "GeckoTerminal",
            self._stale_note(),
            "" if include_majors else "majors hidden (include_majors to show)",
            "/rh new for brand-new pairs",
            f"{plural(total - shown, 'row')} not shown" if shown < total else "",
        ))
        return embed, top, None

    @staticmethod
    def _stale_note() -> str:
        """'list from 4m ago (GeckoTerminal unreachable)' when the last refresh failed and the served list is old."""
        error = getattr(rhchain, "last_error", None)
        ok_ts = getattr(rhchain, "last_ok_ts", 0.0) or 0.0
        if error and ok_ts and time.time() - ok_ts > RHCHAIN_CACHE_SECONDS:
            return f"list from {age(time.time() - ok_ts)} ago (GeckoTerminal unreachable)"
        return ""

    @rhc.command(name="new", description="Brand-new pairs on Robinhood Chain, newest first")
    @app_commands.describe(
        count="How many to show (default 10, max 25)",
        min_liquidity="Hide pools with less than this many dollars of liquidity (default 5000)",
        min_buyers="Hide pools with fewer distinct buyers in the last 5 minutes (default 5)",
        private="Reply only to you",
    )
    async def new(self, inter: discord.Interaction, count: Optional[int] = 10, min_liquidity: Optional[int] = 5000,
                  min_buyers: Optional[int] = 5, private: bool = False):
        priv = PRIVATE or private
        await inter.response.defer(thinking=True, ephemeral=priv)
        n = max(1, min(int(count or 10), 25))
        floor = max(0, int(min_liquidity if min_liquidity is not None else 5000))
        buyers_floor = max(0, int(min_buyers if min_buyers is not None else 5))
        pools = await rhchain.new_pools()
        if not pools:
            await inter.followup.send("Couldn't reach GeckoTerminal for new Robinhood Chain pools. Try again shortly.")
            return
        tokens = rhchain.aggregate(pools)
        tokens = [t for t in tokens if t.symbol.upper() not in rhchain.CHAIN_MAJORS]
        kept = [t for t in tokens if t.liq_usd >= floor and t.buyers("m5") >= buyers_floor]
        kept.sort(key=lambda t: -t.created_ts)
        top = kept[:n]
        if not top:
            await inter.followup.send(f"No new pairs with at least {usd_str(floor)} of liquidity and "
                                      f"{plural(buyers_floor, 'buyer')} in the last 5 minutes ({len(tokens)} seen). "
                                      f"Lower min_liquidity or min_buyers to see the dust.")
            return

        def name(t) -> str:
            quote = t.reference.quote_symbol.upper()
            return t.symbol[:10] + ("" if quote in rhchain.CHAIN_MAJORS else f" ({quote[:6]})")

        rows = [[
            name(t),
            rhchain.age_str(t.created_ts),
            usd_str(t.liq_usd),
            str(t.buyers("m5")) if t.buyers("m5") else UNKNOWN,
            usd_str(t.mc_usd) if t.mc_usd else UNKNOWN,
        ] for t in top]
        embed = discord.Embed(
            title=f"Robinhood Chain{SEP}newest pairs",
            colour=NEUTRAL,
            description=(f"{plural(len(kept), 'new pair')} with ≥ {usd_str(floor)} liquidity and ≥ "
                         f"{plural(buyers_floor, 'buyer')} in 5m ({len(tokens)} seen){SEP}"
                         f"most are dust: check the sell-back line in /rh buy first"),
        )
        shown, total = add_table_fields(
            embed, "Pairs",
            ["Token", "Age", "Liq", "Buyers 5m", "MC"], rows,
            ["l", "r", "r", "r", "r"], max_fields=3,
        )
        self._addresses_field(embed, top)
        embed.set_footer(text=footer(
            "GeckoTerminal new-pools feed", self._stale_note(), "majors hidden",
            "a symbol in brackets is the quote token when it is not a major",
            f"{plural(total - shown, 'row')} not shown" if shown < total else "",
        ))
        await inter.followup.send(embed=embed, ephemeral=priv, **self._view_kw(self._view("board_view", "new", top)))

    # ---------------- autocomplete ----------------

    _bal_cache: dict = {}     # (user_id, token) -> (ts, raw balance)
    _price_cache: dict = {}   # token -> (ts, price or None)
    CHOICE_CACHE_SECONDS = 60
    CHOICE_READ_TIMEOUT = 2.5   # Discord allows 3s for the whole answer; both reads run in parallel

    async def _holding_choices(self, user_id: int, current: str) -> list:
        """What the caller holds, as pick-one choices for the sell commands:
        ``PONS · 36 · cost $20.00 · now $25.20 · 1.26x``.

        Autocomplete has about three seconds to answer, so balances and prices
        come from short caches and bounded reads, all in parallel; a token whose
        balance cannot be read in time is left out this keystroke, and one
        whose price cannot be read shows without its "now" part.
        """
        w = wallets.get(user_id)
        if w is None:
            return []
        positions = pnl.aggregate(ledger.trades(user_id))
        addrs = ledger.tokens_touched(user_id)[:15]
        now = time.time()

        async def balance(addr: str) -> int:
            hit = self._bal_cache.get((user_id, addr))
            if hit and now - hit[0] < self.CHOICE_CACHE_SECONDS:
                return hit[1]
            raw = await asyncio.wait_for(chain.erc20_balance(addr, w.address), self.CHOICE_READ_TIMEOUT)
            self._bal_cache[(user_id, addr)] = (now, raw)
            return raw

        async def price(addr: str) -> Optional[float]:
            hit = self._price_cache.get(addr)
            if hit and now - hit[0] < self.CHOICE_CACHE_SECONDS:
                return hit[1]
            info = await asyncio.wait_for(trade.summary(addr), self.CHOICE_READ_TIMEOUT)
            value = (info or {}).get("price")
            self._price_cache[addr] = (now, value)
            return value

        balances, prices = await asyncio.gather(
            asyncio.gather(*(balance(a) for a in addrs), return_exceptions=True),
            asyncio.gather(*(price(a) for a in addrs), return_exceptions=True),
        )
        q = (current or "").strip().lower()
        choices = []
        for addr, raw, px in zip(addrs, balances, prices):
            if isinstance(raw, BaseException) or raw <= 0:
                continue
            p = positions.get(addr)
            sym, dec = (p.symbol, p.decimals) if p else ("?", 18)
            if q and q not in sym.lower() and q not in addr.lower():
                continue
            amount = raw / 10 ** dec
            px = None if isinstance(px, BaseException) else px
            avg_cost = p.avg_cost if p else None
            label = footer(
                f"{sym}", qty(amount),
                f"cost {usd_str(amount * avg_cost)}" if avg_cost else "",
                f"now {usd_str(amount * px)}" if px else "",
                mult(px / avg_cost) if (px and avg_cost) else "",
            )
            choices.append(app_commands.Choice(name=label[:100], value=chain.to_checksum(addr)))
        return choices[:25]

    @sell.autocomplete("token")
    @auto_sell.autocomplete("token")
    async def sell_token_autocomplete(self, inter: discord.Interaction, current: str):
        try:
            return await self._holding_choices(inter.user.id, current)
        except Exception:
            log.debug("sell autocomplete failed", exc_info=True)
            return []

    @buy.autocomplete("token")
    async def buy_token_autocomplete(self, inter: discord.Interaction, current: str):
        """The busiest tokens on the chain, from the trending board's cache."""
        try:
            pools = await asyncio.wait_for(rhchain.top_pools(), 2.5)
        except Exception:
            return []
        q = (current or "").strip().lower()
        tokens = [t for t in rhchain.aggregate(pools) if t.symbol.upper() not in rhchain.CHAIN_MAJORS]
        tokens.sort(key=lambda t: -t.volume("h24"))
        choices = []
        for t in tokens:
            if q and q not in t.symbol.lower() and q not in t.address.lower():
                continue
            label = f"{t.symbol}{SEP}{usd_str(t.volume('h24'))} 24h vol"
            if t.mc_usd:
                label += f"{SEP}{usd_str(t.mc_usd)} MC"
            choices.append(app_commands.Choice(name=label[:100], value=chain.to_checksum(t.address)))
            if len(choices) == 25:
                break
        return choices

    # ---------------- helpers ----------------
    # Thin delegations to rhc/trade.py, kept so callers and tests have one
    # stable place to patch.

    async def _usd_basis(self, rt: kyber.Route, amount_wei: int,
                         eth_usd: Optional[float] = None) -> Tuple[Optional[float], str]:
        return await trade.usd_basis(rt, amount_wei, eth_usd)

    @staticmethod
    async def _reply(inter: discord.Interaction, text: str, private: bool = False,
                     view: Optional[discord.ui.View] = None) -> None:
        """Deliver a trade result, publicly when configured, and even if the
        followup fails: the tx hash must reach the user. A view (the receipt's
        buttons) rides along when the followup works; the DM fallback drops it."""
        priv = private
        if not priv:
            text = f"**{inter.user.display_name}**{SEP}{text}"
        kw = {"ephemeral": priv, "suppress_embeds": True}
        if view is not None:
            kw["view"] = view
        try:
            await inter.followup.send(text, **kw)
        except Exception:
            log.exception("Followup failed for user %s; falling back to DM", inter.user.id)
            try:
                await inter.user.send(text, suppress_embeds=True)
            except Exception:
                log.exception("DM fallback failed for user %s; result was: %s", inter.user.id, text)

    @staticmethod
    def _refund(user_id: int, usd: float) -> None:
        trade.refund(user_id, usd)

    async def _round_trip(self, token: str, amount_out: int) -> Tuple[Optional[kyber.Route], bool]:
        return await trade.round_trip(token, amount_out)

    @staticmethod
    def _quote_text(rt: kyber.Route, addr: str, sym: str, dec: int, back: Optional[kyber.Route],
                    unavailable: bool, liq: Optional[float] = None) -> str:
        return trade.quote_text(rt, addr, sym, dec, back, unavailable, liq)

    @staticmethod
    def _describe(res: swap.SwapResult, success: str) -> str:
        return trade.describe(res, success)


async def setup(bot: commands.Bot):
    await bot.add_cog(RhcCog(bot))
