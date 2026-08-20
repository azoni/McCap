"""The polling schedule is what keeps the bot under DexScreener's rate limit."""

import pytest

from mccapbot.config import (
    MOVE_MIN_SECONDS,
    MOVE_SAMPLE_DIVISOR,
    POLL_COLD_SECONDS,
    POLL_HOT_SECONDS,
    POLL_UNKNOWN_SECONDS,
    POLL_WARM_SECONDS,
)
from mccapbot.models import MoveAlert, Reminder
from mccapbot.scheduler import (
    describe_tiers,
    due_addresses,
    estimated_requests_per_minute,
    interval_for_move,
    interval_for_progress,
    intervals_by_address,
    progress_to_target,
)

DEX_HARD_LIMIT = 300


def mk(ca="CA1", target=1_000_000, direction="above"):
    return Reminder(
        ca=ca, target_mc=target, direction=direction, channel_id=1,
        creator_id=1, guild_id=1, name="Tok", symbol="TOK",
    )


def mv(ca="CA1", pct=30, window=3600, direction="both"):
    return MoveAlert(
        ca=ca, pct=pct, window_sec=window, direction=direction, channel_id=1,
        creator_id=1, guild_id=1, name="Tok", symbol="TOK",
    )


def test_progress_above_is_ratio_to_target():
    assert progress_to_target("above", 500_000, 1_000_000) == pytest.approx(0.5)


def test_progress_below_inverts():
    # A 'below' alert approaches its target from above, so closeness inverts.
    assert progress_to_target("below", 2_000_000, 1_000_000) == pytest.approx(0.5)


def test_progress_none_when_no_data():
    assert progress_to_target("above", None, 1_000_000) is None
    assert progress_to_target("above", 0, 1_000_000) is None
    assert progress_to_target("above", 100, 0) is None


def test_interval_tiers():
    assert interval_for_progress(None) == POLL_UNKNOWN_SECONDS
    assert interval_for_progress(0.99) == POLL_HOT_SECONDS
    assert interval_for_progress(1.5) == POLL_HOT_SECONDS  # already past target
    assert interval_for_progress(0.60) == POLL_WARM_SECONDS
    assert interval_for_progress(0.01) == POLL_COLD_SECONDS


def test_due_addresses_respects_interval():
    r = mk(ca="HOT", target=1_000_000)
    mc = {"HOT": 999_000}
    assert due_addresses([r], [], mc, {}, now=1000.0) == ["HOT"]
    assert due_addresses([r], [], mc, {"HOT": 1000.0}, now=1000.0) == []
    assert due_addresses([r], [], mc, {"HOT": 1000.0}, now=1000.0 + POLL_HOT_SECONDS) == ["HOT"]


def test_cold_token_is_not_polled_at_hot_cadence():
    r = mk(ca="COLD", target=10_000_000)
    mc = {"COLD": 40_000}
    assert due_addresses([r], [], mc, {"COLD": 0.0}, now=POLL_HOT_SECONDS) == []
    assert due_addresses([r], [], mc, {"COLD": 0.0}, now=POLL_COLD_SECONDS) == ["COLD"]


def test_shared_token_uses_shortest_interval():
    near = mk(ca="SAME", target=1_000_000)      # 999k -> hot
    far = mk(ca="SAME", target=500_000_000)     # 999k -> cold
    mc = {"SAME": 999_000}
    due = due_addresses([far, near], [], mc, {"SAME": 0.0}, now=POLL_HOT_SECONDS)
    assert due == ["SAME"], "one request should serve both alerts"


# ---------------- momentum sampling ----------------


def test_move_sampling_scales_with_window():
    """A 1h window sampled hourly can't detect anything."""
    assert interval_for_move(mv(window=3600)) == 3600 // MOVE_SAMPLE_DIVISOR
    assert interval_for_move(mv(window=86400)) == 86400 // MOVE_SAMPLE_DIVISOR


def test_move_sampling_has_a_floor():
    """Short windows must not be allowed to hammer the API."""
    assert interval_for_move(mv(window=60)) == MOVE_MIN_SECONDS
    assert interval_for_move(mv(window=120)) >= MOVE_MIN_SECONDS


def test_move_alert_can_tighten_a_cold_token():
    """A cold level alert plus a momentum alert on the same token: the
    momentum sampling rate wins, because it's the shorter of the two."""
    r = mk(ca="X", target=100_000_000)   # far away -> cold
    m = mv(ca="X", window=3600)          # -> 300s
    mc = {"X": 50_000}
    intervals = intervals_by_address([r], [m], mc)
    assert intervals["X"] == min(POLL_COLD_SECONDS, interval_for_move(m))


# ---------------- rate ceilings ----------------


def test_request_rate_stays_under_the_limit():
    """36 tokens on the old flat 3s sweep was 720 req/min against a 300 limit."""
    reminders, mc = [], {}
    for i in range(36):
        ca = f"CA{i}"
        reminders.append(mk(ca=ca, target=10_000_000))
        mc[ca] = 50_000
    rate = estimated_requests_per_minute(reminders, [], mc)
    assert rate < DEX_HARD_LIMIT
    assert rate == pytest.approx(36 * 60 / POLL_COLD_SECONDS)


def test_worst_case_all_hot_still_under_limit():
    reminders, mc = [], {}
    for i in range(36):
        ca = f"CA{i}"
        reminders.append(mk(ca=ca, target=1_000_000))
        mc[ca] = 999_000
    assert estimated_requests_per_minute(reminders, [], mc) < DEX_HARD_LIMIT


def test_many_short_window_move_alerts_stay_under_limit():
    """Momentum alerts are the new way to blow the budget: 40 tokens each
    demanding the fastest sampling rate must still fit."""
    moves, mc = [], {}
    for i in range(40):
        ca = f"CA{i}"
        moves.append(mv(ca=ca, window=60))  # floors at MOVE_MIN_SECONDS
        mc[ca] = 1_000_000
    rate = estimated_requests_per_minute([], moves, mc)
    assert rate < DEX_HARD_LIMIT, f"momentum alerts would exceed the limit: {rate}/min"


def test_mixed_workload_under_limit():
    reminders, moves, mc = [], [], {}
    for i in range(30):
        ca = f"L{i}"
        reminders.append(mk(ca=ca, target=1_000_000))
        mc[ca] = 999_000  # all hot, worst case
    for i in range(15):
        ca = f"M{i}"
        moves.append(mv(ca=ca, window=900))
        mc[ca] = 1_000_000
    assert estimated_requests_per_minute(reminders, moves, mc) < DEX_HARD_LIMIT


def test_describe_tiers_counts():
    rs = [mk(ca="A"), mk(ca="B"), mk(ca="C")]
    mc = {"A": 999_000, "B": 600_000, "C": None}
    assert describe_tiers(rs, mc) == {"hot": 1, "warm": 1, "cold": 0, "unknown": 1}
