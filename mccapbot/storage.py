import asyncio
import json
import os
import shutil
import tempfile
import time
from dataclasses import MISSING, asdict, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, TypeVar

from types import SimpleNamespace

from .config import (
    ALERTS_FILE,
    CHAT_HISTORY_FILE,
    CHAT_MEMORY_FILE,
    DATA_DIR,
    MAX_ALERT_EVENTS,
    MAX_SCAN_EVENTS,
    MOVES_FILE,
    REM_FILE,
    RHC_ORDERS_FILE,
    SCANS_FILE,
    WATCH_FILE,
)
from .logging_setup import log
from .models import AlertEvent, AutoOrder, ChatTurn, MemoryNote, MoveAlert, Reminder, ScanEvent, WatchItem

# In-memory
reminders: List[Reminder] = []
move_alerts: List[MoveAlert] = []
watchlist: List[WatchItem] = []
alert_events: List[AlertEvent] = []
scan_events: List[ScanEvent] = []
memory_notes: List[MemoryNote] = []
chat_turns: List[ChatTurn] = []
auto_orders: List[AutoOrder] = []

# Locks
REM_LOCK = asyncio.Lock()
MOVE_LOCK = asyncio.Lock()
WATCH_LOCK = asyncio.Lock()
ALERTS_LOCK = asyncio.Lock()

T = TypeVar("T")


def _ensure_data_dir() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        log.exception("Could not create DATA_DIR %s", DATA_DIR)


def _atomic_write(path: str, payload: Any) -> None:
    """Write JSON via temp-file + rename.

    A plain ``open(path, "w")`` truncates first, so a crash or container stop
    mid-write left a truncated file and lost every alert. ``os.replace`` is
    atomic on both POSIX and Windows.
    """
    _ensure_data_dir()
    target = Path(path)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _coerce(cls: Type[T], raw: Dict[str, Any]) -> T:
    """Build a dataclass from a dict, ignoring unknown keys.

    Lets us add fields (like ``Reminder.id``) without breaking existing files.

    A falsy ``id`` is dropped rather than passed through: only *absent* keys fall
    back to ``default_factory``, so an explicit ``"id": null`` or ``""`` in the
    JSON would survive into the object. Removal is keyed on id, so two records
    sharing a blank one could take out the wrong alert.
    """
    spec = fields(cls)  # type: ignore[arg-type]
    known = {f.name for f in spec}
    # Only *absent* keys fall back to a default_factory, so an explicit null or
    # "" in the JSON would survive into the object — a blank id breaks
    # remove-by-id, and a null timestamp crashes the sort at startup.
    generated = {f.name for f in spec if f.default_factory is not MISSING}  # type: ignore[misc]
    clean = {
        k: v for k, v in raw.items()
        if k in known and not (k in generated and not v)
    }
    return cls(**clean)  # type: ignore[call-arg]


async def _load_list(path: str, cls: Type[T], target: List[T], label: str) -> bool:
    """Load a JSON array of dataclasses. Returns True if the file existed.

    Parses into a local list and only publishes on success. The previous version
    cleared the shared list first and appended one record at a time, so a single
    unparseable entry left the list holding just the records before it — and the
    next save (the first alert to fire) wrote that truncation back to disk,
    destroying the rest permanently. Records that fail individually are skipped
    and counted rather than aborting the whole load.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            log.error("%s is not a JSON array; refusing to load it.", path)
            return False

        parsed: List[T] = []
        skipped = 0
        for it in data:
            try:
                parsed.append(_coerce(cls, it))
            except Exception:
                skipped += 1
                log.warning("Skipping unreadable %s record: %r", label, it)

        target[:] = parsed
        if skipped:
            # Keep the damaged original: the next save would otherwise overwrite
            # it with the survivors and make the loss permanent.
            _backup_corrupt(path)
            log.error(
                "Loaded %d %s but SKIPPED %d unreadable record(s). Original kept as %s.corrupt",
                len(parsed), label, skipped, path,
            )
        else:
            log.info("Loaded %d %s", len(parsed), label)
        return True
    except FileNotFoundError:
        log.info("No %s file at %s; starting fresh.", label, path)
    except Exception:
        # A wholly unreadable file must not silently become an empty one.
        log.exception("Failed to load %s; leaving in-memory state untouched", path)
        _backup_corrupt(path)
    return False


def _backup_corrupt(path: str) -> None:
    """Preserve a file we could not fully read, before anything overwrites it."""
    try:
        src = Path(path)
        if src.exists():
            shutil.copy2(src, src.with_suffix(src.suffix + ".corrupt"))
    except Exception:
        log.debug("Could not back up %s", path, exc_info=True)


# ---- Reminders (level alerts) ----
async def save_reminders() -> None:
    async with REM_LOCK:
        _atomic_write(REM_FILE, [asdict(r) for r in reminders])


async def load_reminders() -> None:
    try:
        with open(REM_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        parsed, backfilled, skipped = [], 0, 0
        for it in data:
            try:
                r = _coerce(Reminder, it)
            except Exception:
                skipped += 1
                log.warning("Skipping unreadable reminder record: %r", it)
                continue
            if not it.get("id"):
                backfilled += 1
            if not it.get("created_ts"):
                r.created_ts = time.time()
            parsed.append(r)
        # Publish only after the whole file parses; a partial list written back
        # by the next save would destroy every record after the bad one.
        reminders[:] = parsed
        if skipped:
            _backup_corrupt(REM_FILE)
            log.error("Skipped %d unreadable reminder(s); original kept as %s.corrupt", skipped, REM_FILE)
        log.info("Loaded %d reminder(s) from %s", len(reminders), REM_FILE)
        if backfilled:
            log.info("Backfilled ids for %d legacy reminder(s)", backfilled)
            await save_reminders()
    except FileNotFoundError:
        log.info("No reminders file at %s; starting fresh.", REM_FILE)
    except Exception:
        log.exception("Failed to load %s", REM_FILE)


def find_reminder(alert_id: str, guild_id: int) -> Optional[Reminder]:
    """Look up an alert by id within a guild. Returns None if it already fired."""
    for r in reminders:
        if r.id == alert_id and r.guild_id == guild_id:
            return r
    return None


# ---- Move alerts ----
async def save_moves() -> None:
    async with MOVE_LOCK:
        _atomic_write(MOVES_FILE, [asdict(m) for m in move_alerts])


async def load_moves() -> None:
    await _load_list(MOVES_FILE, MoveAlert, move_alerts, "move alert(s)")


# ---- Watchlists ----
async def save_watchlist() -> None:
    async with WATCH_LOCK:
        _atomic_write(WATCH_FILE, [asdict(w) for w in watchlist])


async def load_watchlist() -> None:
    await _load_list(WATCH_FILE, WatchItem, watchlist, "watchlist entry/entries")


# ---- Alert history ----
async def save_alerts() -> None:
    async with ALERTS_LOCK:
        _atomic_write(ALERTS_FILE, [asdict(a) for a in alert_events])


async def load_alerts() -> None:
    if await _load_list(ALERTS_FILE, AlertEvent, alert_events, "alert event(s)"):
        alert_events[:] = sorted(alert_events, key=lambda x: x.ts, reverse=True)[:MAX_ALERT_EVENTS]


# ---- Scan events (scanner-bot detections) ----
SCANS_LOCK = asyncio.Lock()


async def save_scans() -> None:
    async with SCANS_LOCK:
        _atomic_write(SCANS_FILE, [asdict(s) for s in scan_events])


async def load_scans() -> None:
    if await _load_list(SCANS_FILE, ScanEvent, scan_events, "scan event(s)"):
        scan_events[:] = sorted(scan_events, key=lambda x: x.ts, reverse=True)[:MAX_SCAN_EVENTS]


def recent_scan(ca: str, guild_id: int, within_sec: float, now: float) -> Optional[ScanEvent]:
    """A prior scan of this token in this guild inside the dedupe window.

    Scanner channels re-scan the same token constantly; without this the history
    fills with duplicates and the performance report double-counts a single call.
    """
    for s in scan_events:
        if s.ca == ca and s.guild_id == guild_id and (now - s.ts) <= within_sec:
            return s
    return None


def scans_to_track(track_seconds: float, now: float) -> List[ScanEvent]:
    """Scans still inside their tracking window, newest first."""
    return [s for s in scan_events if (now - s.ts) <= track_seconds]


def watched_addresses() -> List[str]:
    """Every contract address the watcher needs to poll: alerts and armed auto-orders."""
    return list({r.ca for r in reminders} | {m.ca for m in move_alerts} | {o.ca for o in live_orders()})


# ---- Auto-orders (take-profit / stop-loss sells, one-shot buys) ----
ORDERS_LOCK = asyncio.Lock()

# An armed order's token needs a steady price feed however far it sits from its
# target: a stop-loss in the watcher's 300s cold tier would see a dump five
# minutes late. This window makes interval_for_move land on its 60s floor.
ORDER_POLL_WINDOW_SEC = 720


async def save_orders() -> None:
    async with ORDERS_LOCK:
        _atomic_write(RHC_ORDERS_FILE, [asdict(o) for o in auto_orders])


async def load_orders() -> None:
    await _load_list(RHC_ORDERS_FILE, AutoOrder, auto_orders, "auto order(s)")


def cache_snapshot(ca: str):
    """The watcher's latest TokenSnapshot for an address, or None. Read without
    the cache lock: a torn read here only affects a display line."""
    from .cache import token_cache
    return token_cache.get(ca)


def live_orders() -> List[AutoOrder]:
    """Orders that still need a price feed: armed or mid-fire."""
    return [o for o in auto_orders if o.status in ("armed", "firing")]


def orders_for(user_id: int) -> List[AutoOrder]:
    return [o for o in auto_orders if o.user_id == user_id]


def find_order(order_id: str, user_id: int) -> Optional[AutoOrder]:
    """One of this user's orders by id, or None; never someone else's."""
    for o in auto_orders:
        if o.id == order_id and o.user_id == user_id:
            return o
    return None


def order_watchers():
    """(level_proxies, move_proxies): what the polling scheduler should treat
    the armed orders as. Every live order yields a move-like proxy (steady 60s
    sampling, never backed off); market-cap orders also yield a level-like
    proxy so the hot tier kicks in within 15% of the target. Keeps one price
    path for alerts and orders, and puts the orders in the request-rate log."""
    levels, moves = [], []
    for o in live_orders():
        moves.append(SimpleNamespace(ca=o.ca, window_sec=ORDER_POLL_WINDOW_SEC, last_fired_ts=0.0, cooldown_sec=0))
        if o.metric == "mc":
            levels.append(SimpleNamespace(ca=o.ca, direction=o.direction, target_mc=o.target))
    return levels, moves


# ---- Chat memory (long-term notes) and rolling conversation ----
MEMORY_LOCK = asyncio.Lock()
CHAT_HISTORY_LOCK = asyncio.Lock()


async def save_memory() -> None:
    async with MEMORY_LOCK:
        _atomic_write(CHAT_MEMORY_FILE, [asdict(n) for n in memory_notes])


async def load_memory() -> None:
    await _load_list(CHAT_MEMORY_FILE, MemoryNote, memory_notes, "memory note(s)")


async def save_chat_history() -> None:
    async with CHAT_HISTORY_LOCK:
        _atomic_write(CHAT_HISTORY_FILE, [asdict(t) for t in chat_turns])


async def load_chat_history() -> None:
    await _load_list(CHAT_HISTORY_FILE, ChatTurn, chat_turns, "chat turn(s)")
