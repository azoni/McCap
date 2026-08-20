"""Solana Pay commands: /pay, /pay_status, /pay_list."""

import time
from typing import Dict, Optional
from urllib.parse import quote

import discord
from discord import app_commands
from discord.ext import commands

from ..config import DONATION_WALLET, PAY_EXPIRY_SEC
from ..helpers import to_lamports, username_from_id
from ..models import Invoice
from ..payments import parse_asset_choice, qr_url, solana_pay_link
from ..solana import random_pubkey
from ..storage import invoices, save_invoices
from ..tables import payments_table_with_users


class PaymentsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="pay", description="Pay the bot via Solana Pay (defaults to SOL).")
    @app_commands.describe(
        amount="Amount (e.g., 0.25)",
        asset="(Optional) Choose USDC if not paying in SOL",
        note="Optional note to include",
    )
    @app_commands.choices(
        asset=[
            app_commands.Choice(name="SOL (default)", value="SOL"),
            app_commands.Choice(name="USDC", value="USDC"),
        ]
    )
    async def pay(
        self,
        inter: discord.Interaction,
        amount: float,
        asset: Optional[app_commands.Choice[str]] = None,
        note: Optional[str] = None,
    ):
        await inter.response.defer(thinking=True, ephemeral=True)
        if not DONATION_WALLET:
            await inter.followup.send("⚠️ No DONATION_WALLET configured.", ephemeral=True)
            return
        if amount <= 0:
            await inter.followup.send("Enter a positive amount.", ephemeral=True)
            return

        asset_u, dec, mint = parse_asset_choice(asset.value if asset else "SOL")
        ref_full = random_pubkey()

        if asset_u == "SOL":
            amount_base = to_lamports(amount)
            sp_link = solana_pay_link(
                DONATION_WALLET, amount, label="McCap Bot", message=(note or ""), reference=ref_full
            )
        else:
            amount_base = int(round(amount * (10**dec)))
            sp_link = solana_pay_link(
                DONATION_WALLET,
                amount,
                label="McCap Bot",
                message=(note or ""),
                reference=ref_full,
                spl_token=mint,
            )

        inv = Invoice(
            id=ref_full[:6],
            reference=ref_full,
            asset=asset_u,
            mint=(mint or ""),
            amount_base=amount_base,
            decimals=dec,
            user_id=inter.user.id,
            channel_id=inter.channel_id,
            guild_id=inter.guild_id or 0,
            note=(note or ""),
            created_ts=time.time(),
        )
        invoices.append(inv)
        await save_invoices()

        encoded = quote(sp_link, safe="")
        desc = (
            "**Phantom (browser extension):** click the button below — the extension will open.\n"
            f"Or scan the **QR**, or use Solflare.\n\n**Amount:** {amount:,.4f} {asset_u}\n"
            f"**Invoice ID:** `{inv.id}` (expires in {PAY_EXPIRY_SEC // 60}m)\n\n"
            f"**Mobile users:** you can also tap this Solana Pay link:\n`{sp_link}`"
        )
        embed = discord.Embed(title="Solana Pay", description=desc, color=0x00B894)
        embed.set_image(url=qr_url(sp_link, 260))

        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="Open in Phantom (Extension)",
                url=f"https://phantom.app/ul/v1/pay?link={encoded}",
                style=discord.ButtonStyle.link,
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Open in Solflare",
                url=f"https://solflare.com/ul/v1/solanaPay?link={encoded}",
                style=discord.ButtonStyle.link,
            )
        )
        await inter.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="pay_status", description="Check status of your last payment (or by ID)")
    @app_commands.describe(invoice_id="Optional invoice id (first 6 chars shown when created)")
    async def pay_status(self, inter: discord.Interaction, invoice_id: Optional[str] = None):
        await inter.response.defer(thinking=True, ephemeral=True)
        gid = inter.guild_id or 0
        mine = [i for i in invoices if i.guild_id == gid and i.user_id == inter.user.id]
        if not mine:
            await inter.followup.send("No payments found.", ephemeral=True)
            return

        inv = None
        if invoice_id:
            inv = next((i for i in mine if i.id == invoice_id), None)
        inv = inv or max(mine, key=lambda x: x.created_ts)

        human_amt = inv.amount_base / (10**inv.decimals)
        lines = [
            f"**Invoice:** `{inv.id}`",
            f"**Asset:** {inv.asset}",
            f"**Amount:** {human_amt:,.4f} {inv.asset}",
            f"**Status:** {inv.status.upper()}",
        ]
        if inv.tx_sig:
            lines.append(f"**Tx:** `{inv.tx_sig}`")
        await inter.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(
        name="pay_list", description="List payments (yours by default, or server-wide with permission)."
    )
    @app_commands.describe(
        scope="mine (default) or server",
        status="Filter by status: pending, paid, expired (optional)",
        limit="Max rows to display (default 15)",
        public="Show in channel (True) or only to you (False)",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="mine (default)", value="mine"),
            app_commands.Choice(name="server (admin only)", value="server"),
        ],
        status=[
            app_commands.Choice(name="pending", value="pending"),
            app_commands.Choice(name="paid", value="paid"),
            app_commands.Choice(name="expired", value="expired"),
        ],
    )
    async def pay_list(
        self,
        inter: discord.Interaction,
        scope: Optional[app_commands.Choice[str]] = None,
        status: Optional[app_commands.Choice[str]] = None,
        limit: Optional[int] = 15,
        public: Optional[bool] = True,
    ):
        await inter.response.defer(thinking=True, ephemeral=not public)
        gid = inter.guild_id or 0
        scope_val = scope.value if scope else "mine"

        if scope_val == "server":
            member_ok = isinstance(inter.user, discord.Member) and (
                inter.user.guild_permissions.manage_guild or inter.user.guild_permissions.administrator
            )
            if not member_ok:
                await inter.followup.send(
                    "❌ You need **Manage Server** to view server-wide payments.", ephemeral=not public
                )
                return

        records = [i for i in invoices if i.guild_id == gid]
        if scope_val == "mine":
            records = [i for i in records if i.user_id == inter.user.id]
        if status:
            records = [i for i in records if i.status == status.value]
        records.sort(key=lambda x: x.created_ts, reverse=True)
        records = records[: (limit or 15)]

        if not records:
            await inter.followup.send("No matching payments found.", ephemeral=not public)
            return

        name_by_id: Dict[int, str] = {
            uid: await username_from_id(self.bot, uid) for uid in {i.user_id for i in records}
        }
        scope_label = "your payments" if scope_val == "mine" else "server payments"
        filt = f" — status:{status.value}" if status else ""
        embed = discord.Embed(title="Payments", description=f"{scope_label}{filt}", color=0x00B894)
        embed.add_field(name="History", value=payments_table_with_users(records, name_by_id), inline=False)
        await inter.followup.send(embed=embed, ephemeral=not public)


async def setup(bot: commands.Bot):
    await bot.add_cog(PaymentsCog(bot))
