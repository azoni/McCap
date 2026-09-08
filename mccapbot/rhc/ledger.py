"""Per-user daily spend caps and the trade journal for Robinhood Chain.

Same rules as the Robinhood Crypto ledger in ``spend.py``, per user this time:
persisted on the volume (a counter that resets on redeploy is not a cap), and
an unreadable ledger blocks buying rather than starting the day's budget over.
Only buys count. Exits are never capped: a cap limits risk, and selling is how
risk is reduced.

The journal is append-only and is the record of what actually happened: a
``submitted`` entry the moment a transaction is broadcast, then a resolution
(``confirmed`` / ``reverted`` / ``dropped``). Startup reads it back to rebuild
the list of transactions still unresolved, so a redeploy mid-trade does not
forget that a wallet has money in flight.
"""

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import RHC_JOURNAL_FILE, RHC_LEDGER_FILE, RHC_MAX_DAILY_USD, RHC_MAX_TRADE_USD
from ..logging_setup import log
from ..storage import _backup_corrupt

MAX_JOURNAL = 5000
UNRESOLVED = ("submitted", "pending")
RESOLVED = ("confirmed", "reverted", "dropped")
TRADE_KINDS = ("buy", "sell")


def _today(now: Optional[float] = None) -> str:
    ts = now if now is not None else time.time()
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _read(path: str, default):
    """Read a JSON file. A missing file is the default; a damaged one raises,
    after a copy is kept, so nothing overwrites it on the next write."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        log.exception("Could not read %s", path)
        _backup_corrupt(path)
        raise
    if not isinstance(data, type(default)):
        log.error("%s holds a %s, expected %s; keeping a copy and refusing to use it",
                  path, type(data).__name__, type(default).__name__)
        _backup_corrupt(path)
        raise ValueError(f"{path} has the wrong shape")
    return data


def _write(path: str, data) -> None:
    target = Path(path)
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


# ---------------- spend caps ----------------

def spent_today(user_id: int, now: Optional[float] = None) -> float:
    data = _read(RHC_LEDGER_FILE, {})
    return float((data.get(str(user_id)) or {}).get(_today(now), 0.0))


def check(user_id: int, usd: float, now: Optional[float] = None) -> Tuple[bool, str]:
    """Whether a buy of this size is allowed for this user. Returns (ok, reason).

    Checked before quoting and again after the confirm button: a button can sit
    unclicked while other trades land.
    """
    if usd is None or usd <= 0:
        return False, "Amount must be greater than zero."
    if usd > RHC_MAX_TRADE_USD:
        return False, f"${usd:,.2f} exceeds the per-trade cap of ${RHC_MAX_TRADE_USD:,.2f}."
    try:
        already = spent_today(user_id, now)
    except Exception:
        return False, "The spend ledger is unreadable, so buying is blocked."
    if already + usd > RHC_MAX_DAILY_USD:
        return False, (
            f"${usd:,.2f} would take your total today to ${already + usd:,.2f}, over the daily cap of "
            f"${RHC_MAX_DAILY_USD:,.2f} (${already:,.2f} already spent)."
        )
    return True, ""


def record(user_id: int, usd: float, now: Optional[float] = None) -> float:
    """Reserve a buy against the user's daily budget. Returns the new total."""
    data = _read(RHC_LEDGER_FILE, {})
    mine = data.setdefault(str(user_id), {})
    key = _today(now)
    total = float(mine.get(key, 0.0)) + float(usd)
    mine[key] = round(total, 2)
    for old in sorted(mine)[:-30]:
        mine.pop(old, None)
    _write(RHC_LEDGER_FILE, data)
    return total


def refund(user_id: int, usd: float, now: Optional[float] = None) -> float:
    """Give back a reservation for a buy that definitely did not happen.

    Spend is reserved BEFORE the swap is sent (so two confirm clicks cannot both
    pass the cap) and refunded only on a definite failure. A pending broadcast
    keeps its reservation: the money probably moved.
    """
    data = _read(RHC_LEDGER_FILE, {})
    mine = data.setdefault(str(user_id), {})
    key = _today(now)
    total = max(0.0, float(mine.get(key, 0.0)) - float(usd))
    mine[key] = round(total, 2)
    _write(RHC_LEDGER_FILE, data)
    return total


def remaining(user_id: int, now: Optional[float] = None) -> float:
    try:
        return max(0.0, RHC_MAX_DAILY_USD - spent_today(user_id, now))
    except Exception:
        return 0.0


# ---------------- journal ----------------

def journal(entry: Dict[str, Any]) -> None:
    """Append one record. Never raises: a journaling failure must not turn a
    completed on-chain transaction into an error message."""
    try:
        data = _read(RHC_JOURNAL_FILE, [])
        data.append(entry)
        if len(data) > MAX_JOURNAL:
            data = data[-MAX_JOURNAL:]
        _write(RHC_JOURNAL_FILE, data)
    except Exception:
        log.exception("Could not journal %r", {k: entry.get(k) for k in ("user_id", "kind", "tx", "status")})


def _all() -> List[Dict[str, Any]]:
    try:
        return _read(RHC_JOURNAL_FILE, [])
    except Exception:
        return []


def entries_for(user_id: int) -> List[Dict[str, Any]]:
    return [e for e in _all() if e.get("user_id") == user_id]


def history(user_id: int) -> List[Dict[str, Any]]:
    """A user's buys, sells and withdrawals, oldest first, each with its FINAL
    status under ``final_status`` (a later resolution entry wins)."""
    entries = entries_for(user_id)
    latest: Dict[str, str] = {}
    for e in entries:
        tx = (e.get("tx") or "").lower()
        if tx:
            latest[tx] = str(e.get("status") or "")
    out = []
    for e in entries:
        if e.get("kind") not in ("buy", "sell", "withdraw"):
            continue
        tx = (e.get("tx") or "").lower()
        # The same trade is journaled twice on the fast path (submitted, then
        # confirmed/reverted); keep the last original record per tx.
        if tx and any((o.get("tx") or "").lower() == tx for o in out):
            out = [o for o in out if (o.get("tx") or "").lower() != tx]
        e = dict(e)
        e["final_status"] = latest.get(tx, e.get("status") or "")
        out.append(e)
    return out


def trades(user_id: int) -> List[Dict[str, Any]]:
    """Confirmed buys and sells only: what the profit figures are built from."""
    return [e for e in history(user_id) if e.get("kind") in ("buy", "sell") and e["final_status"] == "confirmed"]


def final_status(tx_hash: str) -> Optional[str]:
    """The latest status the journal holds for a transaction, or None if it was never journaled."""
    want = (tx_hash or "").lower()
    if not want:
        return None
    status = None
    for e in _all():
        if (e.get("tx") or "").lower() == want and e.get("status"):
            status = str(e["status"])
    return status


def entry_for_tx(user_id: int, tx_hash: str) -> Optional[Dict[str, Any]]:
    """The original (non-resolution) record of a transaction, if journaled."""
    for e in entries_for(user_id):
        if (e.get("tx") or "").lower() == (tx_hash or "").lower() and e.get("kind") != "resolution":
            return e
    return None


def unresolved() -> List[Tuple[int, str, float]]:
    """(user_id, tx, ts) for every transaction whose latest status is still
    submitted or pending. Read on startup to rebuild the in-flight guard."""
    latest: Dict[str, Tuple[str, int, float]] = {}
    for e in _all():
        tx = (e.get("tx") or "").lower()
        if not tx:
            continue
        latest[tx] = (str(e.get("status") or ""), int(e.get("user_id") or 0), float(e.get("ts") or 0.0))
    out = [(uid, tx, ts) for tx, (status, uid, ts) in latest.items() if status in UNRESOLVED and uid]
    return sorted(out, key=lambda x: x[2])


def tokens_touched(user_id: int) -> List[str]:
    """Token addresses this user has bought or sold, most recent first."""
    seen: List[str] = []
    for e in reversed(entries_for(user_id)):
        if e.get("kind") not in TRADE_KINDS:
            continue
        t = (e.get("token") or "").lower()
        if t and t not in seen:
            seen.append(t)
    return seen
