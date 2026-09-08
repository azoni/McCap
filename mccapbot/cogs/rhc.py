"""/rhc: Robinhood Chain wallets and DEX trading, per Discord user.

Everything here is ephemeral. Balances, addresses and keys are the caller's
business and nobody else's. Money moves only after a confirm button bound to
the caller. Trading (buy/sell/quote) needs the trading flag and the allowlist;
getting your own funds OUT (export, withdraw) needs only your wallet, so a
kill switch never strands anyone. See ``mccapbot/rhc`` for the execution path.
"""

import os
from typing import Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from .. import rhchain
from ..config import (
    RH_OWNER_ID,
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
from ..logging_setup import log
from ..rhc import chain, kyber, ledger, swap, wallets
from .trade import ConfirmOrder

CUSTODY_WARNING = (
    "**Read this once.** McCap holds this wallet's key, encrypted, on its server. Whoever runs the "
    "server can control it. Keep only what you are actively trading here, withdraw profits, and "
    "treat it like cash in a friend's drawer, not a bank."
)

# Visibility of results (quotes, trades, addresses, balances). Confirm prompts,
# refusals and the key export never use this: they are always private.
PRIVATE = not RHC_PUBLIC_REPLIES


def _gate() -> Optional[str]:
    """Why trading is unavailable, or None if it is armed."""
    if not RHC_TRADING_ENABLE:
        return "Robinhood Chain trading is disabled (`RHC_TRADING_ENABLE=0`)."
    if not RHC_TRADER_IDS and not RH_OWNER_ID:
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
    return user_id in RHC_TRADER_IDS or (bool(RH_OWNER_ID) and user_id == RH_OWNER_ID)


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
                log.info("Robinhood Chain trading armed for %d allowlisted user(s)",
                         len(RHC_TRADER_IDS) + (1 if RH_OWNER_ID and RH_OWNER_ID not in RHC_TRADER_IDS else 0))

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
        raise ValueError(f"Unknown token {q!r}. Pass a contract address (see /rh_trending for the busy ones).")

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
        name="rhc", description="Robinhood Chain: your wallet and DEX trades",
        allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
        allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=False),
    )
    wallet = app_commands.Group(name="wallet", description="Your Robinhood Chain wallet", parent=rhc)

    # ---------------- wallet ----------------

    @wallet.command(name="create", description="Create your Robinhood Chain wallet (McCap holds the key)")
    async def wallet_create(self, inter: discord.Interaction):
        # Gates before the defer, so a refusal stays private even when results are public.
        if await self._deny_trade(inter):
            return
        await inter.response.defer(thinking=True, ephemeral=PRIVATE)
        try:
            w = await wallets.create(inter.user.id, label=inter.user.display_name)
        except wallets.VaultError as e:
            await inter.followup.send(f"❌ {e}", ephemeral=PRIVATE)
            return
        except Exception:
            log.exception("Wallet creation failed for user %s", inter.user.id)
            await inter.followup.send("❌ Wallet creation failed on the server; it's in the logs.", ephemeral=PRIVATE)
            return
        await inter.followup.send(
            f"✅ {inter.user.display_name}'s Robinhood Chain wallet:\n`{w.address}`\n"
            f"Fund it by withdrawing **ETH on Robinhood Chain** from the Robinhood app to that address. "
            f"Gas is paid in ETH.\n\n{CUSTODY_WARNING}\n\n"
            f"[Explorer]({chain.explorer_address(w.address)})",
            ephemeral=PRIVATE, suppress_embeds=True,
        )

    @wallet.command(name="show", description="Your address and ETH balance")
    async def wallet_show(self, inter: discord.Interaction):
        w = wallets.get(inter.user.id)
        if w is None:
            problem = _vault_problem() if not wallets.loaded() else None
            await self._send_private(inter, problem or "You have no wallet yet. `/rhc wallet create` makes one.")
            return
        await inter.response.defer(thinking=True, ephemeral=PRIVATE)
        try:
            bal = await chain.native_balance(w.address)
        except chain.ChainError:
            bal = None
        eth_usd = await self._eth_usd()
        eth = chain.fmt_units(bal, 18) if bal is not None else "unavailable (RPC)"
        usd = f" (≈ ${bal / 1e18 * eth_usd:,.2f})" if (bal is not None and eth_usd) else ""
        await inter.followup.send(
            f"**{inter.user.display_name}** · `{w.address}`\n**{eth} ETH**{usd}\n"
            f"Today's buy budget left: ${ledger.remaining(inter.user.id):,.2f} of ${RHC_MAX_DAILY_USD:,.2f} "
            f"(${RHC_MAX_TRADE_USD:,.2f} per trade)\n[Explorer]({chain.explorer_address(w.address)})",
            ephemeral=PRIVATE, suppress_embeds=True,
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
                                     "before retrying.")
            return
        await self._reply(inter, self._describe(res, f"Sent {chain.fmt_units(amount, 18)} ETH"))

    # ---------------- quotes and trades ----------------

    @rhc.command(name="quote", description="What an ETH amount buys of a token right now")
    @app_commands.describe(token="Contract address or a symbol from /rh_trending", eth="ETH to spend, e.g. 0.01")
    async def quote(self, inter: discord.Interaction, token: str, eth: str):
        if not allowed(inter.user.id):
            await self._send_private(inter, "🔒 You are not on the trader allowlist.")
            return
        await inter.response.defer(thinking=True, ephemeral=PRIVATE)
        try:
            addr, sym, dec = await self._resolve_token(token)
            amount = chain.to_units(eth, 18)
            rt = await kyber.route(chain.NATIVE, addr, amount)
        except (ValueError, kyber.KyberError, chain.ChainError) as e:
            await inter.followup.send(f"❌ {e}", ephemeral=PRIVATE)
            return
        back, unavailable = await self._round_trip(addr, rt.amount_out)
        await inter.followup.send(self._quote_text(rt, addr, sym, dec, back, unavailable), ephemeral=PRIVATE)

    @rhc.command(name="buy", description="Buy a token with ETH from your wallet (asks to confirm)")
    @app_commands.describe(
        token="Contract address or a symbol from /rh_trending", eth="ETH to spend, e.g. 0.01",
        slippage_bps="Max slippage in basis points (default 200 = 2%)",
    )
    async def buy(self, inter: discord.Interaction, token: str, eth: str, slippage_bps: Optional[int] = None):
        await inter.response.defer(thinking=True, ephemeral=True)
        if await self._deny_trade(inter):
            return
        w = wallets.get(inter.user.id)
        if w is None:
            await inter.followup.send("You have no wallet yet. `/rhc wallet create` makes one.", ephemeral=True)
            return
        bps = clamp_slippage(slippage_bps)
        try:
            addr, sym, dec = await self._resolve_token(token)
            amount = chain.to_units(eth, 18)
            rt = await kyber.route(chain.NATIVE, addr, amount)
        except (ValueError, kyber.KyberError, chain.ChainError) as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return

        eth_usd = await self._eth_usd()
        usd, why = await self._usd_basis(rt, amount, eth_usd)
        if usd is None:
            await inter.followup.send(f"🚫 {why}", ephemeral=True)
            return
        ok, why = ledger.check(inter.user.id, usd)
        if not ok:
            await inter.followup.send(f"🚫 {why}", ephemeral=True)
            return
        # Enough ETH for the trade AND its gas, said with numbers, before the
        # confirm prompt rather than after a reservation.
        try:
            bal = await chain.native_balance(w.address)
            gas_price = await chain.gas_price()
        except chain.ChainError:
            await inter.followup.send("❌ Could not read your balance on Robinhood Chain. Try again.", ephemeral=True)
            return
        need = amount + (rt.gas * (100 + swap.GAS_BUFFER_PCT) // 100) * gas_price * swap.FEE_MULTIPLIER
        if bal < need:
            await inter.followup.send(
                f"🚫 Not enough ETH: you have {chain.fmt_units(bal, 18)} ETH and this needs about "
                f"{chain.fmt_units(need, 18)} ETH including gas. Fund the wallet or size down.", ephemeral=True,
            )
            return
        back, unavailable = await self._round_trip(addr, rt.amount_out)
        if unavailable:
            await inter.followup.send("❌ KyberSwap is not answering right now, so the sell-back check cannot run. "
                                      "Try again in a minute.", ephemeral=True)
            return
        if back is None:
            await inter.followup.send(
                f"🚫 **{sym}** cannot be sold back for ETH right now (no route). That is what a honeypot looks "
                f"like; refusing to buy.", ephemeral=True,
            )
            return

        view = ConfirmOrder(inter.user.id, RHC_CONFIRM_TIMEOUT)
        text = self._quote_text(rt, addr, sym, dec, back, False)
        text += (f"\nSlippage **{bps / 100:.2f}%** · counts as ${usd:,.2f} against your caps · budget left after this: "
                 f"${max(0.0, ledger.remaining(inter.user.id) - usd):,.2f}\n"
                 f"This is a real swap from `{_short_addr(w.address)}`. Expires in {RHC_CONFIRM_TIMEOUT}s.")
        await inter.followup.send(text, view=view, ephemeral=True)
        await view.wait()
        if not view.value:
            await inter.followup.send("⏲️ Expired" if view.value is None else "Cancelled", ephemeral=True)
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
        try:
            built = await kyber.build(rt, w.address, bps)
            built.min_out = max(built.min_out, confirmed_floor)
            res = await swap.execute(inter.user.id, built, addr, sym)
        except Exception as e:  # noqa: BLE001
            log.exception("Buy failed for user %s", inter.user.id)
            self._refund(inter.user.id, usd)
            msg = str(e) if isinstance(e, kyber.KyberError) else "internal error; nothing should have been sent, but check the explorer"
            await self._reply(inter, f"❌ {msg}")
            return
        if not res.ok and not res.pending:
            self._refund(inter.user.id, usd)
        got = (f"{chain.fmt_units(res.amount_out, dec)} {sym}" if res.amount_out is not None
               else f"~{chain.fmt_units(built.amount_out, dec)} {sym} (quoted)")
        await self._reply(inter, self._describe(res, f"Bought {got} for {chain.fmt_units(amount, 18)} ETH"))

    @rhc.command(name="sell", description="Sell a percentage of a token you hold for ETH (asks to confirm)")
    @app_commands.describe(
        token="Contract address or a symbol from /rh_trending", percent="1 to 100",
        slippage_bps="Max slippage in basis points (default 200 = 2%)",
    )
    async def sell(self, inter: discord.Interaction, token: str, percent: int, slippage_bps: Optional[int] = None):
        await inter.response.defer(thinking=True, ephemeral=True)
        if await self._deny_trade(inter):
            return
        w = wallets.get(inter.user.id)
        if w is None:
            await inter.followup.send("You have no wallet yet.", ephemeral=True)
            return
        pct = max(1, min(int(percent), 100))
        bps = clamp_slippage(slippage_bps)
        try:
            addr, sym, dec = await self._resolve_token(token)
            have = await chain.erc20_balance(addr, w.address)
            if have <= 0:
                await inter.followup.send(f"You hold no {sym}.", ephemeral=True)
                return
            amount = have * pct // 100
            rt = await kyber.route(addr, chain.NATIVE, amount)
        except (ValueError, kyber.KyberError, chain.ChainError) as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return

        view = ConfirmOrder(inter.user.id, RHC_CONFIRM_TIMEOUT)
        await inter.followup.send(
            f"Sell **{chain.fmt_units(amount, dec)} {sym}** ({pct}% of your {chain.fmt_units(have, dec)}) for "
            f"**≈ {chain.fmt_units(rt.amount_out, 18)} ETH** ({_fmt_usd(rt.amount_out_usd)})\n"
            f"Token `{addr}`\n"
            f"Route: {', '.join(rt.hops) or '?'} · gas ≈ {_fmt_usd(rt.gas_usd)} · slippage {bps / 100:.2f}%\n"
            f"This is a real swap. Expires in {RHC_CONFIRM_TIMEOUT}s.", view=view, ephemeral=True,
        )
        await view.wait()
        if not view.value:
            await inter.followup.send("⏲️ Expired" if view.value is None else "Cancelled", ephemeral=True)
            return
        confirmed_floor = kyber.min_out(rt.amount_out, bps)
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
            res = await swap.execute(inter.user.id, built, addr, sym)
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
        await self._reply(inter, self._describe(res, f"Sold {chain.fmt_units(amount, dec)} {sym} for {got}"))

    @rhc.command(name="holdings", description="What your wallet holds, with rough USD values")
    async def holdings(self, inter: discord.Interaction):
        w = wallets.get(inter.user.id)
        if w is None:
            await self._send_private(inter, "You have no wallet yet.")
            return
        await inter.response.defer(thinking=True, ephemeral=PRIVATE)
        lines = [f"**{inter.user.display_name}** holds:"]
        try:
            bal = await chain.native_balance(w.address)
            eth_usd = await self._eth_usd()
            usd = f" ≈ ${bal / 1e18 * eth_usd:,.2f}" if eth_usd else ""
            lines.append(f"**{chain.fmt_units(bal, 18)} ETH**{usd}")
        except chain.ChainError:
            lines.append("ETH balance unavailable (RPC)")
        for addr in ledger.tokens_touched(inter.user.id)[:15]:
            try:
                have = await chain.erc20_balance(addr, w.address)
                if have <= 0:
                    continue
                sym, dec = await chain.erc20_meta(addr)
                s = await token_summary(addr)
                price = (s or {}).get("price")
                val = f" ≈ ${have / 10 ** dec * price:,.2f}" if price else ""
                lines.append(f"{chain.fmt_units(have, dec)} **{sym}**{val} · `{_short_addr(addr)}`")
            except Exception:
                lines.append(f"`{_short_addr(addr)}`: balance unavailable")
        await inter.followup.send("\n".join(lines), ephemeral=PRIVATE)

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
    async def _reply(inter: discord.Interaction, text: str) -> None:
        """Deliver a trade result, publicly when configured, and even if the
        followup fails: the tx hash must reach the user."""
        if not PRIVATE:
            text = f"**{inter.user.display_name}** · {text}"
        try:
            await inter.followup.send(text, ephemeral=PRIVATE, suppress_embeds=True)
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
