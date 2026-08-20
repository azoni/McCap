"""Watchlists: /watch add, /watch remove, /watch view, /watch lists.

Deliberately read-on-demand. Watchlist tokens are fetched only when someone
runs /watch view, so a long list costs nothing in the background and never eats
into the request budget the alert watcher depends on.
"""

import asyncio
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..config import MAX_WATCH_PER_LIST
from ..dex import token_summary
from ..helpers import humanize, short_ca
from ..models import WatchItem
from ..storage import save_watchlist, watchlist
from ..tables import fixed_table

DEFAULT_LIST = "default"


def _norm(name: Optional[str]) -> str:
    return (name or DEFAULT_LIST).strip().lower()[:32] or DEFAULT_LIST


def entries(guild_id: int, list_name: str) -> List[WatchItem]:
    return [w for w in watchlist if w.guild_id == guild_id and w.list_name == list_name]


def list_names(guild_id: int) -> List[str]:
    return sorted({w.list_name for w in watchlist if w.guild_id == guild_id})


class WatchCog(commands.Cog):
    watch = app_commands.Group(name="watch", description="Track a group of tokens in one view")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _list_autocomplete(self, inter: discord.Interaction, current: str):
        q = (current or "").lower()
        names = list_names(inter.guild_id or 0) or [DEFAULT_LIST]
        return [app_commands.Choice(name=n, value=n) for n in names if q in n][:25]

    # ---------------- /watch add ----------------

    @watch.command(name="add", description="Add a token to a watchlist")
    @app_commands.describe(ca="Contract address / mint", list="Watchlist name (default: default)")
    @app_commands.autocomplete(list=_list_autocomplete)
    async def add(self, inter: discord.Interaction, ca: str, list: Optional[str] = None):
        await inter.response.defer(thinking=True)
        ca = ca.strip()
        gid = inter.guild_id or 0
        name = _norm(list)

        if any(w.ca == ca for w in entries(gid, name)):
            await inter.followup.send(f"`{short_ca(ca)}` is already on **{name}**.")
            return
        if len(entries(gid, name)) >= MAX_WATCH_PER_LIST:
            await inter.followup.send(
                f"**{name}** is full ({MAX_WATCH_PER_LIST} tokens). Remove one or start another list."
            )
            return

        info = await token_summary(ca)
        if not info:
            await inter.followup.send(f"Couldn't find pairs for `{ca}`.")
            return

        watchlist.append(
            WatchItem(
                ca=ca, guild_id=gid, added_by=inter.user.id,
                name=info["name"], symbol=info["symbol"], list_name=name,
            )
        )
        await save_watchlist()
        await inter.followup.send(
            f"👁️ Added **{info['name']} ({info['symbol']})** to **{name}** "
            f"— MC ${humanize(info['mc'])}, {len(entries(gid, name))} token(s) on this list."
        )

    # ---------------- /watch remove ----------------

    async def _token_autocomplete(self, inter: discord.Interaction, current: str):
        q = (current or "").lower()
        out = []
        for w in watchlist:
            if w.guild_id != (inter.guild_id or 0):
                continue
            label = f"{w.symbol or w.name} ({w.list_name})"
            if q and q not in label.lower() and q not in w.ca.lower():
                continue
            out.append(app_commands.Choice(name=label[:100], value=w.ca))
        return out[:25]

    @watch.command(name="remove", description="Remove a token from a watchlist")
    @app_commands.describe(ca="Contract address / mint", list="Watchlist name (default: default)")
    @app_commands.autocomplete(ca=_token_autocomplete, list=_list_autocomplete)
    async def remove(self, inter: discord.Interaction, ca: str, list: Optional[str] = None):
        await inter.response.defer(thinking=False)
        gid = inter.guild_id or 0
        name = _norm(list)
        ca = ca.strip()

        hit = next((w for w in entries(gid, name) if w.ca == ca), None)
        if not hit:
            await inter.followup.send(f"`{short_ca(ca)}` isn't on **{name}**.")
            return
        watchlist.remove(hit)
        await save_watchlist()
        await inter.followup.send(f"🗑️ Removed **{hit.name} ({hit.symbol})** from **{name}**.")

    # ---------------- /watch view ----------------

    @watch.command(name="view", description="Show a watchlist with live market caps")
    @app_commands.describe(list="Watchlist name (default: default)", public="Show to everyone")
    @app_commands.autocomplete(list=_list_autocomplete)
    async def view(self, inter: discord.Interaction, list: Optional[str] = None, public: bool = True):
        await inter.response.defer(thinking=True, ephemeral=not public)
        gid = inter.guild_id or 0
        name = _norm(list)
        items = entries(gid, name)

        if not items:
            avail = list_names(gid)
            extra = f" Available: {', '.join(avail)}." if avail else ""
            await inter.followup.send(f"**{name}** is empty.{extra}", ephemeral=not public)
            return

        infos = await asyncio.gather(*(token_summary(w.ca) for w in items), return_exceptions=True)

        rows, total_mc, missing = [], 0.0, 0
        pairs = []
        for w, info in zip(items, infos):
            if isinstance(info, Exception) or not info:
                missing += 1
                rows.append([w.symbol or w.name, "—", "—", "—"])
                continue
            total_mc += info["mc"] or 0
            pairs.append((w, info))

        # Biggest movers first — that's what you want to see during a run.
        pairs.sort(key=lambda p: p[1]["change24"], reverse=True)
        for w, info in pairs:
            ch = info["change24"]
            rows.insert(
                len(rows) - missing if missing else len(rows),
                [
                    info["symbol"] or info["name"],
                    f"${humanize(info['mc'])}",
                    f"{ch:+.1f}%",
                    f"${humanize(info['liq'])}",
                ],
            )

        table = fixed_table(["Token", "MC", "24h", "Liq"], rows, ["l", "r", "r", "r"], max_width=12)
        gainers = sum(1 for _, i in pairs if i["change24"] > 0)

        embed = discord.Embed(
            title=f"Watchlist · {name}",
            description=f"{len(items)} token(s) · combined MC **${humanize(total_mc)}**",
            color=0x2B90D9,
        )
        embed.add_field(name="Sorted by 24h change", value=table, inline=False)
        foot = f"{gainers} up / {len(pairs) - gainers} down"
        if missing:
            foot += f" · {missing} with no market data"
        embed.set_footer(text=foot)
        await inter.followup.send(embed=embed, ephemeral=not public)

    # ---------------- /watch lists ----------------

    @watch.command(name="lists", description="Show every watchlist in this server")
    async def lists(self, inter: discord.Interaction):
        await inter.response.defer(thinking=True, ephemeral=True)
        gid = inter.guild_id or 0
        names = list_names(gid)
        if not names:
            await inter.followup.send("No watchlists yet — create one with `/watch add`.", ephemeral=True)
            return
        rows = [[n, str(len(entries(gid, n)))] for n in names]
        embed = discord.Embed(title="Watchlists", color=0x2B90D9)
        embed.add_field(name="This server", value=fixed_table(["List", "Tokens"], rows, ["l", "r"]), inline=False)
        await inter.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WatchCog(bot))
