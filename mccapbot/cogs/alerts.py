"""Market-cap alert commands: /mc, /mc_list, /mc_remove, /mc_recent, /mc_status."""

import re
from typing import List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from ..cache import TOKEN_CACHE_LOCK, token_cache, update_cache
from ..dex import build_token_url, choose_consensus_pair, fetch_dex_token, get_image_url, resolve_mc_value
from ..helpers import humanize, parse_mc_input, username_from_id
from ..models import Reminder
from ..scheduler import describe_tiers, estimated_requests_per_minute, interval_for_reminder
from ..storage import alert_events, reminders, save_reminders
from ..tables import alerts_table, fixed_table

_ID_RE = re.compile(r"^[0-9a-f]{6}$")


def resolve_targets(raw: str, scoped: List[Reminder]) -> Tuple[List[Reminder], List[str]]:
    """Turn a user's `/mc_remove` input into concrete alerts.

    Accepts stable ids (``a1b2c3``, what autocomplete supplies) and 1-based
    positions from ``/mc_list``. Ids are matched first because positions shift
    whenever the watcher fires an alert between listing and removing.
    """
    errs: List[str] = []
    if not raw or not raw.strip():
        return [], ["No alerts specified."]

    by_id = {r.id: r for r in scoped}
    picked: List[Reminder] = []
    seen: set = set()

    for tok in re.split(r"[\s,]+", raw.strip()):
        if not tok:
            continue
        low = tok.lower()
        if _ID_RE.match(low):
            r = by_id.get(low)
            if r is None:
                errs.append(f"`{tok}` is not an alert in this server (it may have already fired).")
                continue
        elif tok.isdigit():
            i = int(tok)
            if i < 1 or i > len(scoped):
                errs.append(f"Index {i} is out of range (1–{len(scoped)}).")
                continue
            r = scoped[i - 1]
        else:
            errs.append(f"`{tok}` is neither an alert id nor a positive index.")
            continue
        if r.id not in seen:
            seen.add(r.id)
            picked.append(r)

    return picked, errs


class AlertsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------- helpers ----------------

    @staticmethod
    def _scoped(guild_id: int) -> List[Reminder]:
        return [r for r in reminders if r.guild_id == guild_id]

    @staticmethod
    def _can_manage(user: discord.abc.User) -> bool:
        return isinstance(user, discord.Member) and (
            user.guild_permissions.manage_guild or user.guild_permissions.administrator
        )

    # ---------------- /mc ----------------

    @app_commands.command(
        name="mc",
        description="Set a Market Cap alert (auto up/down). Optional note is included when it fires.",
    )
    @app_commands.describe(
        ca="Contract address / mint",
        target="Target MC (e.g. 250k, 2.5m, 1b, 1t)",
        note="Optional message to include when the alert fires",
    )
    async def mc(self, inter: discord.Interaction, ca: str, target: str, note: Optional[str] = None):
        await inter.response.defer(thinking=True)
        ca = ca.strip()
        try:
            target_val = parse_mc_input(target)
        except Exception:
            await inter.followup.send(
                "❌ Invalid target. Use `2500000` or shorthand like `250k`, `2.5m`, `1b`, `1t`."
            )
            return
        if target_val <= 0:
            await inter.followup.send("❌ Target must be greater than zero.")
            return

        data = await fetch_dex_token(ca)
        if not data or not data.get("pairs"):
            await inter.followup.send(f"Couldn't find pairs for `{ca}`.")
            return
        best, consensus, _ = choose_consensus_pair(data["pairs"], ca)
        if not best:
            await inter.followup.send(f"No valid pairs found for `{ca}` on allowed DEXes.")
            return

        base = best.get("baseToken") or {}
        name = base.get("name") or base.get("symbol") or "Token"
        symbol = base.get("symbol") or ""
        mc_now_val, src = resolve_mc_value(best, ca)
        url = build_token_url(ca, best)
        img = get_image_url(best, ca)

        await update_cache(
            ca,
            mc=mc_now_val,
            url=url,
            source=src,
            dex=best.get("dexId", ""),
            chain=best.get("chainId", ""),
            quote=((best.get("quoteToken") or {}).get("symbol") or "").upper(),
            consensus=consensus,
            image_url=img,
        )

        if mc_now_val is None:
            direction = "above"
            msg = (
                f"⚠️ `{name}` has no reported MC/FDV yet. I'll watch and alert when it "
                f"reaches **${humanize(target_val)} MC**."
            )
        else:
            direction = "below" if target_val < mc_now_val else "above"
            msg = (
                f"⏰ Alert set for **{name} ({symbol})** — Market Cap "
                f"{'≤' if direction == 'below' else '≥'} ${humanize(target_val)} "
                f"(current: ${humanize(mc_now_val)})."
            )

        rem = Reminder(
            ca=ca,
            target_mc=float(target_val),
            direction=direction,
            channel_id=inter.channel_id,
            creator_id=inter.user.id,
            guild_id=inter.guild_id or 0,
            name=name,
            symbol=symbol,
            note=(note or "").strip(),
        )
        reminders.append(rem)
        await save_reminders()

        every = interval_for_reminder(rem, mc_now_val)
        msg += f"\n🆔 `{rem.id}` · checking every {every}s"
        if note:
            msg += " · 📝 note saved"
        await inter.followup.send(msg)

    # ---------------- /mc_list ----------------

    @app_commands.command(name="mc_list", description="List this server's Market Cap alerts (cached current MC)")
    @app_commands.describe(
        user="Only show alerts created by this user",
        public="Show to everyone (True) or only you (False)",
    )
    async def mc_list(self, inter: discord.Interaction, user: Optional[discord.User] = None, public: bool = True):
        await inter.response.defer(thinking=True, ephemeral=not public)

        gid = inter.guild_id or 0
        sr = self._scoped(gid)
        if user:
            sr = [r for r in sr if r.creator_id == user.id]

        if not sr:
            await inter.followup.send(
                "No active alerts in this server." + (f" (filtered by {user.display_name})" if user else ""),
                ephemeral=not public,
            )
            return

        async with TOKEN_CACHE_LOCK:
            snap_by_ca = {r.ca: token_cache.get(r.ca) for r in sr}

        # Resolve display names once instead of per row.
        names = {uid: await username_from_id(self.bot, uid) for uid in {r.creator_id for r in sr}}

        headers = ["#", "ID", "Token", "Target", "Current", "By"]
        aligns = ["r", "l", "l", "r", "r", "l"]
        rows_ge, rows_le = [], []

        # Index against the unfiltered server list so /mc_remove positions match.
        full = self._scoped(gid)
        pos = {r.id: i for i, r in enumerate(full, 1)}

        for r in sr:
            s = snap_by_ca.get(r.ca)
            curr = f"${humanize(s.mc)}" if s and s.mc is not None else "—"
            dir_sym = "≥" if r.direction == "above" else "≤"
            row = [
                str(pos.get(r.id, "?")),
                r.id,
                r.symbol or r.name,
                f"{dir_sym} ${humanize(r.target_mc)}",
                curr,
                names.get(r.creator_id, "?"),
            ]
            (rows_ge if r.direction == "above" else rows_le).append(row)

        filt = f" (filtered by {user.display_name})" if user else ""
        embed = discord.Embed(
            title="Market Cap Alerts",
            description=f"Cached values shown; grouped by alert direction{filt}.",
            color=0x2B90D9,
        )
        if rows_ge:
            embed.add_field(name="📈 Breakouts (MC ≥ target)", value=fixed_table(headers, rows_ge, aligns), inline=False)
        if rows_le:
            embed.add_field(name="📉 Pullbacks (MC ≤ target)", value=fixed_table(headers, rows_le, aligns), inline=False)
        embed.set_footer(text=f"{len(sr)} alert(s){filt} • remove with /mc_remove")
        await inter.followup.send(embed=embed, ephemeral=not public)

    # ---------------- /mc_remove ----------------

    async def _remove_autocomplete(self, inter: discord.Interaction, current: str):
        gid = inter.guild_id or 0
        scoped = self._scoped(gid)
        q = (current or "").lower().strip()
        # Only offer alerts the caller is actually allowed to remove.
        allowed = [r for r in scoped if r.creator_id == inter.user.id or self._can_manage(inter.user)]
        out = []
        for r in allowed:
            label = (
                f"{r.symbol or r.name} {'≥' if r.direction == 'above' else '≤'} "
                f"${humanize(r.target_mc)} ({r.id})"
            )
            if q and q not in label.lower() and q not in r.ca.lower():
                continue
            out.append(app_commands.Choice(name=label[:100], value=r.id))
            if len(out) >= 25:
                break
        return out

    @app_commands.command(name="mc_remove", description="Remove one or more alerts (pick from the list, or pass ids)")
    @app_commands.describe(alerts="Alert id(s) or /mc_list index/indices, e.g. 'a1b2c3' or '1 3 5'")
    @app_commands.autocomplete(alerts=_remove_autocomplete)
    async def mc_remove(self, inter: discord.Interaction, alerts: str):
        await inter.response.defer(thinking=False)

        gid = inter.guild_id or 0
        scoped = self._scoped(gid)
        if not scoped:
            await inter.followup.send("There are no active alerts in this server.")
            return

        picked, parse_errs = resolve_targets(alerts, scoped)
        if not picked:
            await inter.followup.send(
                "❌ Nothing to remove:\n" + "\n".join(f"• {e}" for e in parse_errs)
            )
            return

        can_manage = self._can_manage(inter.user)
        removed, denied = [], []
        for rem in picked:
            if inter.user.id != rem.creator_id and not can_manage:
                denied.append(rem)
                continue
            try:
                reminders.remove(rem)
            except ValueError:
                parse_errs.append(f"`{rem.id}` already fired or was removed.")
                continue
            removed.append(rem)

        if removed:
            await save_reminders()

        lines = []
        if removed:
            lines.append("🗑️ **Removed:**")
            lines += [
                f"• `{r.id}` {r.name} ({r.symbol}) — MC "
                f"{'≥' if r.direction == 'above' else '≤'} ${humanize(r.target_mc)}"
                for r in removed
            ]
        else:
            lines.append("No alerts removed.")
        if denied:
            lines.append(
                "\n🔒 **Permission denied for:** "
                + ", ".join(f"`{r.id}`" for r in denied)
                + " (only the creator or users with **Manage Server** can remove those)."
            )
        if parse_errs:
            lines.append("\n⚠️ **Input issues:**")
            lines += [f"• {e}" for e in parse_errs]
        await inter.followup.send("\n".join(lines))

    # ---------------- /mc_recent ----------------

    @app_commands.command(name="mc_recent", description="Show recent fired MC alerts (default last 5).")
    @app_commands.describe(
        count="How many to show (default 5, max 50)",
        user="Only alerts created by this user (optional)",
        public="Show to everyone (True) or only you (False)",
    )
    async def mc_recent(
        self,
        inter: discord.Interaction,
        count: Optional[int] = 5,
        user: Optional[discord.User] = None,
        public: Optional[bool] = True,
    ):
        await inter.response.defer(thinking=True, ephemeral=not public)
        gid = inter.guild_id or 0
        evs = [e for e in alert_events if e.guild_id == gid]
        if user:
            evs = [e for e in evs if e.creator_id == user.id]
        evs.sort(key=lambda e: e.ts, reverse=True)
        evs = evs[: max(1, min(int(count or 5), 50))]
        if not evs:
            await inter.followup.send("No matching alerts found.", ephemeral=not public)
            return

        name_by_id = {uid: await username_from_id(self.bot, uid) for uid in {e.creator_id for e in evs}}
        async with TOKEN_CACHE_LOCK:
            current_by_ca = {e.ca: (token_cache[e.ca].mc if e.ca in token_cache else None) for e in evs}

        filt = f" — by: {user.name}" if user else ""
        embed = discord.Embed(
            title="Recent Alerts", description=f"Most recent {len(evs)} alert(s){filt}", color=0xF39C12
        )
        embed.add_field(name="History", value=alerts_table(evs, name_by_id, current_by_ca), inline=False)
        await inter.followup.send(embed=embed, ephemeral=not public)

    # ---------------- /mc_status ----------------

    @app_commands.command(name="mc_status", description="Show what the alert watcher is doing right now")
    async def mc_status(self, inter: discord.Interaction):
        await inter.response.defer(thinking=True, ephemeral=True)
        gid = inter.guild_id or 0
        scoped = self._scoped(gid)

        async with TOKEN_CACHE_LOCK:
            mc_all = {r.ca: (token_cache[r.ca].mc if r.ca in token_cache else None) for r in reminders}

        tiers = describe_tiers(reminders, mc_all)
        rate = estimated_requests_per_minute(reminders, mc_all)

        rows = [
            ["🔥 hot (near target)", str(tiers["hot"])],
            ["🌤 warm", str(tiers["warm"])],
            ["🧊 cold", str(tiers["cold"])],
            ["❔ no MC data", str(tiers["unknown"])],
        ]
        embed = discord.Embed(title="Watcher status", color=0x2B90D9)
        embed.add_field(name="Alert tiers (all servers)", value=fixed_table(["Tier", "Count"], rows, ["l", "r"]), inline=False)
        embed.add_field(
            name="Load",
            value=(
                f"**{len(reminders)}** alert(s) over **{len({r.ca for r in reminders})}** token(s)\n"
                f"≈ **{rate:.0f}** DexScreener req/min (limit 300)\n"
                f"**{len(scoped)}** alert(s) in this server"
            ),
            inline=False,
        )
        await inter.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AlertsCog(bot))
