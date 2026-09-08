"""/mc_check — holder and risk context for a token, on demand.

Answers the question a market cap alone cannot: is this thing worth touching?
Concentration, holder count, mint/freeze authority and dev mints come from
Jupiter; the market cap comes from whichever source actually has one.
"""

import discord
from discord import app_commands
from discord.ext import commands

from .. import jupiter
from ..config import TOP_HOLDER_WARN_PCT
from ..dex import token_summary
from ..helpers import BAD, NEUTRAL, SEP, footer, pct, plural, short_ca, usd


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
        from_jupiter = not (summary or {}).get("mc") and bool(tok and tok.mcap)

        concentrated = tok is not None and tok.concentrated(TOP_HOLDER_WARN_PCT)
        risky = concentrated or (tok is not None and tok.authorities_live())

        head = f"**{usd(mc)}** MC" + (" (Jupiter's figure)" if from_jupiter else "")
        if summary:
            head += (f"{SEP}24h {pct(summary.get('change24'))}{SEP}liquidity {usd(summary.get('liq'))} "
                     f"in {plural(int(summary.get('pools') or 0), 'pool')}")
        desc = head
        # One line of what Jupiter actually knows; missing fields are omitted,
        # never shown as zero. The same line rides on fired alerts.
        risk = jupiter.risk_line(tok, TOP_HOLDER_WARN_PCT)
        if risk:
            desc += f"\n🔎 {risk}"

        embed = discord.Embed(
            title=f"{name} ({symbol})" if symbol else name,
            url=(summary or {}).get("url") or f"https://gmgn.ai/sol/token/{ca}",
            colour=BAD if risky else NEUTRAL,
            description=desc,
        )
        if summary and summary.get("image_url"):
            embed.set_thumbnail(url=summary["image_url"])

        # These are third-party measurements, not verdicts. Say so.
        embed.set_footer(text=footer("Holder data from Jupiter", "not financial advice"))
        await inter.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(CheckCog(bot))
