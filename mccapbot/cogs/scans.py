"""React to scanner-bot posts (Rick and friends) and score the calls.

Detection needs the MESSAGE_CONTENT privileged intent — without it Discord
delivers empty embeds/components for other bots' messages and this cog can see
nothing at all. That is a Developer Portal toggle, so the cog checks at startup
and warns loudly rather than sitting there looking healthy while doing nothing.

The valuable part is not detection but scoring: every detection records the
market cap at scan time, and a background loop tracks each token afterwards to
find its peak. /scans report then answers the question no scanner bot answers —
were the calls any good?
"""

import asyncio
import time
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from .. import scan
from ..config import (
    SCAN_AUTO_MOVE_MAX,
    SCAN_AUTO_MOVE_PCT,
    SCAN_AUTO_MOVE_TTL,
    SCAN_AUTO_MOVE_WINDOW,
    SCAN_AUTO_WATCHLIST,
    SCAN_AUTO_WATCHLIST_NAME,
    SCAN_CHANNEL_IDS,
    SCAN_DEDUPE_SECONDS,
    SCAN_OPINION_MIN_SECONDS,
    SCAN_POST_OPINION,
    SCAN_TRACK_HOURS,
    SCAN_TRACK_INTERVAL,
    SCAN_WATCH_ENABLE,
    SCANNER_BOT_IDS,
)
from ..dex import token_summary
from ..helpers import humanize, short_ca
from ..logging_setup import log
from ..models import MoveAlert, ScanEvent, WatchItem
from ..storage import (
    move_alerts,
    recent_scan,
    save_moves,
    save_scans,
    save_watchlist,
    scan_events,
    scans_to_track,
    watchlist,
)
from ..tables import add_table_fields


def _fmt_mult(m: Optional[float]) -> str:
    return "—" if m is None else f"{m:.2f}x"


def _median(vals: List[float]) -> float:
    s = sorted(vals)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


class ScansCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_opinion: Dict[int, float] = {}   # channel_id -> loop clock
        self._tracker: Optional[asyncio.Task] = None

    # ---------------- lifecycle ----------------

    @commands.Cog.listener()
    async def on_ready(self):
        if not SCAN_WATCH_ENABLE:
            log.info("Scan watching disabled (SCAN_WATCH_ENABLE=0).")
            return

        # The most likely way this feature "breaks": the intent is off, so
        # nothing ever matches and there is no error anywhere to look at.
        if not self.bot.intents.message_content:
            log.warning(
                "SCAN WATCHING IS INERT: the MESSAGE_CONTENT intent is disabled, so other "
                "bots' embeds arrive empty. Enable it in the Discord Developer Portal "
                "(Bot > Privileged Gateway Intents) - no Discord approval is required."
            )
        else:
            log.info(
                "Scan watching active | scanners=%s | channels=%s",
                sorted(SCANNER_BOT_IDS) or "any bot",
                sorted(SCAN_CHANNEL_IDS) or "all",
            )

        if self._tracker is None:
            self._tracker = asyncio.create_task(self._track_loop(), name="scan-tracker")

    def cog_unload(self):
        if self._tracker and not self._tracker.done():
            self._tracker.cancel()

    # ---------------- detection ----------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not SCAN_WATCH_ENABLE or message.guild is None:
            return
        if SCAN_CHANNEL_IDS and message.channel.id not in SCAN_CHANNEL_IDS:
            return

        self_id = self.bot.user.id if self.bot.user else None
        if not scan.is_scanner_message(message, SCANNER_BOT_IDS, self_id):
            return

        mints = scan.extract_mints(message)
        if not mints:
            # Only worth flagging when we were told this bot posts scans;
            # otherwise it is ordinary chatter from an unrelated bot.
            if SCANNER_BOT_IDS:
                surfaces = scan.scan_surfaces(message)
                log.warning(
                    "Scanner %s posted a message with no extractable mint (%s) - "
                    "its format may have changed.",
                    message.author.id, surfaces.describe(),
                )
            return

        try:
            await self._handle(message, mints)
        except Exception:
            log.exception("Failed handling scan from %s", message.author.id)

    async def _handle(self, message: discord.Message, mints: List[str]) -> None:
        now = time.time()
        gid = message.guild.id

        # Confirm a candidate is a real token before acting. The first mint that
        # resolves wins, which is why extract_mints ranks chart-link mints first.
        summary = None
        ca = None
        for candidate in mints[:3]:
            summary = await token_summary(candidate)
            if summary:
                ca = candidate
                break
        if not summary or not ca:
            log.debug("No candidate from %s resolved to a token: %s", message.author.id, mints[:3])
            return

        if recent_scan(ca, gid, SCAN_DEDUPE_SECONDS, now):
            log.debug("Skipping duplicate scan of %s in guild %s", short_ca(ca), gid)
            return

        ev = ScanEvent(
            ca=ca,
            guild_id=gid,
            channel_id=message.channel.id,
            scanner_id=message.author.id,
            name=summary["name"],
            symbol=summary["symbol"],
            mc_at_scan=summary["mc"],
            message_id=message.id,
            requested_by=self._who_asked(message),
            ts=now,
            last_mc=summary["mc"],
            peak_mc=summary["mc"],
            peak_ts=now,
            last_checked_ts=now,
        )
        scan_events.insert(0, ev)
        await save_scans()
        log.info(
            "Scan detected | %s (%s) MC=%s | scanner=%s | id=%s",
            ev.name, ev.symbol, humanize(ev.mc_at_scan), ev.scanner_id, ev.id,
        )

        if SCAN_AUTO_WATCHLIST:
            await self._add_to_watchlist(ev)
        armed = await self._maybe_arm_move(ev)
        if SCAN_POST_OPINION:
            await self._post_opinion(message, ev, summary, armed)

    @staticmethod
    def _who_asked(message: discord.Message) -> int:
        """Best-effort: the human whose paste triggered the scan.

        Scanner bots either reply to the user's message or are invoked as a
        slash command; neither is guaranteed. The report shows a dash when this
        cannot be resolved rather than pretending to know.
        """
        ref = getattr(message, "reference", None)
        resolved = getattr(ref, "resolved", None) if ref else None
        author = getattr(resolved, "author", None)
        if author is not None and getattr(author, "id", None):
            return author.id
        meta = getattr(message, "interaction_metadata", None)
        user = getattr(meta, "user", None) if meta else None
        return getattr(user, "id", 0) or 0

    # ---------------- actions ----------------

    async def _add_to_watchlist(self, ev: ScanEvent) -> None:
        already = any(
            w.ca == ev.ca and w.guild_id == ev.guild_id and w.list_name == SCAN_AUTO_WATCHLIST_NAME
            for w in watchlist
        )
        if already:
            return
        watchlist.append(WatchItem(
            ca=ev.ca,
            guild_id=ev.guild_id,
            added_by=ev.scanner_id,
            name=ev.name,
            symbol=ev.symbol,
            list_name=SCAN_AUTO_WATCHLIST_NAME,
        ))
        await save_watchlist()

    async def _maybe_arm_move(self, ev: ScanEvent) -> Optional[MoveAlert]:
        """Arm a momentum alert, respecting the cap that protects the budget."""
        if SCAN_AUTO_MOVE_PCT <= 0:
            return None
        if any(m.ca == ev.ca and m.auto_expires_ts for m in move_alerts):
            return None  # already auto-armed for this token

        auto_count = sum(1 for m in move_alerts if m.auto_expires_ts)
        if auto_count >= SCAN_AUTO_MOVE_MAX:
            log.info(
                "Not auto-arming %s: already at %d/%d auto alerts (protects the request budget)",
                ev.symbol or ev.ca, auto_count, SCAN_AUTO_MOVE_MAX,
            )
            return None

        m = MoveAlert(
            ca=ev.ca,
            pct=SCAN_AUTO_MOVE_PCT,
            window_sec=SCAN_AUTO_MOVE_WINDOW,
            direction="both",
            channel_id=ev.channel_id,
            creator_id=ev.scanner_id,
            guild_id=ev.guild_id,
            name=ev.name,
            symbol=ev.symbol,
            note=f"auto-armed from scan {ev.id}",
            auto_expires_ts=time.time() + SCAN_AUTO_MOVE_TTL,
        )
        move_alerts.append(m)
        await save_moves()
        return m

    async def _post_opinion(
        self, message: discord.Message, ev: ScanEvent, summary: Dict, armed: Optional[MoveAlert]
    ) -> None:
        now = asyncio.get_running_loop().time()
        if now - self._last_opinion.get(message.channel.id, 0.0) < SCAN_OPINION_MIN_SECONDS:
            return
        self._last_opinion[message.channel.id] = now

        change = summary.get("change24") or 0.0
        embed = discord.Embed(
            title=f"{ev.name} ({ev.symbol})",
            url=summary.get("url"),
            colour=(0x2ECC71 if change >= 0 else 0xE74C3C),
            description=(
                f"**MC** ${humanize(ev.mc_at_scan)}  |  **24h** {change:+.1f}%\n"
                f"**Liquidity** ${humanize(summary.get('liq'))} across "
                f"{summary.get('pools', 0)} pool(s)"
            ),
        )
        if summary.get("image_url"):
            embed.set_thumbnail(url=summary["image_url"])
        foot = f"tracking from ${humanize(ev.mc_at_scan)} | scan {ev.id}"
        if armed:
            foot += f" | auto-alert +/-{armed.pct:g}%"
        embed.set_footer(text=foot)

        try:
            await message.reply(embed=embed, mention_author=False)
        except discord.HTTPException:
            log.debug("Could not reply in channel %s", message.channel.id, exc_info=True)

    # ---------------- performance tracking ----------------

    async def _track_loop(self) -> None:
        await self.bot.wait_until_ready()
        track_seconds = SCAN_TRACK_HOURS * 3600

        while not self.bot.is_closed():
            try:
                await asyncio.sleep(SCAN_TRACK_INTERVAL)
                await self._expire_auto_moves()

                now = time.time()
                live = scans_to_track(track_seconds, now)
                if not live:
                    continue

                # One lookup per distinct token, not one per scan event.
                changed = False
                for ca in {s.ca for s in live}:
                    summary = await token_summary(ca)
                    if not summary or summary.get("mc") is None:
                        continue
                    mc = summary["mc"]
                    for s in live:
                        if s.ca != ca:
                            continue
                        s.last_mc = mc
                        s.last_checked_ts = now
                        if s.peak_mc is None or mc > s.peak_mc:
                            s.peak_mc = mc
                            s.peak_ts = now
                        changed = True
                if changed:
                    await save_scans()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scan tracker loop error")

    async def _expire_auto_moves(self) -> None:
        now = time.time()
        expired = [m for m in move_alerts if m.auto_expires_ts and m.auto_expires_ts <= now]
        if not expired:
            return
        ids = {m.id for m in expired}
        move_alerts[:] = [m for m in move_alerts if m.id not in ids]
        await save_moves()
        log.info("Expired %d auto-armed scan alert(s)", len(expired))

    # ---------------- commands ----------------

    scans = app_commands.Group(
        name="scans",
        description="Scanner-bot detections and how the calls performed",
    )

    @scans.command(name="report", description="How did scanned tokens perform since they were called?")
    @app_commands.describe(
        hours="Look back this many hours (default 24)",
        sort="Rank by peak gain or current gain",
    )
    @app_commands.choices(sort=[
        app_commands.Choice(name="peak gain", value="peak"),
        app_commands.Choice(name="current gain", value="current"),
    ])
    async def report(
        self,
        inter: discord.Interaction,
        hours: Optional[int] = 24,
        sort: Optional[app_commands.Choice[str]] = None,
    ):
        await inter.response.defer(thinking=True)
        hours = max(1, min(int(hours or 24), 24 * 30))
        cutoff = time.time() - hours * 3600
        gid = inter.guild_id or 0

        evs = [s for s in scan_events if s.guild_id == gid and s.ts >= cutoff]
        if not evs:
            note = "" if self.bot.intents.message_content else (
                "\n⚠️ The MESSAGE_CONTENT intent is off, so scans cannot be detected at all."
            )
            await inter.followup.send(f"No scans recorded in the last {hours}h.{note}")
            return

        key = sort.value if sort else "peak"

        def rank(s: ScanEvent) -> float:
            m = s.multiple() if key == "peak" else s.current_multiple()
            return m if m is not None else -1.0

        evs.sort(key=rank, reverse=True)

        rows = [[
            s.symbol or s.name,
            f"${humanize(s.mc_at_scan)}",
            _fmt_mult(s.multiple()),
            _fmt_mult(s.current_multiple()),
        ] for s in evs[:20]]

        scored = [m for m in (s.multiple() for s in evs) if m is not None]
        winners = sum(1 for m in scored if m >= 2.0)
        median = f" | median peak {_median(scored):.2f}x" if scored else ""

        embed = discord.Embed(
            title=f"Scan performance - last {hours}h",
            description=(
                f"**{len(evs)}** scan(s) | **{winners}** hit 2x or better{median}\n"
                "Peak is the best it reached after the call; now is where it stands."
            ),
            colour=0x2B90D9,
        )
        shown, total_rows = add_table_fields(
            embed, "Ranked by " + ("peak" if key == "peak" else "current"),
            ["Token", "At scan", "Peak", "Now"], rows, ["l", "r", "r", "r"], max_fields=4,
        )
        foot = f"Each scan is tracked for {SCAN_TRACK_HOURS}h after detection"
        if shown < total_rows:
            foot += f" · {total_rows - shown} row(s) not shown"
        embed.set_footer(text=foot)
        await inter.followup.send(embed=embed)

    @scans.command(name="recent", description="Most recently detected scans")
    @app_commands.describe(count="How many to show (default 10, max 25)")
    async def recent(self, inter: discord.Interaction, count: Optional[int] = 10):
        await inter.response.defer(thinking=True, ephemeral=True)
        gid = inter.guild_id or 0
        n = max(1, min(int(count or 10), 25))
        evs = [s for s in scan_events if s.guild_id == gid][:n]
        if not evs:
            await inter.followup.send("No scans recorded yet.", ephemeral=True)
            return

        rows = [[
            s.symbol or s.name,
            short_ca(s.ca),
            f"${humanize(s.mc_at_scan)}",
            _fmt_mult(s.current_multiple()),
        ] for s in evs]
        embed = discord.Embed(title="Recent scans", colour=0xF39C12)
        add_table_fields(
            embed, f"{len(evs)} detection(s)",
            ["Token", "CA", "At scan", "Now"], rows, ["l", "l", "r", "r"], max_fields=3,
        )
        await inter.followup.send(embed=embed, ephemeral=True)

    @scans.command(name="status", description="Is scan detection actually working?")
    async def status(self, inter: discord.Interaction):
        await inter.response.defer(thinking=True, ephemeral=True)
        intent_on = bool(self.bot.intents.message_content)
        auto = sum(1 for m in move_alerts if m.auto_expires_ts)
        active = intent_on and SCAN_WATCH_ENABLE
        lines = [
            f"**Detection:** {'active' if active else 'INERT'}",
            f"**MESSAGE_CONTENT intent:** "
            f"{'on' if intent_on else 'OFF - enable it in the Developer Portal'}",
            f"**Watching:** "
            f"{', '.join(str(i) for i in sorted(SCANNER_BOT_IDS)) or 'any bot posting a mint'}",
            f"**Channels:** {', '.join(str(i) for i in sorted(SCAN_CHANNEL_IDS)) or 'all visible'}",
            f"**Scans recorded here:** {len([s for s in scan_events if s.guild_id == (inter.guild_id or 0)])}",
            f"**Auto-armed alerts:** {auto}/{SCAN_AUTO_MOVE_MAX}",
            f"**Actions:** watchlist={'on' if SCAN_AUTO_WATCHLIST else 'off'} | "
            f"reply={'on' if SCAN_POST_OPINION else 'off'} | "
            f"auto-alert={('+/-%g%%' % SCAN_AUTO_MOVE_PCT) if SCAN_AUTO_MOVE_PCT > 0 else 'off'}",
        ]
        await inter.followup.send("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ScansCog(bot))
