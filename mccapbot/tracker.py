"""Grades every recorded call: scanner-bot detections and the discovery feed's
own posts.

Every ``ScanEvent`` inside its tracking window gets its market cap re-read
every five minutes so the record carries the peak it reached and where it
stands now. ``/scans report`` and ``/rh feed status`` read those numbers.
This used to be a loop inside the scans cog, which is only loaded when scan
watching is on — so with it off (the normal state: it needs a privileged
intent) nothing ever tracked anything. It runs from ``bot.setup_hook`` now,
unconditionally, and also retires auto-armed momentum alerts past their TTL.

What this module must never do:
- read more than ``FEED_TRACK_MAX_TOKENS`` tokens per pass, however many
  events are live (one DexScreener request per distinct token, newest first);
- treat a failed read as a price: a token with no data keeps its last figures;
- run more than one copy: ``bot.setup_hook`` owns the task, no cog starts one.
"""

import asyncio
import time
from typing import List, Optional

from . import alerts, storage
from .config import FEED_TRACK_MAX_TOKENS, SCAN_TRACK_HOURS, SCAN_TRACK_INTERVAL
from .dex import token_summary
from .logging_setup import log


def tokens_to_check(now: float, limit: int = FEED_TRACK_MAX_TOKENS) -> List[str]:
    """Distinct addresses still inside the tracking window, newest event
    first, capped. ``scan_events`` is kept newest-first, so first seen wins."""
    out: List[str] = []
    for s in storage.scans_to_track(SCAN_TRACK_HOURS * 3600, now):
        if s.ca not in out:
            out.append(s.ca)
        if len(out) >= max(1, limit):
            break
    return out


async def tick(now: Optional[float] = None) -> int:
    """One pass. Returns how many tokens were read."""
    await alerts.expire_auto_alerts()
    now = time.time() if now is None else now
    live = storage.scans_to_track(SCAN_TRACK_HOURS * 3600, now)
    if not live:
        return 0
    changed = False
    checked = 0
    for ca in tokens_to_check(now):
        summary = await token_summary(ca)
        checked += 1
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
        await storage.save_scans()
    return checked


async def run(bot=None, interval: int = SCAN_TRACK_INTERVAL) -> None:
    """The loop. With a bot it waits for the gateway and stops when the bot
    closes; without one it runs until cancelled.

    The bot comes first because that is what every caller has: when the delay
    was the first parameter, ``tracker.run(bot)`` put a Bot where the seconds
    go, ``asyncio.sleep`` raised, and because the sleep itself was what failed
    the loop spun at full speed logging the same error. The delay is also
    checked once here rather than trusted every iteration.
    """
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval < 0:
        log.error("Scan tracker started with a bad interval %r; using %ss instead.", interval, SCAN_TRACK_INTERVAL)
        interval = SCAN_TRACK_INTERVAL
    if bot is not None:
        await bot.wait_until_ready()
    while bot is None or not bot.is_closed():
        try:
            await asyncio.sleep(interval)
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scan tracker loop error")
            await asyncio.sleep(min(interval, 60) or 1)   # never spin on a failure
