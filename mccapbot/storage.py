import asyncio
import json
import os
import tempfile
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, TypeVar

from .config import ALERTS_FILE, DATA_DIR, MAX_ALERT_EVENTS, MOVES_FILE, REM_FILE, WATCH_FILE
from .logging_setup import log
from .models import AlertEvent, MoveAlert, Reminder, WatchItem

# In-memory
reminders: List[Reminder] = []
move_alerts: List[MoveAlert] = []
watchlist: List[WatchItem] = []
alert_events: List[AlertEvent] = []

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
            json.dump(payload, f, ensure_ascii=False, indent=2)
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
    """
    known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    return cls(**{k: v for k, v in raw.items() if k in known})  # type: ignore[call-arg]


async def _load_list(path: str, cls: Type[T], target: List[T], label: str) -> bool:
    """Load a JSON array of dataclasses. Returns True if the file existed."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        target.clear()
        for it in data:
            target.append(_coerce(cls, it))
        log.info("Loaded %d %s", len(target), label)
        return True
    except FileNotFoundError:
        log.info("No %s file at %s; starting fresh.", label, path)
    except Exception:
        log.exception("Failed to load %s", path)
    return False


# ---- Reminders (level alerts) ----
async def save_reminders() -> None:
    async with REM_LOCK:
        _atomic_write(REM_FILE, [asdict(r) for r in reminders])


async def load_reminders() -> None:
    try:
        with open(REM_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        reminders.clear()
        backfilled = 0
        for it in data:
            r = _coerce(Reminder, it)
            if not it.get("id"):
                backfilled += 1
            if not it.get("created_ts"):
                r.created_ts = time.time()
            reminders.append(r)
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


def watched_addresses() -> List[str]:
    """Every contract address the watcher needs to poll."""
    return list({r.ca for r in reminders} | {m.ca for m in move_alerts})
