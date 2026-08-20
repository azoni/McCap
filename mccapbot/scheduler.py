"""Adaptive polling schedule for market-cap alerts.

The original watcher re-fetched every tracked token every 3 seconds. With 36
tokens that is 720 requests/minute against a 300/minute limit, so DexScreener
started refusing calls and alerts fired late or not at all.

A token sitting at $40k with a $10M target does not need second-by-second
polling; one that is 5% away does. These helpers grade each token by how close
it is to firing and hand back a per-token refresh interval, which keeps the
whole sweep comfortably inside the rate limit while making near-miss alerts
*more* responsive than before.

Kept free of discord/aiohttp imports so it can be unit tested directly.
"""

from typing import Dict, Iterable, List, Optional

from .config import (
    HOT_BAND,
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


def due_addresses(
    reminders: Iterable,
    mc_by_ca: Dict[str, Optional[float]],
    last_checked: Dict[str, float],
    now: float,
) -> List[str]:
    """Return the contract addresses whose refresh interval has elapsed.

    Several alerts can watch the same token with different targets, so each
    address takes the *shortest* interval any of its alerts asks for — the
    closest-to-firing alert sets the pace and the rest ride along for free.
    """
    interval_by_ca: Dict[str, int] = {}
    for r in reminders:
        wanted = interval_for_reminder(r, mc_by_ca.get(r.ca))
        current = interval_by_ca.get(r.ca)
        if current is None or wanted < current:
            interval_by_ca[r.ca] = wanted

    due: List[str] = []
    for ca, interval in interval_by_ca.items():
        seen = last_checked.get(ca)
        if seen is None or (now - seen) >= interval:
            due.append(ca)
    return due


def describe_tiers(reminders: Iterable, mc_by_ca: Dict[str, Optional[float]]) -> Dict[str, int]:
    """Count alerts per tier — used by /mc_status for operator visibility."""
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


def estimated_requests_per_minute(reminders: Iterable, mc_by_ca: Dict[str, Optional[float]]) -> float:
    """Projected outgoing request rate for the current alert set."""
    interval_by_ca: Dict[str, int] = {}
    for r in reminders:
        wanted = interval_for_reminder(r, mc_by_ca.get(r.ca))
        current = interval_by_ca.get(r.ca)
        if current is None or wanted < current:
            interval_by_ca[r.ca] = wanted
    return sum(60.0 / i for i in interval_by_ca.values() if i > 0)
