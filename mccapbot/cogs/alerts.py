"""Alert commands: /mc, /mc_move, /mc_list, /mc_remove, /mc_recent, /mc_status, /mc_clear."""

import re
import time
from typing import List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from .. import gecko, history
from ..cache import TOKEN_CACHE_LOCK, token_cache, update_cache
from ..config import MOVE_DEFAULT_COOLDOWN
from ..logging_setup import log
from ..dex import build_token_url, choose_consensus_pair, fetch_dex_token, get_image_url, resolve_mc_value
from ..helpers import (
    NEUTRAL,
    SEP,
    UNKNOWN,
    RelativeTargetError,
    fit_lines,
    footer,
    human_window,
    parse_target,
    parse_window,
    pct,
    plural,
    usd,
    username_from_id,
    when,
)
from ..alerts import NO_DATA_GIVE_UP
from ..alerts import _no_data as watcher_no_data
from ..models import MoveAlert, Reminder
from ..scheduler import (
    describe_tiers,
    estimated_requests_per_minute,
    interval_for_reminder,
)
from ..storage import alert_events, live_orders, move_alerts, order_watchers, reminders, save_moves, save_reminders
from ..views import ConfirmOrder
from ..tables import add_table_fields

MESSAGE_LIMIT = 2000

# Kept under its old name: the reply-length regression test imports it.
_fit = fit_lines

_ID_RE = re.compile(r"^[0-9a-f]{6}$")
ARROWS = {"up": "▲", "down": "▼", "both": "±"}


def resolve_targets(raw: str, scoped: List, scope: str = "in this server") -> Tuple[List, List[str]]:
    """Turn a user's `/mc_remove` input into concrete alerts.

    Accepts stable ids (``a1b2c3``, what autocomplete supplies) and 1-based
    positions from ``/mc_list``. Ids are matched first because positions shift
    whenever the watcher fires an alert between listing and removing.

    ``scope`` is the caller's wording for where it looked — a DM lists the
    caller's alerts across every server, so "in this server" would be a lie.
    """
    errs: List[str] = []
    if not raw or not raw.strip():
        return [], ["No alerts specified."]

    by_id = {r.id: r for r in scoped}
    picked, seen = [], set()

    for tok in re.split(r"[\s,]+", raw.strip()):
        if not tok:
            continue
        low = tok.lower()
        if _ID_RE.match(low):
            r = by_id.get(low)
            if r is None:
                errs.append(f"`{tok}` is not an alert {scope} — check `/mc_list`.")
                continue
        # str.isdigit() is True for '²' and '⁵', which int() then rejects with a
        # ValueError. Unhandled, that killed the whole command after the defer,
        # so the interaction just hung on "thinking".
        elif tok.isascii() and tok.isdigit():
            if not scoped:
                errs.append(
                    "There are no numbered alerts here — momentum alerts are removed by id."
                )
                continue
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

    if not picked and not errs:
        errs.append("No alerts specified.")
    return picked, errs


async def _lookup(ca: str):
    """Resolve a contract address to (name, symbol, mc, url, image, consensus, dex info)."""
    data = await fetch_dex_token(ca)
    if not data or not data.get("pairs"):
        return None
    best, consensus, _ = choose_consensus_pair(data["pairs"], ca)
    if not best:
        return None
    base = best.get("baseToken") or {}
    mc, src = resolve_mc_value(best, ca)
    return {
        "name": base.get("name") or base.get("symbol") or "Token",
        "symbol": base.get("symbol") or "",
        "mc": mc,
        "src": src,
        "url": build_token_url(ca, best),
        "image": get_image_url(best, ca),
        "consensus": consensus,
        "dex": best.get("dexId", ""),
        "chain": best.get("chainId", ""),
        "quote": ((best.get("quoteToken") or {}).get("symbol") or "").upper(),
    }


def _level_label(r) -> str:
    """PONS ≥ $2M: how a level alert is named everywhere it is listed."""
    return f"{r.symbol or r.name} {'≥' if r.direction == 'above' else '≤'} {usd(r.target_mc)}"


def _move_label(m) -> str:
    """PONS ▲30% / 1h."""
    return f"{m.symbol or m.name} {ARROWS[m.direction]}{pct(m.pct, signed=False)} / {human_window(m.window_sec)}"


class AlertsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------- helpers ----------------

    @staticmethod
    def _scoped(inter: discord.Interaction) -> List[Reminder]:
        """Alerts visible in this context.

        In a server that means the server's alerts. When the app is invoked
        from a user installation (a DM, or a server the bot isn't in) there is
        no guild, so scope to the caller's own alerts across every server —
        falling back to guild_id 0 would pool every user's private alerts
        together and show them to each other.
        """
        if inter.guild_id:
            return [r for r in reminders if r.guild_id == inter.guild_id]
        return [r for r in reminders if r.creator_id == inter.user.id]

    @staticmethod
    def _scoped_moves(inter: discord.Interaction) -> List[MoveAlert]:
        if inter.guild_id:
            return [m for m in move_alerts if m.guild_id == inter.guild_id]
        return [m for m in move_alerts if m.creator_id == inter.user.id]

    @staticmethod
    def _may_remove(user, owner_id: int, can_manage: bool) -> bool:
        """Who is allowed to delete an alert.

        creator_id 0 means it was armed automatically from a detected scan and
        belongs to nobody, so anyone in the server can clear it — otherwise the
        server accumulates alerts only an admin can remove.
        """
        return (not owner_id) or user.id == owner_id or can_manage

    @staticmethod
    def _can_manage(user: discord.abc.User) -> bool:
        return isinstance(user, discord.Member) and (
            user.guild_permissions.manage_guild or user.guild_permissions.administrator
        )

    # ---------------- /mc ----------------

    @app_commands.command(
        name="mc",
        description="Alert when market cap hits a target. Accepts 2x, +50%, -30% or an absolute like 2.5m.",
    )
    @app_commands.describe(
        ca="Contract address / mint",
        target="2x, +50%, -30%, or absolute (250k, 2.5m, 1b)",
        note="Optional message included when it fires",
    )
    async def mc(self, inter: discord.Interaction, ca: str, target: str, note: Optional[str] = None):
        await inter.response.defer(thinking=True)
        ca = ca.strip()

        info = await _lookup(ca)
        if not info:
            await inter.followup.send(f"Couldn't find pairs for `{ca}`.")
            return

        mc_now = info["mc"]
        try:
            target_val, spec = parse_target(target, mc_now)
        except RelativeTargetError:
            await inter.followup.send(
                f"**{info['name']}** has no reported market cap yet, so `{target}` has nothing to "
                "anchor to. Use an absolute target like `250k`."
            )
            return
        except ValueError:
            await inter.followup.send(
                "❌ Invalid target. Use `2x`, `+50%`, `-30%`, or an absolute like `250k` / `2.5m`."
            )
            return
        if target_val <= 0:
            await inter.followup.send("❌ Target must be greater than zero.")
            return

        await update_cache(
            ca, mc=mc_now, url=info["url"], source=info["src"], dex=info["dex"],
            chain=info["chain"], quote=info["quote"], consensus=info["consensus"],
            image_url=info["image"],
        )

        name = info["symbol"] or info["name"]
        if mc_now is None:
            direction = "above"
            msg = (
                f"⚠️ **{name}** has no reported market cap yet. I'll watch and alert when it "
                f"reaches **{usd(target_val)} MC**."
            )
        else:
            direction = "below" if target_val < mc_now else "above"
            sym = "≤" if direction == "below" else "≥"
            anchor = f"{spec} from {usd(mc_now)} now" if spec else f"now {usd(mc_now)}"
            msg = f"⏰ **{name}** alert set: MC {sym} **{usd(target_val)}** ({anchor})"

        rem = Reminder(
            ca=ca, target_mc=float(target_val), direction=direction,
            channel_id=inter.channel_id, creator_id=inter.user.id,
            guild_id=inter.guild_id or 0, name=info["name"], symbol=info["symbol"],
            note=(note or "").strip(), spec=spec, anchor_mc=mc_now,
        )
        reminders.append(rem)
        await save_reminders()

        msg += "\n" + footer(
            f"`{rem.id}`",
            f"checks every {human_window(interval_for_reminder(rem, mc_now))}",
            "note saved" if note else "",
        )
        await inter.followup.send(msg)

    # ---------------- /mc_move ----------------

    @app_commands.command(
        name="mc_move",
        description="Alert on momentum: fire when a token moves X% within a time window.",
    )
    @app_commands.describe(
        ca="Contract address / mint",
        percent="Size of the move, e.g. 30 for 30%",
        window="Time window: 15m, 1h, 4h, 1d",
        direction="Pump, dump, or either (default either)",
        cooldown="Minimum gap between repeats (default 30m)",
        note="Optional message included when it fires",
    )
    @app_commands.choices(
        direction=[
            app_commands.Choice(name="either (default)", value="both"),
            app_commands.Choice(name="pump only", value="up"),
            app_commands.Choice(name="dump only", value="down"),
        ]
    )
    async def mc_move(
        self,
        inter: discord.Interaction,
        ca: str,
        percent: float,
        window: str = "1h",
        direction: Optional[app_commands.Choice[str]] = None,
        cooldown: Optional[str] = None,
        note: Optional[str] = None,
    ):
        await inter.response.defer(thinking=True)
        ca = ca.strip()

        if percent <= 0:
            await inter.followup.send("❌ Percent must be greater than zero.")
            return
        try:
            window_sec = parse_window(window)
        except ValueError as e:
            await inter.followup.send(f"❌ Invalid window: {e}")
            return
        try:
            cooldown_sec = parse_window(cooldown) if cooldown else MOVE_DEFAULT_COOLDOWN
        except ValueError as e:
            await inter.followup.send(f"❌ Invalid cooldown: {e}")
            return

        info = await _lookup(ca)
        if not info:
            await inter.followup.send(f"Couldn't find pairs for `{ca}`.")
            return

        await update_cache(
            ca, mc=info["mc"], url=info["url"], source=info["src"], dex=info["dex"],
            chain=info["chain"], quote=info["quote"], consensus=info["consensus"],
            image_url=info["image"],
        )
        history.record(ca, info["mc"], time.time())

        dir_val = direction.value if direction else "both"
        mv = MoveAlert(
            ca=ca, pct=float(percent), window_sec=window_sec, direction=dir_val,
            channel_id=inter.channel_id, creator_id=inter.user.id,
            guild_id=inter.guild_id or 0, name=info["name"], symbol=info["symbol"],
            note=(note or "").strip(), cooldown_sec=cooldown_sec,
        )
        move_alerts.append(mv)
        await save_moves()

        # Seed from historical candles so the alert is live now rather than
        # half a window from now.
        armed = False
        try:
            if info["mc"]:
                await gecko.backfill(ca, window_sec, info["mc"])
                armed = history.pct_change(ca, window_sec, time.time()) is not None
        except Exception:
            log.debug("Backfill failed for %s", ca, exc_info=True)

        label = {"up": "pump", "down": "dump", "both": "move"}[dir_val]
        now = f" (now {usd(info['mc'])})" if info["mc"] else ""
        readiness = (
            "✅ Armed now"
            if armed
            else f"Warming up: needs about {human_window(window_sec // 2)} of history first"
        )
        await inter.followup.send(
            f"📊 **{info['symbol'] or info['name']}** momentum alert set: fires on a "
            f"**{pct(percent, signed=False)}** {label} within **{human_window(window_sec)}**{now}\n"
            + footer(f"`{mv.id}`", f"re-arms after {human_window(cooldown_sec)}", "note saved" if note else "")
            + f"\n{readiness}"
        )

    # ---------------- /mc_list ----------------

    @app_commands.command(name="mc_list", description="List active alerts")
    @app_commands.describe(
        user="Only show alerts created by this user",
        public="Show to everyone (True) or only you (False)",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def mc_list(self, inter: discord.Interaction, user: Optional[discord.User] = None, public: bool = True):
        await inter.response.defer(thinking=True, ephemeral=not public)

        sr = self._scoped(inter)
        mv = self._scoped_moves(inter)
        if user:
            sr = [r for r in sr if r.creator_id == user.id]
            mv = [m for m in mv if m.creator_id == user.id]

        where = "in this server" if inter.guild_id else "on your account"
        if not sr and not mv:
            await inter.followup.send(
                f"No active alerts {where}." + (f" None by {user.display_name}." if user else ""),
                ephemeral=not public,
            )
            return

        async with TOKEN_CACHE_LOCK:
            snap = {x.ca: token_cache.get(x.ca) for x in (*sr, *mv)}

        uids = {x.creator_id for x in (*sr, *mv)}
        names = {uid: await username_from_id(self.bot, uid) for uid in uids}

        embed = discord.Embed(title=footer("Alerts", user.display_name if user else ""), color=NEUTRAL)

        # Indices are only offered inside a server. From a user installation the
        # listing spans every server, while /mc_remove run in a server numbers
        # just that one — so the same number would mean different alerts in the
        # two places. Ids are unambiguous everywhere, so DMs get ids only.
        numbered = bool(inter.guild_id)
        pos = {r.id: i for i, r in enumerate(self._scoped(inter), 1)} if numbered else {}
        headers = (["#"] if numbered else []) + ["ID", "Token", "Target", "Now", "By"]
        aligns = (["r"] if numbered else []) + ["l", "l", "r", "r", "l"]
        rows_ge, rows_le = [], []
        unchecked = no_data = 0
        for r in sr:
            s = snap.get(r.ca)
            # Both render as the unknown mark; the footer says how many of each
            # there are, so a cold start still reads differently from a dead token.
            if s is None:
                unchecked += 1
            elif s.mc is None:
                no_data += 1
            curr = usd(s.mc) if s is not None and s.mc is not None else UNKNOWN
            tgt = usd(r.target_mc) + (f" ({r.spec})" if r.spec else "")
            row = ([str(pos[r.id])] if numbered else []) + [
                r.id, r.symbol or r.name, tgt, curr, names.get(r.creator_id, UNKNOWN)
            ]
            (rows_ge if r.direction == "above" else rows_le).append(row)

        # Tables are split across fields: Discord rejects any single field over
        # 1024 chars with a 400, which used to fail /mc_list outright once a
        # server had roughly 18 alerts in one direction.
        hidden = 0
        if rows_ge:
            shown, total = add_table_fields(embed, "📈 Breakouts", headers, rows_ge, aligns, max_fields=4)
            hidden += total - shown
        if rows_le:
            shown, total = add_table_fields(embed, "📉 Pullbacks", headers, rows_le, aligns, max_fields=3)
            hidden += total - shown

        if mv:
            mheaders = ["ID", "Token", "Move", "Window", "Now", "By"]
            maligns = ["l", "l", "r", "r", "r", "l"]
            mrows = []
            for m in mv:
                s = snap.get(m.ca)
                mrows.append([
                    m.id, m.symbol or m.name, f"{ARROWS[m.direction]}{pct(m.pct, signed=False)}",
                    human_window(m.window_sec),
                    usd(s.mc) if s is not None and s.mc is not None else UNKNOWN,
                    names.get(m.creator_id, UNKNOWN),
                ])
            shown, total = add_table_fields(embed, "📊 Momentum", mheaders, mrows, maligns, max_fields=3)
            hidden += total - shown

        embed.set_footer(text=footer(
            f"{len(sr)} level + {plural(len(mv), 'momentum alert')}",
            f"{unchecked} not checked yet" if unchecked else "",
            f"{no_data} with no market cap" if no_data else "",
            "/mc_remove to delete",
            f"{plural(hidden, 'row')} not shown (filter with user:)" if hidden else "",
        ))
        await inter.followup.send(embed=embed, ephemeral=not public)

    # ---------------- /mc_remove ----------------

    async def _remove_autocomplete(self, inter: discord.Interaction, current: str):
        q = (current or "").lower().strip()
        can_manage = self._can_manage(inter.user)
        out = []
        for r in self._scoped(inter):
            if not self._may_remove(inter.user, r.creator_id, can_manage):
                continue
            label = f"{_level_label(r)} ({r.id})"
            if q and q not in label.lower() and q not in r.ca.lower():
                continue
            out.append(app_commands.Choice(name=label[:100], value=r.id))
        for m in self._scoped_moves(inter):
            if not self._may_remove(inter.user, m.creator_id, can_manage):
                continue
            label = f"{_move_label(m)} ({m.id})"
            if q and q not in label.lower() and q not in m.ca.lower():
                continue
            out.append(app_commands.Choice(name=label[:100], value=m.id))
        return out[:25]

    @app_commands.command(name="mc_remove", description="Remove alerts (pick from the list, or pass ids)")
    @app_commands.describe(alerts="Alert ids or /mc_list numbers, e.g. 'a1b2c3' or '1 3 5'")
    @app_commands.autocomplete(alerts=_remove_autocomplete)
    # /mc_list works from a user installation and points people here, so this has
    # to be reachable in the same places. Unlike /mc and /mc_move it posts
    # nothing later, so there is no delivery problem.
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def mc_remove(self, inter: discord.Interaction, alerts: str):
        await inter.response.defer(thinking=False)

        scoped = self._scoped(inter)
        scoped_moves = self._scoped_moves(inter)
        if not scoped and not scoped_moves:
            await inter.followup.send("There are no active alerts here.")
            return

        # Move alerts are id-only (they aren't numbered in /mc_list).
        move_by_id = {m.id: m for m in scoped_moves}
        tokens = [t for t in re.split(r"[\s,]+", alerts.strip()) if t]

        move_hits, seen_moves = [], set()
        for t in tokens:
            mid = t.lower()
            if mid in move_by_id and mid not in seen_moves:
                seen_moves.add(mid)
                move_hits.append(move_by_id[mid])
        remaining = " ".join(t for t in tokens if t.lower() not in move_by_id)

        scope = "in this server" if inter.guild_id else "on your account"
        if remaining.strip():
            picked, parse_errs = resolve_targets(remaining, scoped, scope)
        elif move_hits:
            picked, parse_errs = [], []
        else:
            # All-whitespace or all-punctuation input reached here with an empty
            # error list, producing a bare "Nothing to remove:" and no reason.
            picked, parse_errs = resolve_targets(alerts, scoped, scope)

        if not picked and not move_hits:
            reason = "\n".join(f"- {e}" for e in parse_errs) or "- No alerts specified."
            await inter.followup.send(f"❌ Nothing to remove:\n{reason}")
            return

        can_manage = self._can_manage(inter.user)
        removed, denied = [], []

        for rem in picked:
            if not self._may_remove(inter.user, rem.creator_id, can_manage):
                denied.append(rem)
                continue
            try:
                reminders.remove(rem)
            except ValueError:
                parse_errs.append(f"`{rem.id}` already fired or was removed.")
                continue
            removed.append(("level", rem))

        for mv in move_hits:
            if not self._may_remove(inter.user, mv.creator_id, can_manage):
                denied.append(mv)
                continue
            try:
                move_alerts.remove(mv)
            except ValueError:
                continue
            removed.append(("move", mv))

        if any(k == "level" for k, _ in removed):
            await save_reminders()
        if any(k == "move" for k, _ in removed):
            await save_moves()

        lines = []
        if removed:
            lines.append(f"🗑️ **Removed {plural(len(removed), 'alert')}**")
            for kind, x in removed:
                lines.append(f"- `{x.id}` {_move_label(x) if kind == 'move' else _level_label(x)}")
        else:
            lines.append("No alerts removed.")
        if denied:
            lines.append(
                "\n🔒 **Not yours to remove:** " + ", ".join(f"`{x.id}`" for x in denied)
                + " (only the creator or someone with **Manage Server** can)."
            )
        if parse_errs:
            lines.append("\n⚠️ **Not understood:**")
            lines += [f"- {e}" for e in parse_errs]
        await inter.followup.send(fit_lines(lines, MESSAGE_LIMIT))

    # ---------------- /mc_recent ----------------

    @app_commands.command(name="mc_recent", description="Show recently fired alerts")
    @app_commands.describe(
        count="How many to show (default 5, max 50)",
        user="Only alerts created by this user",
        public="Show to everyone (True) or only you (False)",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def mc_recent(
        self,
        inter: discord.Interaction,
        count: Optional[int] = 5,
        user: Optional[discord.User] = None,
        public: Optional[bool] = True,
    ):
        await inter.response.defer(thinking=True, ephemeral=not public)
        if inter.guild_id:
            evs = [e for e in alert_events if e.guild_id == inter.guild_id]
        else:
            evs = [e for e in alert_events if e.creator_id == inter.user.id]
        if user:
            evs = [e for e in evs if e.creator_id == user.id]
        evs.sort(key=lambda e: e.ts, reverse=True)
        evs = evs[: max(1, min(int(count or 5), 50))]
        if not evs:
            await inter.followup.send("No alerts have fired here yet.", ephemeral=not public)
            return

        name_by_id = {uid: await username_from_id(self.bot, uid) for uid in {e.creator_id for e in evs}}

        # The market cap at the moment it fired is on the event itself.
        lines = []
        for e in evs:
            if getattr(e, "kind", "level") == "move":
                up = e.direction == "up"
                what = f"{'📈' if up else '📉'} **{e.symbol or e.name}** {'+' if up else '-'}{pct(e.target_mc, signed=False)} move"
                at = f"at {usd(e.current_mc)}" if e.current_mc is not None else ""
            else:
                up = e.direction == "above"
                what = f"{'📈' if up else '📉'} **{e.symbol or e.name}** {'≥' if up else '≤'} {usd(e.target_mc)}"
                at = f"fired at {usd(e.current_mc)}" if e.current_mc is not None else ""
            lines.append(footer(what, at, when(e.ts), name_by_id.get(e.creator_id, UNKNOWN)))

        embed = discord.Embed(
            title=footer("Recent alerts", user.display_name if user else ""),
            description=fit_lines(lines, 4000),
            color=NEUTRAL,
        )
        await inter.followup.send(embed=embed, ephemeral=not public)

    # ---------------- /mc_status ----------------

    @app_commands.command(name="mc_status", description="What the watcher is doing right now")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def mc_status(self, inter: discord.Interaction):
        await inter.response.defer(thinking=True, ephemeral=True)

        addresses = list({r.ca for r in reminders} | {m.ca for m in move_alerts})
        async with TOKEN_CACHE_LOCK:
            mc_all = {ca: (token_cache[ca].mc if ca in token_cache else None) for ca in addresses}

        tiers = describe_tiers(reminders, mc_all)
        order_levels, order_moves = order_watchers()
        rate = estimated_requests_per_minute([*reminders, *order_levels], [*move_alerts, *order_moves], mc_all, watcher_no_data)
        warming = sum(1 for m in move_alerts if history.pct_change(m.ca, m.window_sec, time.time()) is None)
        dead = sum(1 for ca in addresses if watcher_no_data.get(ca, 0) > 0)
        backed_off = sum(1 for ca in addresses if watcher_no_data.get(ca, 0) >= NO_DATA_GIVE_UP)
        here = "in this server" if inter.guild_id else "on your account"

        lines = [
            f"**{len(reminders)}** level + **{len(move_alerts)}** momentum alerts over "
            f"**{len(addresses)}** tokens{SEP}≈ **{rate:.0f}** requests/min of 300",
            footer(
                f"Level alerts: 🔥 {tiers['hot']} near target",
                f"🌤 {tiers['warm']} warm", f"🧊 {tiers['cold']} cold", f"❔ {tiers['unknown']} no data",
            ),
            footer(
                f"{plural(warming, 'momentum alert')} still filling their window",
                f"{plural(len(self._scoped(inter)), 'level alert')} {here}",
                f"{plural(len(live_orders()), 'auto-order')} armed" if live_orders() else "",
            ),
        ]
        if dead:
            # These can never fire, so say so plainly rather than leaving the
            # owner to wonder why a third of the list shows the unknown mark.
            lines.append(
                f"⚠️ {plural(dead, 'token')} return no market cap so their alerts cannot fire "
                f"({backed_off} backed off). They show as {UNKNOWN} in /mc_list; /mc_remove clears them."
            )
        embed = discord.Embed(title="Watcher status", color=NEUTRAL, description="\n".join(lines))
        await inter.followup.send(embed=embed, ephemeral=True)

    # ---------------- clear ----------------

    @app_commands.command(name="mc_clear", description="Remove every alert in this server (server managers only)")
    @app_commands.guild_only()
    async def mc_clear(self, inter: discord.Interaction):
        """Start fresh. Confirm-gated and limited to people who can manage the
        server, since it removes everyone's alerts here, not just the caller's."""
        if not self._can_manage(inter.user):
            await inter.response.send_message("🔒 Only server managers can clear every alert.", ephemeral=True)
            return
        levels = [r for r in reminders if r.guild_id == inter.guild_id]
        moves = [m for m in move_alerts if m.guild_id == inter.guild_id]
        if not levels and not moves:
            await inter.response.send_message("No active alerts here.", ephemeral=True)
            return
        view = ConfirmOrder(inter.user.id, 60)
        await inter.response.send_message(
            f"Remove **{plural(len(levels), 'level alert')}** and **{plural(len(moves), 'momentum alert')}** "
            f"from this server? This cannot be undone.", view=view, ephemeral=True,
        )
        await view.wait()
        if not view.value:
            await inter.followup.send("Cancelled. Nothing was removed.", ephemeral=True)
            return
        reminders[:] = [r for r in reminders if r.guild_id != inter.guild_id]
        move_alerts[:] = [m for m in move_alerts if m.guild_id != inter.guild_id]
        await save_reminders()
        await save_moves()
        log.info("User %s cleared %d level + %d move alert(s) in guild %s", inter.user.id, len(levels), len(moves), inter.guild_id)
        await inter.followup.send(
            f"🧹 Cleared {len(levels)} level and {plural(len(moves), 'momentum alert')}. /mc_list is empty here."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AlertsCog(bot))
