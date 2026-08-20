"""Liquidity-venue suggestion: /mc_lp."""

import discord
from discord import app_commands
from discord.ext import commands

from ..dex import fetch_dex_token, summarize_lp_venues, table_lp


class LpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="mc_lp", description="Suggest the best LP venue (Meteora, Raydium, Pumpswap) for a token"
    )
    @app_commands.describe(ca="Contract address / mint")
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

        if best:
            best_name, a = best
            rec_title = f"✅ Best LP venue: **{best_name.capitalize()}**"
            rec_link = a.get("best_url")
            rec_line = f"[Open pool]({rec_link})" if rec_link else ""
            notes = "Scored by liquidity + 24h volume + 24h tx count (log-weighted)."
        else:
            rec_title, rec_line, notes = "✅ Best LP venue", "", "No single venue dominated; compare metrics below."

        embed = discord.Embed(
            title="LP Venue Suggestion",
            description=f"{rec_title}\n{rec_line}\n\n{notes}",
            color=0x00B894,
        )
        embed.add_field(name="Meteora / Raydium / Pumpswap (aggregated)", value=table_lp(agg), inline=False)
        embed.set_footer(text="Heuristic suggestion — always double-check slippage/fees for your pool.")
        await inter.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(LpCog(bot))
