"""Adaptive polling schedule.

The original watcher re-fetched every tracked token every 3 seconds. With 36
tokens that is 720 requests/minute against a 300/minute limit, so DexScreener
started refusing calls and alerts fired late or not at all.

Level alerts are graded by how close the token is to firing — a token at $40k
with a $10M target does not need second-by-second polling, one that is 5% away
does. Momentum alerts instead need a steady sample rate fine enough to measure
their window. Each token takes the shortest interval anything asks of it.

Kept free of discord/aiohttp imports so it can be unit tested directly.
"""

from typing import Dict, Iterable, List, Optional

from .config import (
    HOT_BAND,
    MOVE_MIN_SECONDS,
    MOVE_SAMPLE_DIVISOR,
    POLL_COLD_SECONDS,
    POLL_HOT_SECONDS,
    POLL_UNKNOWN_SECONDS,
    POLL_WARM_SECONDS,
    WARM_BAND,
)


def progress_to_target(direction: str, current_mc: Optional[float], target_mc: float) -> Optional[float]:
    """How close a token is to firing, as a 0..1+ ratio (1.0 == at target).

    For ``above`` alerts that is current/target. For ``below`` alerts the token
    approaches the target from above, so the ratio inverts to target/current.
    Returns None when there is no usable market cap to judge from.
    """
    if current_mc is None or current_mc <= 0 or target_mc <= 0:
        return None
    if direction == "above":
        return current_mc / target_mc
    return target_mc / current_mc


def interval_for_progress(progress: Optional[float]) -> int:
    """Map closeness-to-target onto a refresh interval in seconds."""
    if progress is None:
        return POLL_UNKNOWN_SECONDS
    if progress >= HOT_BAND:
        return POLL_HOT_SECONDS
    if progress >= WARM_BAND:
        return POLL_WARM_SECONDS
    return POLL_COLD_SECONDS


def interval_for_reminder(reminder, current_mc: Optional[float]) -> int:
    return interval_for_progress(progress_to_target(reminder.direction, current_mc, reminder.target_mc))


def interval_for_move(move) -> int:
    """Sampling rate for a momentum alert.

    A 1h window sampled once an hour can't detect anything, so sample several
    times per window — but never faster than MOVE_MIN_SECONDS, or a handful of
    short-window alerts would dominate the request budget.
    """
    return max(MOVE_MIN_SECONDS, int(move.window_sec // max(1, MOVE_SAMPLE_DIVISOR)))


def move_ready(move, now: float) -> bool:
    """False while a momentum alert is still inside its cooldown."""
    return (now - move.last_fired_ts) >= move.cooldown_sec


def move_triggered(direction: str, pct: float, change: Optional[float]) -> bool:
    """Whether an observed percent change satisfies a momentum alert.

    ``change`` is None when the window has not filled yet. That is explicitly
    *not* a trigger and must never be coerced to 0.0, or a freshly restarted
    bot would report "no movement" for every token.
    """
    if change is None:
        return False
    if direction == "up":
        return change >= pct
    if direction == "down":
        return change <= -pct
    return abs(change) >= pct


def intervals_by_address(
    reminders: Iterable,
    moves: Iterable,
    mc_by_ca: Dict[str, Optional[float]],
) -> Dict[str, int]:
    """Shortest refresh interval each address needs, across all its watchers."""
    out: Dict[str, int] = {}

    def want(ca: str, seconds: int) -> None:
        cur = out.get(ca)
        if cur is None or seconds < cur:
            out[ca] = seconds

    for r in reminders:
        want(r.ca, interval_for_reminder(r, mc_by_ca.get(r.ca)))
    for m in moves:
        want(m.ca, interval_for_move(m))
    return out


def due_addresses(
    reminders: Iterable,
    moves: Iterable,
    mc_by_ca: Dict[str, Optional[float]],
    last_checked: Dict[str, float],
    now: float,
) -> List[str]:
    """Return the contract addresses whose refresh interval has elapsed."""
    due: List[str] = []
    for ca, interval in intervals_by_address(reminders, moves, mc_by_ca).items():
        seen = last_checked.get(ca)
        if seen is None or (now - seen) >= interval:
            due.append(ca)
    return due


def describe_tiers(reminders: Iterable, mc_by_ca: Dict[str, Optional[float]]) -> Dict[str, int]:
    """Count level alerts per tier — used by /mc_status for operator visibility."""
    tiers = {"hot": 0, "warm": 0, "cold": 0, "unknown": 0}
    for r in reminders:
        p = progress_to_target(r.direction, mc_by_ca.get(r.ca), r.target_mc)
        if p is None:
            tiers["unknown"] += 1
        elif p >= HOT_BAND:
            tiers["hot"] += 1
        elif p >= WARM_BAND:
            tiers["warm"] += 1
        else:
            tiers["cold"] += 1
    return tiers


def estimated_requests_per_minute(
    reminders: Iterable,
    moves: Iterable,
    mc_by_ca: Dict[str, Optional[float]],
) -> float:
    """Projected outgoing request rate for the current alert set."""
    return sum(60.0 / i for i in intervals_by_address(reminders, moves, mc_by_ca).values() if i > 0)
