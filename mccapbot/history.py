"""Rolling per-token market-cap history.

Level alerts only need the latest value, but a momentum alert asks "has this
moved 30% in the last hour?", which needs a baseline from the past. This keeps
a bounded, in-memory series of (timestamp, mc) samples per token.

Deliberately not persisted: after a restart the bot simply has no baseline yet
and momentum alerts stay quiet until the window refills, which is the safe
failure mode. Persisting a stale pre-restart baseline would fire alerts for
"moves" that were really just the bot being offline.
"""

from collections import deque
from typing import Deque, Dict, Iterable, Optional, Tuple

from .config import HISTORY_MAX_SAMPLES

# ca -> deque of (ts, mc), oldest first
_series: Dict[str, Deque[Tuple[float, float]]] = {}


def record(ca: str, mc: Optional[float], ts: float) -> None:
    """Append a sample. ``None`` market caps are ignored, not stored as zero."""
    if mc is None or mc <= 0:
        return
    s = _series.get(ca)
    if s is None:
        s = _series[ca] = deque(maxlen=HISTORY_MAX_SAMPLES)
    s.append((ts, float(mc)))


def prune(ca: str, older_than_ts: float) -> None:
    """Drop samples older than a cutoff."""
    s = _series.get(ca)
    if not s:
        return
    while s and s[0][0] < older_than_ts:
        s.popleft()
    if not s:
        _series.pop(ca, None)


def forget(keep: Iterable[str]) -> None:
    """Drop series for tokens nobody watches any more."""
    keep = set(keep)
    for ca in [c for c in _series if c not in keep]:
        _series.pop(ca, None)


def baseline(ca: str, window_sec: int, now: float) -> Optional[Tuple[float, float]]:
    """Oldest sample still inside the window, as ``(ts, mc)``.

    Returns None when the window has not filled yet — callers must treat that
    as "not enough data", never as "no movement".
    """
    s = _series.get(ca)
    if not s:
        return None
    cutoff = now - window_sec
    for ts, mc in s:
        if ts >= cutoff:
            return ts, mc
    return None


def latest(ca: str) -> Optional[Tuple[float, float]]:
    s = _series.get(ca)
    return s[-1] if s else None


def pct_change(ca: str, window_sec: int, now: float) -> Optional[float]:
    """Percent change across the window, or None if there isn't enough history.

    Requires the window to have actually filled: a series that only started 2
    minutes ago cannot answer "how much has this moved in an hour", and
    answering 0% there would silently suppress real alerts.
    """
    s = _series.get(ca)
    if not s or len(s) < 2:
        return None
    base = baseline(ca, window_sec, now)
    last = s[-1]
    if not base or base[1] <= 0:
        return None
    # The oldest retained sample must be old enough to represent the window.
    span = last[0] - s[0][0]
    if span < window_sec * 0.5:
        return None
    return (last[1] - base[1]) / base[1] * 100.0


def span_seconds(ca: str) -> float:
    """How much history we hold for a token, in seconds."""
    s = _series.get(ca)
    if not s or len(s) < 2:
        return 0.0
    return s[-1][0] - s[0][0]


def sample_count(ca: str) -> int:
    s = _series.get(ca)
    return len(s) if s else 0


def clear() -> None:
    _series.clear()
