"""/rhc: Robinhood Chain wallets, DEX trading, and the chain's trending board.

Trading (buy/sell) needs the trading flag and the allowlist; getting your own
funds OUT (export, withdraw) needs only your wallet, so a kill switch never
strands anyone. Money moves only after a confirm button bound to the caller.
With ``RHC_PUBLIC_REPLIES`` on (the default) quotes, prompts, results, balances
and the trending board post to the channel; refusals, the withdraw prompt and
the private-key export are always visible only to the user. See
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

from .. import rhchain
from ..config import (
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
from ..helpers import humanize
from ..logging_setup import log
from ..rhc import chain, guard, kyber, ledger, pnl, portfolio, swap, wallets
from ..tables import add_table_fields
from ..views import ConfirmOrder

THIN_POOL_USD = 25_000   # below this, warn that 2% slippage will often not survive the send

CUSTODY_WARNING = (
    "**Read this once.** McCap holds this wallet's key, encrypted, on its server. Whoever runs the "
    "server can control it. Keep only what you are actively trading here, withdraw profits, and "
    "treat it like cash in a friend's drawer, not a bank."
)

# Visibility of quotes, prompts, results and balances. Refusals, the withdraw
# prompt and the key export never use this: they are always private.
PRIVATE = not RHC_PUBLIC_REPLIES


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


def _fmt_usd(v: Optional[float]) -> str:
    return f"${v:,.2f}" if v is not None else "—"


def _short_addr(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}"


class RhcCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await wallets.load()
        swap.restore_pending()
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

    # ---------------- gates ----------------

    @staticmethod
    async def _send_private(inter: discord.Interaction, text: str) -> None:
        """An always-private message, whether or not the interaction was deferred yet."""
        if inter.response.is_done():
            await inter.followup.send(text, ephemeral=True)
        else:
            await inter.response.send_message(text, ephemeral=True)

    async def _deny_trade(self, inter: discord.Interaction) -> bool:
        """Reply and return True when the caller may not trade here.

        Allowlist first, so a stranger learns nothing about the vault's state.
        """
        if not allowed(inter.user.id):
            log.warning("Rejected /rhc trade command from non-allowlisted user %s", inter.user.id)
            await self._send_private(inter, "🔒 You are not on the trader allowlist.")
            return True
        if not guild_ok(inter.guild_id):
            await self._send_private(inter, "🔒 Trading commands are not enabled here.")
            return True
        reason = _gate()
        if reason:
            await self._send_private(inter, f"🔒 {reason}")
            return True
        return False

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
        pools = await rhchain.top_pools()
        matches = [t for t in rhchain.aggregate(pools) if t.symbol.upper() == q.upper()]
        if len(matches) == 1:
            addr = matches[0].address
            sym, dec = await chain.erc20_meta(addr)
            return chain.to_checksum(addr), sym or matches[0].symbol, dec
        if len(matches) > 1:
            raise ValueError(f"Several tokens use the symbol {q}; pass the contract address instead.")
        raise ValueError(f"Unknown token {q!r}. Pass a contract address (see /rhc trending for the busy ones).")

    async def _eth_usd(self) -> Optional[float]:
        try:
            s = await token_summary(chain.WETH)
            return (s or {}).get("price")
        except Exception:
            return None

    # ---------------- groups ----------------

    # Guild-installed only (the bot posts results into channels), usable from a
    # server or a DM with the bot. Declared here so tree defaults cannot change it.
    rhc = app_commands.Group(
        name="rhc", description="Robinhood Chain: your wallet, DEX trades, and what's trending",
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
            f"✅ {inter.user.display_name}'s Robinhood Chain wallet:\n`{w.address}`\n"
            f"Fund it by withdrawing **ETH on Robinhood Chain** from the Robinhood app to that address. "
            f"Gas is paid in ETH.\n\n{CUSTODY_WARNING}\n\n"
            f"[Explorer]({chain.explorer_address(w.address)})",
            ephemeral=priv, suppress_embeds=True,
        )

    @wallet.command(name="show", description="Your address and ETH balance")
    @app_commands.describe(private="Reply only to you")
    async def wallet_show(self, inter: discord.Interaction, private: bool = False):
        priv = PRIVATE or private
        w = wallets.get(inter.user.id)
        if w is None:
            problem = _vault_problem() if not wallets.loaded() else None
            await self._send_private(inter, problem or "You have no wallet yet. `/rhc wallet create` makes one.")
            return
        await inter.response.defer(thinking=True, ephemeral=priv)
        try:
            bal = await chain.native_balance(w.address)
        except chain.ChainError:
            bal = None
        eth_usd = await self._eth_usd()
        eth = chain.fmt_units(bal, 18) if bal is not None else "unavailable (RPC)"
        usd = f" (≈ ${bal / 1e18 * eth_usd:,.2f})" if (bal is not None and eth_usd) else ""
        await inter.followup.send(
            f"**{inter.user.display_name}** · `{w.address}`\n**{eth} ETH**{usd}\n"
            f"Daily cap: ${ledger.remaining(inter.user.id):,.2f} of ${RHC_MAX_DAILY_USD:,.2f} left today "
            f"(${RHC_MAX_TRADE_USD:,.2f} per buy)\n[Explorer]({chain.explorer_address(w.address)})",
            ephemeral=priv, suppress_embeds=True,
        )

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
                f"🚫 Not enough ETH: you have {chain.fmt_units(bal, 18)} ETH; gas needs about {chain.fmt_units(fee, 18)} ETH, "
                f"so the most you can send is **{chain.fmt_units(max_send, 18)} ETH**.", ephemeral=True,
            )
            return
        eth_usd = await self._eth_usd()
        usd = f" (≈ ${amount / 1e18 * eth_usd:,.2f})" if eth_usd else ""
        view = ConfirmOrder(inter.user.id, RHC_CONFIRM_TIMEOUT)
        await inter.followup.send(
            f"Send **{chain.fmt_units(amount, 18)} ETH**{usd} on Robinhood Chain to `{chain.to_checksum(to)}`?{warning}\n"
            f"Gas ≈ {chain.fmt_units(fee, 18)} ETH. Transfers cannot be reversed.", view=view, ephemeral=True,
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
        await self._reply(inter, self._describe(res, f"Sent {chain.fmt_units(amount, 18)} ETH"), True)

    # ---------------- trades ----------------

    @rhc.command(name="buy", description="Buy a token with ETH or dollars from your wallet (shows the quote, asks to confirm)")
    @app_commands.describe(
        token="Contract address or a symbol from /rhc trending",
        eth="ETH to spend, e.g. 0.01 (or use usd)",
        usd="Dollars to spend instead of ETH, e.g. 5",
        slippage_bps="Max slippage in basis points (default 200 = 2%)", private="Reply only to you",
    )
    async def buy(self, inter: discord.Interaction, token: str, eth: Optional[str] = None,
                  usd: Optional[float] = None, slippage_bps: Optional[int] = None, private: bool = False):
        priv = PRIVATE or private
        if await self._deny_trade(inter):
            return
        w = wallets.get(inter.user.id)
        if w is None:
            await self._send_private(inter, "You have no wallet yet. `/rhc wallet create` makes one.")
            return
        if (eth is None) == (usd is None):
            await self._send_private(inter, "Give either `eth` or `usd`, e.g. `/rhc buy PONS eth:0.01` or `/rhc buy PONS usd:5`.")
            return
        await inter.response.defer(thinking=True, ephemeral=priv)
        bps = clamp_slippage(slippage_bps)
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
            rt = await kyber.route(chain.NATIVE, addr, amount)
        except (ValueError, kyber.KyberError, chain.ChainError) as e:
            await inter.followup.send(f"❌ {e}", ephemeral=priv)
            return

        usd, why = await self._usd_basis(rt, amount, eth_usd)
        if usd is None:
            await inter.followup.send(f"🚫 {why}", ephemeral=priv)
            return
        ok, why = ledger.check(inter.user.id, usd)
        if not ok:
            await inter.followup.send(f"🚫 {why}", ephemeral=priv)
            return
        # Enough ETH for the trade AND its gas, said with numbers, before the
        # confirm prompt rather than after a reservation.
        try:
            bal = await chain.native_balance(w.address)
            gas_price = await chain.gas_price()
        except chain.ChainError:
            await inter.followup.send("❌ Could not read your balance on Robinhood Chain. Try again.", ephemeral=priv)
            return
        need = amount + (rt.gas * (100 + swap.GAS_BUFFER_PCT) // 100) * gas_price * swap.FEE_MULTIPLIER
        if bal < need:
            await inter.followup.send(
                f"🚫 Not enough ETH: you have {chain.fmt_units(bal, 18)} ETH and this needs about "
                f"{chain.fmt_units(need, 18)} ETH including gas. Fund the wallet or size down.", ephemeral=priv,
            )
            return
        back, unavailable = await self._round_trip(addr, rt.amount_out)
        if unavailable:
            await inter.followup.send("❌ KyberSwap is not answering right now, so the sell-back check cannot run. "
                                      "Try again in a minute.", ephemeral=priv)
            return
        if back is None:
            await inter.followup.send(
                f"🚫 **{sym}** cannot be sold back for ETH right now (no route). That is what a honeypot looks "
                f"like; refusing to buy.", ephemeral=priv,
            )
            return

        # Liquidity tells the user whether the default slippage will survive
        # the few seconds between quote and send.
        liq_line = ""
        info = None
        try:
            info = await token_summary(addr)
            liq = (info or {}).get("liq")
            if liq is not None:
                liq_line = f"\nLiquidity ${humanize(liq)}"
                if liq < THIN_POOL_USD and bps < 500:
                    liq_line += (f" · **thin pool**: {bps / 100:.0f}% slippage often fails on these; "
                                 f"re-run with `slippage_bps:500` if it does")
        except Exception:
            pass

        view = ConfirmOrder(inter.user.id, RHC_CONFIRM_TIMEOUT)
        text = f"**{inter.user.display_name}** is buying\n" + self._quote_text(rt, addr, sym, dec, back, False) + liq_line
        text += (f"\nSlippage **{bps / 100:.2f}%** · daily cap: ${max(0.0, ledger.remaining(inter.user.id) - usd):,.2f} "
                 f"of ${RHC_MAX_DAILY_USD:,.2f} left after this\n"
                 f"Real swap from `{_short_addr(w.address)}`. Only {inter.user.display_name} can confirm; "
                 f"expires in {RHC_CONFIRM_TIMEOUT}s.")
        await inter.followup.send(text, view=view, ephemeral=priv)
        await view.wait()
        if not view.value:
            await inter.followup.send("⏲️ Expired, nothing was bought. Run it again and press Confirm."
                                      if view.value is None else "Cancelled, nothing was bought.", ephemeral=True)
            return

        # The confirmed numbers are the deal. Always re-quote after the click (a
        # route is good for ~10s and the button sat for longer than that) and
        # refuse if the fresh output is below the floor the user confirmed; the
        # price moved, they get to decide again.
        confirmed_floor = kyber.min_out(rt.amount_out, bps)
        try:
            rt = await kyber.route(chain.NATIVE, addr, amount)
        except kyber.KyberError as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return
        if rt.amount_out < confirmed_floor:
            await inter.followup.send(
                f"🚫 The price moved while you were confirming: {chain.fmt_units(rt.amount_out, dec)} {sym} now "
                f"vs the {chain.fmt_units(confirmed_floor, dec)} floor you confirmed. Nothing was bought; "
                f"run the command again.", ephemeral=True,
            )
            return
        # Reserve the spend BEFORE the swap so two confirms cannot both pass the
        # cap. The ETH price was fetched before the prompt; no round trip here.
        usd, why = await self._usd_basis(rt, amount, eth_usd)
        if usd is None:
            await inter.followup.send(f"🚫 {why}", ephemeral=True)
            return
        ok, why = ledger.check(inter.user.id, usd)
        if not ok:
            await inter.followup.send(f"🚫 {why}", ephemeral=True)
            return
        try:
            ledger.record(inter.user.id, usd)
        except Exception:
            log.exception("Ledger write failed for user %s", inter.user.id)
            await inter.followup.send("❌ The spend ledger could not be written; refusing to trade.", ephemeral=True)
            return
        # Journaled with the trade so holdings and profit can be built from it.
        extra = {"decimals": dec, "eth_usd": eth_usd,
                 "mc_usd": (info or {}).get("mc"), "price_usd": (info or {}).get("price")}
        try:
            built = await kyber.build(rt, w.address, bps)
            built.min_out = max(built.min_out, confirmed_floor)
            res = await swap.execute(inter.user.id, built, addr, sym, extra)
            # A thin pool moves in the seconds between quote and send. When the
            # pre-flight simulation says the slippage floor would not be met,
            # nothing was sent, so one fresh quote and retry is free. The
            # confirmed floor still applies: the user never gets less than they
            # agreed to.
            if not res.ok and not res.pending and guard.is_slippage_revert(res.error):
                rt2 = await kyber.route(chain.NATIVE, addr, amount)
                if rt2.amount_out >= confirmed_floor:
                    built = await kyber.build(rt2, w.address, bps)
                    built.min_out = max(built.min_out, confirmed_floor)
                    res = await swap.execute(inter.user.id, built, addr, sym, extra)
        except Exception as e:  # noqa: BLE001
            log.exception("Buy failed for user %s", inter.user.id)
            self._refund(inter.user.id, usd)
            msg = str(e) if isinstance(e, kyber.KyberError) else "internal error; nothing should have been sent, but check the explorer"
            await self._reply(inter, f"❌ {msg}", priv)
            return
        if not res.ok and not res.pending:
            self._refund(inter.user.id, usd)
        got = (f"{chain.fmt_units(res.amount_out, dec)} {sym}" if res.amount_out is not None
               else f"~{chain.fmt_units(built.amount_out, dec)} {sym} (quoted)")
        spent = f"{chain.fmt_units(amount, 18)} ETH ({pnl.fmt_usd(built.amount_in_usd)})"
        mc_note = f" at ${humanize((info or {}).get('mc'))} MC" if (info or {}).get("mc") else ""
        await self._reply(inter, self._describe(res, f"Bought {got} for {spent}{mc_note}"), priv)

    @rhc.command(name="sell", description="Sell a percentage of a token you hold for ETH (asks to confirm)")
    @app_commands.describe(
        token="Contract address or a symbol from /rhc trending", percent="1 to 100",
        slippage_bps="Max slippage in basis points (default 200 = 2%)", private="Reply only to you",
    )
    async def sell(self, inter: discord.Interaction, token: str, percent: int, slippage_bps: Optional[int] = None,
                   private: bool = False):
        priv = PRIVATE or private
        if await self._deny_trade(inter):
            return
        w = wallets.get(inter.user.id)
        if w is None:
            await self._send_private(inter, "You have no wallet yet.")
            return
        await inter.response.defer(thinking=True, ephemeral=priv)
        pct = max(1, min(int(percent), 100))
        bps = clamp_slippage(slippage_bps)
        try:
            addr, sym, dec = await self._resolve_token(token)
            have = await chain.erc20_balance(addr, w.address)
            if have <= 0:
                await inter.followup.send(f"You hold no {sym}.", ephemeral=priv)
                return
            amount = have * pct // 100
            rt = await kyber.route(addr, chain.NATIVE, amount)
        except (ValueError, kyber.KyberError, chain.ChainError) as e:
            await inter.followup.send(f"❌ {e}", ephemeral=priv)
            return

        view = ConfirmOrder(inter.user.id, RHC_CONFIRM_TIMEOUT)
        await inter.followup.send(
            f"**{inter.user.display_name}** is selling **{chain.fmt_units(amount, dec)} {sym}** "
            f"({pct}% of {chain.fmt_units(have, dec)}) for **≈ {chain.fmt_units(rt.amount_out, 18)} ETH** "
            f"({_fmt_usd(rt.amount_out_usd)})\nToken `{addr}`\n"
            f"Route: {', '.join(rt.hops) or '?'} · gas ≈ {_fmt_usd(rt.gas_usd)} · slippage {bps / 100:.2f}%\n"
            f"Real swap. Only {inter.user.display_name} can confirm; expires in {RHC_CONFIRM_TIMEOUT}s.",
            view=view, ephemeral=priv,
        )
        await view.wait()
        if not view.value:
            await inter.followup.send("⏲️ Expired, nothing was sold. Run it again and press Confirm."
                                      if view.value is None else "Cancelled, nothing was sold.", ephemeral=True)
            return
        confirmed_floor = kyber.min_out(rt.amount_out, bps)
        # Where this token stands against the entry: the multiple people quote
        # ("bought at 50K, sold at 200K, 4x") comes from the journal's entry MC
        # and the market cap right now.
        info = None
        try:
            info = await token_summary(addr)
        except Exception:
            pass
        entry = pnl.entry_for(inter.user.id, addr)
        mc_now = (info or {}).get("mc")
        multiple = (mc_now / entry.entry_mc) if (entry and entry.entry_mc and mc_now) else None
        extra = {"decimals": dec, "mc_usd": mc_now, "price_usd": (info or {}).get("price"),
                 "entry_mc": (entry.entry_mc if entry else None), "multiple": multiple}
        try:
            rt = await kyber.route(addr, chain.NATIVE, amount)   # always fresh after the click
            if rt.amount_out < confirmed_floor:
                await inter.followup.send(
                    f"🚫 The price moved while you were confirming: {chain.fmt_units(rt.amount_out, 18)} ETH now vs "
                    f"the {chain.fmt_units(confirmed_floor, 18)} floor you confirmed. Nothing was sold; run it again.",
                    ephemeral=True,
                )
                return
            built = await kyber.build(rt, w.address, bps)
            built.min_out = max(built.min_out, confirmed_floor)
            res = await swap.execute(inter.user.id, built, addr, sym, extra)
        except kyber.KyberError as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return
        except Exception:
            log.exception("Sell failed for user %s", inter.user.id)
            await inter.followup.send("❌ Internal error during the sell. Check your balances and the explorer "
                                      "before retrying.", ephemeral=True)
            return
        got = (f"{chain.fmt_units(res.amount_out, 18)} ETH" if res.amount_out is not None
               else f"~{chain.fmt_units(built.amount_out, 18)} ETH (quoted)")
        got += f" ({pnl.fmt_usd(built.amount_out_usd)})"
        tail = ""
        if res.ok and multiple is not None and entry is not None:
            tail = (f" · **{pnl.fmt_x(multiple)}** from your entry (${humanize(entry.entry_mc)} → "
                    f"${humanize(mc_now)} MC)")
        await self._reply(inter, self._describe(res, f"Sold {chain.fmt_units(amount, dec)} {sym} for {got}{tail}"), priv)

    @rhc.command(name="holdings", description="What your wallet holds: cost, worth now, multiple, total")
    @app_commands.describe(private="Reply only to you")
    async def holdings(self, inter: discord.Interaction, private: bool = False):
        priv = PRIVATE or private
        w = wallets.get(inter.user.id)
        if w is None:
            await self._send_private(inter, "You have no wallet yet.")
            return
        await inter.response.defer(thinking=True, ephemeral=priv)
        u = await pnl.user_pnl(inter.user.id, w.address)
        embed = discord.Embed(
            title=f"{inter.user.display_name}'s Robinhood Chain wallet",
            colour=0x57F287 if u.pnl_usd >= 0 else 0xED4245,
            description=(
                f"`{w.address}`\n"
                f"**{chain.fmt_units(u.eth_wei, 18)} ETH** ({pnl.fmt_usd(u.eth_value_usd)}) · "
                f"tokens {pnl.fmt_usd(u.tokens_worth_usd)} · **total {pnl.fmt_usd(u.total_usd)}**"
            ),
        )
        rows = [[
            p.symbol[:10],
            pnl.fmt_amount(p.balance),
            pnl.fmt_usd(p.open_cost_usd),
            pnl.fmt_usd(p.worth_usd),
            pnl.fmt_usd(p.unrealized_usd, signed=True),
            pnl.fmt_x(p.multiple_now),
        ] for p in u.open_positions]
        if rows:
            add_table_fields(embed, "Open positions", ["Token", "Amount", "Cost", "Worth", "PnL", "x"], rows,
                             ["l", "r", "r", "r", "r", "r"], max_fields=3)
            entries = [f"{p.symbol}: bought at ${humanize(p.entry_mc)} MC, now ${humanize(p.mc_now)}"
                       for p in u.open_positions if p.entry_mc and p.mc_now][:6]
            if entries:
                embed.add_field(name="Entry → now (market cap)", value="\n".join(entries), inline=False)
        else:
            embed.add_field(name="Open positions", value="None. `/rhc buy` to open one.", inline=False)
        embed.add_field(
            name="Profit",
            value=(f"Unrealized {pnl.fmt_usd(u.unrealized_usd, signed=True)} · realized {pnl.fmt_usd(u.realized_usd, signed=True)} · "
                   f"gas {pnl.fmt_usd(u.gas_usd)} ({u.gas_eth:.5f} ETH) · **net {pnl.fmt_usd(u.pnl_usd, signed=True)}**"),
            inline=False,
        )
        embed.set_footer(text="Cost and profit from your McCap trades; prices from KyberSwap at trade time and DexScreener now. /rhc pnl for the chart.")
        await inter.followup.send(embed=embed, ephemeral=priv)

    @rhc.command(name="history", description="Your recent trades and withdrawals")
    @app_commands.describe(count="How many to show (default 10, max 25)", private="Reply only to you")
    async def history(self, inter: discord.Interaction, count: Optional[int] = 10, private: bool = False):
        priv = PRIVATE or private
        w = wallets.get(inter.user.id)
        if w is None:
            await self._send_private(inter, "You have no wallet yet.")
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
            when = time.strftime("%m-%d %H:%M", time.gmtime(float(e.get("ts") or 0)))
            icon = icons.get(e.get("final_status"), "•")
            dec = int(e.get("decimals") or 18)
            tx = e.get("tx") or ""
            link = f" [tx]({chain.explorer_tx(tx)})" if tx else ""
            if e.get("kind") == "buy":
                got = e.get("actual_out_estimate") or e.get("quoted_out") or "0"
                mc = f" at ${humanize(float(e['mc_usd']))} MC" if e.get("mc_usd") else ""
                lines.append(f"{icon} `{when}` BUY {chain.fmt_units(int(float(e.get('amount_in') or 0)), 18)} ETH → "
                             f"{chain.fmt_units(int(float(got)), dec)} {e.get('symbol')} ({pnl.fmt_usd(pnl._f(e.get('usd_in')))}{mc}){link}")
            elif e.get("kind") == "sell":
                out = e.get("actual_out_estimate") or e.get("quoted_out") or "0"
                mult = f" · {pnl.fmt_x(float(e['multiple']))}" if e.get("multiple") else ""
                lines.append(f"{icon} `{when}` SELL {chain.fmt_units(int(float(e.get('amount_in') or 0)), dec)} {e.get('symbol')} → "
                             f"{chain.fmt_units(int(float(out)), 18)} ETH ({pnl.fmt_usd(pnl._f(e.get('usd_out')))}{mult}){link}")
            else:
                lines.append(f"{icon} `{when}` WITHDRAW {chain.fmt_units(int(float(e.get('amount') or 0)), 18)} ETH → "
                             f"`{_short_addr(e.get('to') or '')}`{link}")
        embed = discord.Embed(title=f"{inter.user.display_name}'s last {len(rows)} transaction(s)",
                              colour=0x2B90D9, description="\n".join(lines)[:4000])
        await inter.followup.send(embed=embed, ephemeral=priv)

    @rhc.command(name="pnl", description="Your profit per token, gas spent, and a chart")
    @app_commands.describe(private="Reply only to you")
    async def pnl_cmd(self, inter: discord.Interaction, private: bool = False):
        priv = PRIVATE or private
        w = wallets.get(inter.user.id)
        if w is None:
            await self._send_private(inter, "You have no wallet yet.")
            return
        await inter.response.defer(thinking=True, ephemeral=priv)
        u = await pnl.user_pnl(inter.user.id, w.address)
        if not u.positions:
            await inter.followup.send(f"**{inter.user.display_name}** has no trades yet.", ephemeral=priv)
            return
        rows = [[
            p.symbol[:10],
            pnl.fmt_usd(p.cost_usd),
            pnl.fmt_usd(p.realized_usd, signed=True),
            pnl.fmt_usd(p.unrealized_usd, signed=True),
            pnl.fmt_usd(p.pnl_usd, signed=True),
            pnl.fmt_x(p.multiple_now),
        ] for p in u.positions]
        embed = discord.Embed(
            title=f"{inter.user.display_name}'s profit",
            colour=0x57F287 if u.pnl_usd >= 0 else 0xED4245,
            description=(f"Bought {pnl.fmt_usd(u.cost_usd)} of tokens · realized {pnl.fmt_usd(u.realized_usd, signed=True)} · "
                         f"unrealized {pnl.fmt_usd(u.unrealized_usd, signed=True)} · gas {pnl.fmt_usd(u.gas_usd)} "
                         f"({u.gas_eth:.5f} ETH)\n**Net {pnl.fmt_usd(u.pnl_usd, signed=True)}**"),
        )
        add_table_fields(embed, "By token", ["Token", "Cost", "Realized", "Unreal.", "Net", "x"], rows,
                         ["l", "r", "r", "r", "r", "r"], max_fields=3)
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
        gas_usd = f" ({pnl.fmt_usd(g.gas_wei / 1e18 * eth_usd)})" if eth_usd else ""
        best = f"{pnl.fmt_x(g.best_multiple)} on {g.best_symbol}" if g.best_multiple else "none yet"
        summary = await portfolio.summary()
        embed = discord.Embed(
            title="Robinhood Chain · group stats",
            colour=0x2B90D9,
            description=(
                f"**{g.wallets}** wallet(s), **{g.traders}** with trades · holding "
                f"{summary.eth:.4f} ETH{(' + ' + str(len(summary.positions)) + ' token position(s)') if summary.positions else ''}"
                f" ≈ {pnl.fmt_usd(summary.total_usd)}\n"
                f"**{g.buys}** buys, **{g.sells}** sells · volume {pnl.fmt_usd(g.volume_usd)}\n"
                f"Gas spent **{g.gas_wei / 1e18:.5f} ETH**{gas_usd}\n"
                f"Realized profit {pnl.fmt_usd(g.realized_usd, signed=True)} · best sell {best}"
            ),
        )
        await inter.followup.send(embed=embed, ephemeral=priv)

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
        """Addresses are what /rhc buy needs, and a table cell is not copyable."""
        lines = [f"{t.symbol[:10]:<10} {t.address}" for t in tokens[:15]]
        if lines:
            embed.add_field(name="Addresses (for /rhc buy and /mc)", value="```\n" + "\n".join(lines) + "\n```", inline=False)
        links = " · ".join(f"[{t.symbol[:10]}]({t.deepest.url()})" for t in tokens[:10])
        if links:
            embed.add_field(name="Charts", value=links[:1024], inline=False)

    @rhc.command(name="trending", description="Busiest and fastest-moving tokens on the Robinhood chain")
    @app_commands.describe(
        window="Volume and market-cap change over 5m to 24h (default 24h)",
        sort="volume (default), gainers, losers, or newest pools",
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
        label = rhchain.WINDOW_LABELS[w]

        pools = await rhchain.top_pools()
        if not pools:
            await inter.followup.send("Couldn't reach GeckoTerminal for Robinhood chain pools. Try again shortly.")
            return
        tokens = rhchain.aggregate(pools)
        top = rhchain.rank(tokens, w, mode, include_majors=include_majors, n=n)
        if not top:
            await inter.followup.send("Nothing to show for that filter.")
            return

        def pct(v: Optional[float]) -> str:
            return f"{v:+.1f}%" if v is not None else "—"

        def mc(v: Optional[float]) -> str:
            return f"${humanize(v)}" if v else "—"

        # "MC then → now" is the number people want for a mover: $800K to $1.1M
        # inside the window, not just a percentage.
        rows = [[
            t.symbol[:10],
            f"${humanize(t.volume(w))}",
            pct(t.change(w)),
            mc(t.mc_before(w)),
            mc(t.mc_usd),
            f"${humanize(t.liq_usd)}",
        ] for t in top]

        shown_tokens = [t for t in tokens if include_majors or t.symbol.upper() not in rhchain.CHAIN_MAJORS]
        total_vol = sum(t.volume(w) for t in shown_tokens)
        venues = sorted({p.dex for t in shown_tokens for p in t.pools})
        titles = {"volume": f"top by {label} volume", "gainers": f"{label} gainers",
                  "losers": f"{label} losers", "new": "newest of the busy pools"}
        embed = discord.Embed(
            title=f"Robinhood chain · {titles[mode]}",
            colour=0x2ECC71 if mode != "losers" else 0xE74C3C,
            description=(
                f"{len(shown_tokens)} token(s) in the {len(pools)} busiest pools · "
                f"**${humanize(total_vol)}** traded in {label} · "
                f"{', '.join(venues) if venues else 'no venues'}"
            ),
        )
        shown, total = add_table_fields(
            embed, f"Volume, market cap {label} ago → now, liquidity",
            ["Token", f"Vol {label}", f"Δ {label}", f"MC {label} ago", "MC now", "Liq"], rows,
            ["l", "r", "r", "r", "r", "r"], max_fields=3,
        )
        self._addresses_field(embed, top)
        foot = "GeckoTerminal · Robinhood chain DEX pools · /rhc new for brand-new pairs"
        foot += "" if include_majors else " · WETH/USDG/stables hidden (include_majors to show)"
        if shown < total:
            foot += f" · {total - shown} row(s) not shown"
        embed.set_footer(text=foot)
        await inter.followup.send(embed=embed, ephemeral=priv)

    @rhc.command(name="new", description="Brand-new pairs on the Robinhood chain, newest first")
    @app_commands.describe(
        count="How many to show (default 10, max 25)",
        min_liquidity="Hide pools with less than this many dollars of liquidity (default 1000)",
        private="Reply only to you",
    )
    async def new(self, inter: discord.Interaction, count: Optional[int] = 10, min_liquidity: Optional[int] = 1000,
                  private: bool = False):
        priv = PRIVATE or private
        await inter.response.defer(thinking=True, ephemeral=priv)
        n = max(1, min(int(count or 10), 25))
        floor = max(0, int(min_liquidity if min_liquidity is not None else 1000))
        pools = await rhchain.new_pools()
        if not pools:
            await inter.followup.send("Couldn't reach GeckoTerminal for new Robinhood chain pools. Try again shortly.")
            return
        tokens = rhchain.aggregate(pools)
        tokens = [t for t in tokens if t.symbol.upper() not in rhchain.CHAIN_MAJORS]
        kept = [t for t in tokens if t.liq_usd >= floor]
        kept.sort(key=lambda t: -t.created_ts)
        top = kept[:n]
        if not top:
            await inter.followup.send(f"No new pairs with at least ${floor:,} of liquidity right now "
                                      f"({len(tokens)} seen). Lower min_liquidity to see the dust.")
            return

        def pct(v: Optional[float]) -> str:
            return f"{v:+.1f}%" if v is not None else "—"

        rows = [[
            t.symbol[:10],
            rhchain.age_str(t.created_ts),
            f"${humanize(t.liq_usd)}",
            f"${humanize(t.volume('h1'))}",
            pct(t.change("h1")),
            f"${humanize(t.mc_usd)}" if t.mc_usd else "—",
        ] for t in top]
        embed = discord.Embed(
            title="Robinhood chain · newest pairs",
            colour=0x3498DB,
            description=(f"{len(kept)} of the {len(tokens)} newest pools have ≥ ${floor:,} liquidity · "
                         f"oldest shown {rhchain.age_str(top[-1].created_ts)} · most are dust: check the sell-back "
                         f"line in /rhc buy before touching one"),
        )
        shown, total = add_table_fields(
            embed, "Age, liquidity, 1h volume, 1h change, market cap",
            ["Token", "Age", "Liq", "Vol 1h", "Δ 1h", "MC"], rows,
            ["l", "r", "r", "r", "r", "r"], max_fields=3,
        )
        self._addresses_field(embed, top)
        foot = "GeckoTerminal new-pools feed · WETH/USDG/stables hidden"
        if shown < total:
            foot += f" · {total - shown} row(s) not shown"
        embed.set_footer(text=foot)
        await inter.followup.send(embed=embed, ephemeral=priv)

    # ---------------- helpers ----------------

    async def _usd_basis(self, rt: kyber.Route, amount_wei: int,
                         eth_usd: Optional[float] = None) -> Tuple[Optional[float], str]:
        """Dollar size of an ETH-in trade for the caps: the LARGER of Kyber's
        amountInUsd and amount × DexScreener's ETH price, so a single wrong feed
        cannot shrink a trade under the cap. (None, reason) when neither prices it.
        Pass ``eth_usd`` when it was already fetched; the post-confirm path must
        not spend a network round trip while a fresh quote is aging."""
        if eth_usd is None:
            eth_usd = await self._eth_usd()
        alt = amount_wei / 1e18 * eth_usd if eth_usd else None
        kyber_usd = rt.amount_in_usd if rt.amount_in_usd > 0 else None
        if kyber_usd and alt:
            if abs(alt - kyber_usd) / max(alt, kyber_usd) > 0.2:
                log.warning("USD disagreement for %s wei: Kyber $%.2f vs DexScreener $%.2f", amount_wei, kyber_usd, alt)
            return max(kyber_usd, alt), ""
        if kyber_usd or alt:
            return kyber_usd or alt, ""
        return None, ("Could not price this trade in USD (neither KyberSwap nor DexScreener gave a figure), "
                      "so the caps cannot be checked. Refusing.")

    @staticmethod
    async def _reply(inter: discord.Interaction, text: str, private: bool = False) -> None:
        """Deliver a trade result, publicly when configured, and even if the
        followup fails: the tx hash must reach the user."""
        priv = private
        if not priv:
            text = f"**{inter.user.display_name}** · {text}"
        try:
            await inter.followup.send(text, ephemeral=priv, suppress_embeds=True)
        except Exception:
            log.exception("Followup failed for user %s; falling back to DM", inter.user.id)
            try:
                await inter.user.send(text, suppress_embeds=True)
            except Exception:
                log.exception("DM fallback failed for user %s; result was: %s", inter.user.id, text)

    @staticmethod
    def _refund(user_id: int, usd: float) -> None:
        try:
            ledger.refund(user_id, usd)
        except Exception:
            log.exception("Ledger refund failed for user %s", user_id)

    async def _round_trip(self, token: str, amount_out: int) -> Tuple[Optional[kyber.Route], bool]:
        """(sell-back route or None, kyber_unavailable).

        None with unavailable=False means Kyber says there is no way back: honeypot
        territory. None with unavailable=True means we simply could not ask.
        """
        try:
            return await kyber.route(token, chain.NATIVE, amount_out), False
        except kyber.NoRoute:
            return None, False
        except kyber.KyberError:
            return None, True

    @staticmethod
    def _quote_text(rt: kyber.Route, addr: str, sym: str, dec: int, back: Optional[kyber.Route],
                    unavailable: bool) -> str:
        impact = rt.price_impact_pct
        lines = [
            f"**{chain.fmt_units(rt.amount_in, 18)} ETH** ({_fmt_usd(rt.amount_in_usd)}) → "
            f"**{chain.fmt_units(rt.amount_out, dec)} {sym}** ({_fmt_usd(rt.amount_out_usd)})",
            f"Token `{addr}`",
            f"Route: {', '.join(rt.hops) or '?'} · gas ≈ {_fmt_usd(rt.gas_usd)}"
            + (f" · impact {impact:+.2f}%" if impact is not None else ""),
        ]
        if unavailable:
            lines.append("⚠️ Could not check the sell route back to ETH (KyberSwap did not answer).")
        elif back is None:
            lines.append("⚠️ **No sell route back to ETH.** Honeypot until proven otherwise.")
        elif rt.amount_in > 0:
            rt_pct = (back.amount_out / rt.amount_in - 1.0) * 100.0
            flag = "⚠️ " if rt_pct < -25 else ""
            lines.append(f"{flag}Sell straight back: {chain.fmt_units(back.amount_out, 18)} ETH ({rt_pct:+.1f}% round trip)")
        return "\n".join(lines)

    @staticmethod
    def _describe(res: swap.SwapResult, success: str) -> str:
        if res.ok:
            gas = f" · gas {chain.fmt_units(res.gas_cost_wei, 18)} ETH" if res.gas_cost_wei else ""
            return f"✅ {success}{gas}\n[Transaction]({res.explorer})"
        if res.pending:
            return f"⏳ {res.error}\n[Transaction]({res.explorer})"
        link = f"\n[Transaction]({res.explorer})" if res.tx else ""
        return f"❌ {res.error}{link}"


async def setup(bot: commands.Bot):
    await bot.add_cog(RhcCog(bot))
