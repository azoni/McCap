"""The polling schedule is what keeps the bot under DexScreener's rate limit."""

import pytest

from mccapbot.config import (
    POLL_COLD_SECONDS,
    POLL_HOT_SECONDS,
    POLL_UNKNOWN_SECONDS,
    POLL_WARM_SECONDS,
)
from mccapbot.models import Reminder
from mccapbot.scheduler import (
    describe_tiers,
    due_addresses,
    estimated_requests_per_minute,
    interval_for_progress,
    progress_to_target,
)


def mk(ca="CA1", target=1_000_000, direction="above"):
    return Reminder(
        ca=ca,
        target_mc=target,
        direction=direction,
        channel_id=1,
        creator_id=1,
        guild_id=1,
        name="Tok",
        symbol="TOK",
    )


def test_progress_above_is_ratio_to_target():
    assert progress_to_target("above", 500_000, 1_000_000) == pytest.approx(0.5)
    assert progress_to_target("above", 990_000, 1_000_000) == pytest.approx(0.99)


def test_progress_below_inverts():
    # A 'below' alert approaches its target from above, so closeness inverts.
    assert progress_to_target("below", 2_000_000, 1_000_000) == pytest.approx(0.5)
    assert progress_to_target("below", 1_010_000, 1_000_000) == pytest.approx(0.99, rel=1e-2)


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
    mc = {"HOT": 999_000}  # hot tier
    # Never checked -> due immediately.
    assert due_addresses([r], mc, {}, now=1000.0) == ["HOT"]
    # Just checked -> not due.
    assert due_addresses([r], mc, {"HOT": 1000.0}, now=1000.0) == []
    # Interval elapsed -> due again.
    assert due_addresses([r], mc, {"HOT": 1000.0}, now=1000.0 + POLL_HOT_SECONDS) == ["HOT"]


def test_cold_token_is_not_polled_at_hot_cadence():
    r = mk(ca="COLD", target=10_000_000)
    mc = {"COLD": 40_000}  # 0.4% of target -> cold
    assert due_addresses([r], mc, {"COLD": 0.0}, now=POLL_HOT_SECONDS) == []
    assert due_addresses([r], mc, {"COLD": 0.0}, now=POLL_COLD_SECONDS) == ["COLD"]


def test_shared_token_uses_shortest_interval():
    """Two alerts on one token: the closest one sets the pace for both."""
    near = mk(ca="SAME", target=1_000_000)      # current 999k -> hot
    far = mk(ca="SAME", target=500_000_000)     # current 999k -> cold
    mc = {"SAME": 999_000}
    due = due_addresses([far, near], mc, {"SAME": 0.0}, now=POLL_HOT_SECONDS)
    assert due == ["SAME"]
    # And only one request is issued for the shared address.
    assert len(due) == 1


def test_request_rate_stays_under_the_limit():
    """The regression this whole module exists for.

    36 tokens on the old flat 3s sweep was 720 req/min against a 300 limit.
    Realistically most alerts sit far from their target, so the tiered
    schedule should land far below that.
    """
    reminders, mc = [], {}
    for i in range(36):
        ca = f"CA{i}"
        reminders.append(mk(ca=ca, target=10_000_000))
        mc[ca] = 50_000  # cold: 0.5% of target
    rate = estimated_requests_per_minute(reminders, mc)
    assert rate < 300, f"would exceed the DexScreener limit: {rate}/min"
    assert rate == pytest.approx(36 * 60 / POLL_COLD_SECONDS)


def test_worst_case_all_hot_still_under_limit():
    """Even if every tracked token is about to fire, stay under 300/min."""
    reminders, mc = [], {}
    for i in range(36):
        ca = f"CA{i}"
        reminders.append(mk(ca=ca, target=1_000_000))
        mc[ca] = 999_000  # all hot
    rate = estimated_requests_per_minute(reminders, mc)
    assert rate < 300, f"hot-tier sweep exceeds the limit: {rate}/min"


def test_describe_tiers_counts():
    rs = [mk(ca="A", target=1_000_000), mk(ca="B", target=1_000_000), mk(ca="C", target=1_000_000)]
    mc = {"A": 999_000, "B": 600_000, "C": None}
    tiers = describe_tiers(rs, mc)
    assert tiers == {"hot": 1, "warm": 1, "cold": 0, "unknown": 1}
