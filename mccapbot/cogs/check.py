"""/mc_check — holder and risk context for a token, on demand.

Answers the question a market cap alone cannot: is this thing worth touching?
Concentration, holder count, mint/freeze authority and dev mints come from
Jupiter; the market cap comes from whichever source actually has one.
"""

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .. import jupiter
from ..config import TOP_HOLDER_WARN_PCT
from ..dex import token_summary
from ..helpers import humanize, short_ca


def _yes_no(v: Optional[bool], good: str, bad: str) -> str:
    if v is None:
        return "—"
    return good if v else bad


class CheckCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="mc_check",
        description="Holder concentration, authorities and risk context for a token",
    )
    @app_commands.describe(ca="Contract address / mint")
    # Read-only, so it works from a user installation anywhere.
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def mc_check(self, inter: discord.Interaction, ca: str):
        await inter.response.defer(thinking=True)
        ca = ca.strip()

        tok = await jupiter.fetch_one(ca)
        summary = await token_summary(ca)

        if tok is None and summary is None:
            await inter.followup.send(f"Couldn't find `{short_ca(ca)}` on Jupiter or DexScreener.")
            return

        name = (summary or {}).get("name") or (tok.name if tok else "Token")
        symbol = (summary or {}).get("symbol") or (tok.symbol if tok else "")
        # Prefer DexScreener's consensus market cap; fall back to Jupiter.
        mc = (summary or {}).get("mc") or (tok.mcap if tok else None)
        mc_src = "DexScreener" if (summary or {}).get("mc") else ("Jupiter" if tok and tok.mcap else "")

        concentrated = tok is not None and tok.concentrated(TOP_HOLDER_WARN_PCT)
        risky = concentrated or (tok is not None and tok.authorities_live())
        colour = 0xE74C3C if risky else 0x2ECC71

        embed = discord.Embed(
            title=f"{name} ({symbol})",
            url=(summary or {}).get("url") or f"https://gmgn.ai/sol/token/{ca}",
            colour=colour,
            description=f"**MC** ${humanize(mc)}" + (f"  ·  _{mc_src}_" if mc_src else ""),
        )
        if summary and summary.get("image_url"):
            embed.set_thumbnail(url=summary["image_url"])

        if tok is not None:
            embed.add_field(
                name="Holders",
                value=(f"{tok.holders:,}" if tok.holders is not None else "—"),
                inline=True,
            )
            top10 = "—"
            if tok.top10_pct is not None:
                top10 = f"{'⚠️ ' if concentrated else ''}{tok.top10_pct:.1f}%"
            embed.add_field(name="Top 10 hold", value=top10, inline=True)
            embed.add_field(
                name="Dev mints",
                value=(str(tok.dev_mints) if tok.dev_mints is not None else "—"),
                inline=True,
            )
            embed.add_field(
                name="Mint authority",
                value=_yes_no(tok.mint_disabled, "revoked", "⚠️ live"),
                inline=True,
            )
            embed.add_field(
                name="Freeze authority",
                value=_yes_no(tok.freeze_disabled, "revoked", "⚠️ live"),
                inline=True,
            )
            embed.add_field(name="Organic activity", value=(tok.organic or "—"), inline=True)

        if summary:
            embed.add_field(
                name="Market",
                value=(
                    f"24h {summary.get('change24', 0):+.1f}%  ·  "
                    f"liquidity ${humanize(summary.get('liq'))} across "
                    f"{summary.get('pools', 0)} pool(s)"
                ),
                inline=False,
            )

        # These are third-party measurements, not verdicts. Say so.
        embed.set_footer(
            text="Holder data from Jupiter · not financial advice, and not a safety guarantee"
        )
        await inter.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(CheckCog(bot))
