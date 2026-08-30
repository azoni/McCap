"""Daily spend ledger for Robinhood trading.

Persisted to the volume rather than held in memory: an in-memory counter would
reset on every deploy, and "the daily cap resets whenever the bot restarts" is
not a cap. McCap redeploys several times a day.

Money moved, so this errs toward refusing: an unreadable ledger blocks trading
instead of silently starting the day's budget over.
"""

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Tuple

from .config import RH_MAX_DAILY_USD, RH_MAX_TRADE_USD, RH_SPEND_FILE
from .logging_setup import log


def _today(now: float = None) -> str:
    ts = now if now is not None else time.time()
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _load() -> Dict:
    try:
        with open(RH_SPEND_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # Unreadable: report it and let the caller fail closed.
        log.exception("Could not read the spend ledger at %s", RH_SPEND_FILE)
        raise


def _save(data: Dict) -> None:
    target = Path(RH_SPEND_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=target.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def spent_today(now: float = None) -> float:
    """Dollars already committed today (UTC)."""
    data = _load()
    return float(data.get(_today(now), 0.0))


def check(usd: float, now: float = None) -> Tuple[bool, str]:
    """Whether an order of this size is allowed. Returns (ok, reason).

    Checked BEFORE the order is sent, and re-checked by the caller after the
    user confirms — a confirmation button can sit unclicked while other trades
    land.
    """
    if usd <= 0:
        return False, "Amount must be greater than zero."
    if usd > RH_MAX_TRADE_USD:
        return False, (
            f"${usd:,.2f} exceeds the per-trade cap of ${RH_MAX_TRADE_USD:,.2f}."
        )
    try:
        already = spent_today(now)
    except Exception:
        return False, "The spend ledger is unreadable, so trading is blocked."
    if already + usd > RH_MAX_DAILY_USD:
        return False, (
            f"${usd:,.2f} would take today's total to ${already + usd:,.2f}, "
            f"over the daily cap of ${RH_MAX_DAILY_USD:,.2f} "
            f"(${already:,.2f} already spent)."
        )
    return True, ""


def record(usd: float, now: float = None) -> float:
    """Commit spend against today's budget. Returns the new total."""
    data = _load()
    key = _today(now)
    total = float(data.get(key, 0.0)) + float(usd)
    data[key] = round(total, 2)
    # Keep a short history; the file should not grow forever.
    for old in sorted(data)[:-30]:
        data.pop(old, None)
    _save(data)
    return total


def remaining(now: float = None) -> float:
    try:
        return max(0.0, RH_MAX_DAILY_USD - spent_today(now))
    except Exception:
        return 0.0
