"""/help: every command McCap has, with what it does. Built from the command
tree itself, so it cannot drift from what is actually registered."""

from typing import Dict, List

import discord
from discord import app_commands
from discord.ext import commands

# Where each top-level command belongs on the help card. Anything unlisted
# lands in "Other", so a new cog still shows up.
AREAS = [
    ("Robinhood Chain wallets & trading", ("rh",)),
    ("Market-cap alerts", ("mc", "mc_move", "mc_list", "mc_remove", "mc_recent", "mc_status", "mc_check", "mc_lp")),
    ("Watchlists", ("watch",)),
    ("Chat memory", ("memory",)),
    ("Scan watching", ("scans",)),
]


def _flatten(cmd, prefix: str = "") -> List[tuple]:
    """(qualified name, description) for a command, or for every leaf of a group."""
    name = f"{prefix}{cmd.name}"
    if isinstance(cmd, app_commands.Group):
        out: List[tuple] = []
        for sub in cmd.commands:
            out.extend(_flatten(sub, name + " "))
        return out
    return [(name, cmd.description)]


def build_help(tree: app_commands.CommandTree) -> discord.Embed:
    by_top: Dict[str, List[tuple]] = {}
    for cmd in tree.get_commands():
        if cmd.name == "help":
            continue
        by_top[cmd.name] = _flatten(cmd)

    embed = discord.Embed(
        title="McCap commands",
        colour=0x2B90D9,
        description="Trading results and quotes post to the channel; anything about your keys or a refusal is only shown to you.",
    )
    placed = set()
    for area, names in AREAS:
        lines = []
        for n in names:
            for qual, desc in by_top.get(n, []):
                lines.append(f"`/{qual}` · {desc}")
                placed.add(n)
        if lines:
            embed.add_field(name=area, value="\n".join(lines)[:1024], inline=False)
    other = [f"`/{qual}` · {desc}" for n, entries in by_top.items() if n not in placed for qual, desc in entries]
    if other:
        embed.add_field(name="Other", value="\n".join(other)[:1024], inline=False)
    embed.set_footer(text="Buy/sell ask you to press Confirm within the time limit; expired means nothing happened.")
    return embed


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="What every McCap command does")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def help(self, inter: discord.Interaction):
        await inter.response.send_message(embed=build_help(self.bot.tree))


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
