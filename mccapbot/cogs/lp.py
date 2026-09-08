"""Liquidity-venue suggestion: /mc_lp."""

import discord
from discord import app_commands
from discord.ext import commands

from ..dex import fetch_dex_token, summarize_lp_venues
from ..helpers import NEUTRAL, SEP, footer, plural, usd


class LpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="mc_lp", description="Suggest the best LP venue (Meteora, Raydium, Pumpswap) for a token"
    )
    @app_commands.describe(ca="Contract address / mint")
    # Read-only market data, same class as /mc_check: works from a user install anywhere.
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def mc_lp(self, inter: discord.Interaction, ca: str):
        await inter.response.defer(thinking=True)
        ca = ca.strip()
        data = await fetch_dex_token(ca)
        if not data or not data.get("pairs"):
            await inter.followup.send(f"Couldn't find pairs for `{ca}`.")
            return

        agg, best = summarize_lp_venues(data["pairs"], ca)
        if not agg:
            await inter.followup.send("No eligible pools found on Meteora, Raydium, or Pumpswap.")
            return

        symbol = next(
            ((p.get("baseToken") or {}).get("symbol") or "" for p in data["pairs"]
             if ((p.get("baseToken") or {}).get("address") or "").lower() == ca.lower()),
            "",
        )
        # Best first; only venues that actually have pools get a line.
        ranked = sorted(agg.items(), key=lambda kv: (-kv[1]["score"], -kv[1]["liq"], -kv[1]["vol"]))
        lines = []
        for venue, a in ranked:
            quotes = ", ".join(sorted(a["quotes"].keys())[:2])
            line = footer(
                f"**{venue.capitalize()}**",
                f"{usd(a['liq'])} liquidity",
                f"{usd(a['vol'])} 24h volume",
                plural(int(a["tx"]), "trade"),
                quotes,
            )
            if a.get("best_url"):
                line += f"\n[Open pool]({a['best_url']})"
            lines.append(line)

        embed = discord.Embed(
            title=footer("Best LP venue", symbol),
            description="\n".join(lines),
            color=NEUTRAL,
        )
        embed.set_footer(text=footer("Ranked by liquidity, volume and trade count",
                                     "check fees and slippage on the pool"))
        await inter.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(LpCog(bot))
