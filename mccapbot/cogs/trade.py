"""Robinhood trading commands.

Real orders against a real brokerage account, so the gates matter more than the
features. Every command here is refused unless ALL of the following hold:

* ``RH_TRADING_ENABLE`` is on
* credentials are configured
* ``RH_OWNER_ID`` is set AND the caller is that user

That last one is not optional. McCap runs in shared servers; without an owner
check, any member could spend the account holder's money. An unset owner means
*nobody* can trade — never everyone.

Buys additionally require a button press, are capped per-trade and per-day, and
the cap is re-checked after confirmation because a button can sit unclicked
while other orders land.
"""

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .. import coingecko, robinhood, spend
from ..config import (
    RH_CONFIRM_TIMEOUT,
    RH_MAX_DAILY_USD,
    RH_MAX_TRADE_USD,
    RH_OWNER_ID,
    RH_TRADING_ENABLE,
)
from ..logging_setup import log
from ..tables import add_table_fields


def _gate() -> Optional[str]:
    """Why trading is unavailable, or None if it is armed."""
    if not RH_TRADING_ENABLE:
        return "Trading is disabled (`RH_TRADING_ENABLE=0`)."
    if not robinhood.configured():
        return "Robinhood credentials are not configured."
    if not RH_OWNER_ID:
        return (
            "No `RH_OWNER_ID` is set, so nobody is authorised to trade. "
            "This is deliberate — an unset owner must never mean everyone."
        )
    return None


class ConfirmOrder(discord.ui.View):
    """A single-use confirm/cancel prompt, bound to one user."""

    def __init__(self, owner_id: int, timeout: int):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.value: Optional[bool] = None

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        # Buttons are visible to whoever can see the message; bind them to the
        # owner so nobody else can press Confirm.
        if inter.user.id != self.owner_id:
            await inter.response.send_message("This isn't your order.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, _b: discord.ui.Button):
        self.value = True
        for child in self.children:
            child.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, _b: discord.ui.Button):
        self.value = False
        for child in self.children:
            child.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()


class TradeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _deny(self, inter: discord.Interaction) -> bool:
        """Reply and return True when the caller may not trade."""
        reason = _gate()
        if reason:
            await inter.followup.send(f"🔒 {reason}", ephemeral=True)
            return True
        if inter.user.id != RH_OWNER_ID:
            log.warning("Rejected trading command from non-owner %s", inter.user.id)
            await inter.followup.send(
                "🔒 Only the account owner can use trading commands.", ephemeral=True
            )
            return True
        return False

    # ---------------- reads ----------------

    @app_commands.command(name="rh_balance", description="Robinhood buying power and holdings")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def rh_balance(self, inter: discord.Interaction):
        # Always ephemeral: account balances are nobody else's business.
        await inter.response.defer(thinking=True, ephemeral=True)
        if await self._deny(inter):
            return
        try:
            account = await robinhood.get_account()
            holdings = await robinhood.get_holdings()
        except robinhood.RobinhoodError as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return

        embed = discord.Embed(title="Robinhood Crypto", colour=0x2B90D9)
        power = account.get("buying_power")
        if power is not None:
            embed.add_field(name="Buying power", value=f"${float(power):,.2f}", inline=True)
        embed.add_field(
            name="Today's spend",
            value=f"${spend.spent_today():,.2f} / ${RH_MAX_DAILY_USD:,.2f}",
            inline=True,
        )

        rows = []
        for h in (holdings.get("results") or [])[:15]:
            qty = h.get("total_quantity") or h.get("quantity")
            if qty and float(qty) > 0:
                rows.append(f"{h.get('asset_code', '?')}: {float(qty):,.8f}".rstrip("0").rstrip("."))
        embed.add_field(name="Holdings", value="\n".join(rows) or "none", inline=False)
        await inter.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="rh_quote", description="Best bid/ask for a Robinhood crypto pair")
    @app_commands.describe(symbol="Trading pair, e.g. BTC-USD")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def rh_quote(self, inter: discord.Interaction, symbol: str):
        await inter.response.defer(thinking=True, ephemeral=True)
        if await self._deny(inter):
            return
        try:
            q = await robinhood.get_quote(symbol.upper().strip())
        except robinhood.RobinhoodError as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return
        await inter.followup.send(
            f"**{q.symbol}**  bid ${q.bid:,.6f} · ask ${q.ask:,.6f}"
            if q.bid and q.ask else f"**{q.symbol}** — no quote available",
            ephemeral=True,
        )

    # ---------------- buy ----------------

    @app_commands.command(name="rh_buy", description="Buy a dollar amount of a crypto pair (asks to confirm)")
    @app_commands.describe(symbol="Trading pair, e.g. BTC-USD", usd="Dollar amount to spend")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def rh_buy(self, inter: discord.Interaction, symbol: str, usd: float):
        await inter.response.defer(thinking=True, ephemeral=True)
        if await self._deny(inter):
            return

        symbol = symbol.upper().strip()
        ok, why = spend.check(usd)
        if not ok:
            await inter.followup.send(f"🚫 {why}", ephemeral=True)
            return

        try:
            quote = await robinhood.get_quote(symbol)
            price = quote.ask or quote.mid
            if not price:
                raise robinhood.RobinhoodError(f"No usable price for {symbol}.")
            qty, actual = robinhood.quantity_for_usd(usd, price)
        except robinhood.RobinhoodError as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return

        view = ConfirmOrder(inter.user.id, RH_CONFIRM_TIMEOUT)
        embed = discord.Embed(
            title="Confirm market buy",
            colour=0xE67E22,
            description=(
                f"**{symbol}**\n"
                f"Spend **${actual:,.2f}** at ~${price:,.6f}\n"
                f"Quantity **{qty}**\n\n"
                f"Caps: ${RH_MAX_TRADE_USD:,.2f}/trade · "
                f"${spend.remaining():,.2f} left today"
            ),
        )
        embed.set_footer(text=f"This is a real order. Expires in {RH_CONFIRM_TIMEOUT}s.")
        await inter.followup.send(embed=embed, view=view, ephemeral=True)

        await view.wait()
        if view.value is None:
            await inter.followup.send("⏲️ Confirmation expired — nothing was ordered.", ephemeral=True)
            return
        if not view.value:
            await inter.followup.send("Cancelled — nothing was ordered.", ephemeral=True)
            return

        # Re-check: other orders may have landed while this sat unconfirmed.
        ok, why = spend.check(actual)
        if not ok:
            await inter.followup.send(f"🚫 {why}", ephemeral=True)
            return

        try:
            order = await robinhood.place_market_order(symbol, "buy", qty)
        except robinhood.RobinhoodError as e:
            await inter.followup.send(f"❌ Order failed: {e}", ephemeral=True)
            return

        # Only record spend once the order was actually accepted.
        total = spend.record(actual)
        await inter.followup.send(
            f"✅ Buy placed for **{qty} {symbol}** (~${actual:,.2f})\n"
            f"Order `{order.get('id', '?')}` · state `{order.get('state', '?')}`\n"
            f"Today: ${total:,.2f} / ${RH_MAX_DAILY_USD:,.2f}",
            ephemeral=True,
        )

    # ---------------- sell ----------------

    @app_commands.command(name="rh_sell", description="Sell a quantity of a crypto pair (asks to confirm)")
    @app_commands.describe(symbol="Trading pair, e.g. BTC-USD", quantity="Asset quantity to sell")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def rh_sell(self, inter: discord.Interaction, symbol: str, quantity: str):
        await inter.response.defer(thinking=True, ephemeral=True)
        if await self._deny(inter):
            return

        symbol = symbol.upper().strip()
        try:
            qty = float(quantity)
        except ValueError:
            await inter.followup.send("❌ Quantity must be a number.", ephemeral=True)
            return
        if qty <= 0:
            await inter.followup.send("❌ Quantity must be greater than zero.", ephemeral=True)
            return

        est = ""
        try:
            q = await robinhood.get_quote(symbol)
            if q.bid:
                est = f"\n≈ **${qty * q.bid:,.2f}** at ~${q.bid:,.6f}"
        except robinhood.RobinhoodError:
            pass  # a quote is nice to have, not required to sell

        view = ConfirmOrder(inter.user.id, RH_CONFIRM_TIMEOUT)
        embed = discord.Embed(
            title="Confirm market sell",
            colour=0xE67E22,
            description=f"**{symbol}**\nSell **{quantity}**{est}",
        )
        embed.set_footer(text=f"This is a real order. Expires in {RH_CONFIRM_TIMEOUT}s.")
        await inter.followup.send(embed=embed, view=view, ephemeral=True)

        await view.wait()
        if not view.value:
            msg = "⏲️ Confirmation expired" if view.value is None else "Cancelled"
            await inter.followup.send(f"{msg} — nothing was ordered.", ephemeral=True)
            return

        try:
            order = await robinhood.place_market_order(symbol, "sell", quantity)
        except robinhood.RobinhoodError as e:
            await inter.followup.send(f"❌ Order failed: {e}", ephemeral=True)
            return
        # Selling returns money; it is not charged against the daily spend cap.
        await inter.followup.send(
            f"✅ Sell placed for **{quantity} {symbol}**\n"
            f"Order `{order.get('id', '?')}` · state `{order.get('state', '?')}`",
            ephemeral=True,
        )

    # ---------------- trending ----------------

    @app_commands.command(
        name="rh_trending",
        description="Biggest movers among the coins Robinhood actually lists",
    )
    @app_commands.describe(
        window="Rank by 1h, 24h or 7d change (default 24h)",
        count="How many to show (default 10, max 25)",
    )
    @app_commands.choices(window=[
        app_commands.Choice(name="1 hour", value="1h"),
        app_commands.Choice(name="24 hours", value="24h"),
        app_commands.Choice(name="7 days", value="7d"),
    ])
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def rh_trending(
        self,
        inter: discord.Interaction,
        window: Optional[app_commands.Choice[str]] = None,
        count: Optional[int] = 10,
    ):
        # Public market data only — no account details, so this needs no owner
        # gate and is the one trading-adjacent command anyone can run.
        await inter.response.defer(thinking=True)
        key = window.value if window else "24h"
        n = max(1, min(int(count or 10), 25))

        symbols, authoritative = await robinhood.get_trading_pairs()
        coins = await coingecko.for_symbols(symbols)
        if not coins:
            await inter.followup.send(
                "Couldn't reach CoinGecko for market data — try again shortly."
            )
            return

        field = {"1h": "change_1h", "24h": "change_24h", "7d": "change_7d"}[key]
        ranked = [c for c in coins if getattr(c, field) is not None]
        ranked.sort(key=lambda c: getattr(c, field), reverse=True)
        top = ranked[:n]

        rows = [[
            c.symbol,
            f"${c.price:,.4f}".rstrip("0").rstrip(".") if c.price else "—",
            f"{getattr(c, field):+.2f}%",
            f"{c.change_24h:+.1f}%" if c.change_24h is not None else "—",
        ] for c in top]

        gainers = sum(1 for c in ranked if getattr(c, field) > 0)
        embed = discord.Embed(
            title=f"Robinhood movers · {key}",
            colour=0x2ECC71 if (top and getattr(top[0], field) > 0) else 0xE74C3C,
            description=(
                f"{len(ranked)} tradeable coin(s) · **{gainers} up / "
                f"{len(ranked) - gainers} down** over {key}"
            ),
        )
        shown, total = add_table_fields(
            embed, f"Sorted by {key} change",
            ["Coin", "Price", key, "24h"], rows, ["l", "r", "r", "r"], max_fields=3,
        )
        foot = "Prices from CoinGecko"
        foot += (
            " · pair list from your Robinhood account"
            if authoritative
            else " · pair list is a built-in approximation (no credentials configured)"
        )
        if shown < total:
            foot += f" · {total - shown} row(s) not shown"
        embed.set_footer(text=foot)
        await inter.followup.send(embed=embed)

    # ---------------- orders ----------------

    @app_commands.command(name="rh_orders", description="Recent Robinhood orders")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def rh_orders(self, inter: discord.Interaction):
        await inter.response.defer(thinking=True, ephemeral=True)
        if await self._deny(inter):
            return
        try:
            data = await robinhood.get_orders()
        except robinhood.RobinhoodError as e:
            await inter.followup.send(f"❌ {e}", ephemeral=True)
            return

        results = (data.get("results") or [])[:10]
        if not results:
            await inter.followup.send("No recent orders.", ephemeral=True)
            return
        lines = [
            f"`{o.get('id','?')[:8]}` {o.get('side','?')} {o.get('symbol','?')} "
            f"— {o.get('state','?')}"
            for o in results
        ]
        await inter.followup.send("\n".join(lines)[:1900], ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TradeCog(bot))
