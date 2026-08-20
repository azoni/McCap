"""Momentum alert trigger rules: direction, magnitude, cooldown."""

import pytest

from mccapbot.models import MoveAlert
from mccapbot.scheduler import move_ready, move_triggered


def mv(direction="both", pct=30, cooldown=1800, last_fired=0.0):
    m = MoveAlert(
        ca="CA", pct=pct, window_sec=3600, direction=direction, channel_id=1,
        creator_id=1, guild_id=1, name="T", symbol="T", cooldown_sec=cooldown,
    )
    m.last_fired_ts = last_fired
    return m


# ---------------- magnitude + direction ----------------


def test_both_fires_on_either_side():
    assert move_triggered("both", 30, 35) is True
    assert move_triggered("both", 30, -35) is True


def test_both_ignores_small_moves():
    assert move_triggered("both", 30, 29.9) is False
    assert move_triggered("both", 30, -29.9) is False


def test_up_only_ignores_dumps():
    assert move_triggered("up", 30, 35) is True
    assert move_triggered("up", 30, -35) is False, "a pump alert must not fire on a crash"


def test_down_only_ignores_pumps():
    assert move_triggered("down", 30, -35) is True
    assert move_triggered("down", 30, 35) is False, "a dump alert must not fire on a rally"


def test_exact_threshold_fires():
    assert move_triggered("both", 30, 30) is True
    assert move_triggered("up", 30, 30) is True
    assert move_triggered("down", 30, -30) is True


def test_missing_history_never_triggers():
    """None means 'not enough data', which must not be read as a move."""
    for d in ("up", "down", "both"):
        assert move_triggered(d, 30, None) is False


def test_zero_change_never_triggers():
    for d in ("up", "down", "both"):
        assert move_triggered(d, 30, 0.0) is False


# ---------------- cooldown ----------------


# Epoch-scale clock: last_fired_ts defaults to 0.0, so a never-fired alert is
# ready the moment it exists.
NOW = 1_787_000_000.0


def test_fresh_alert_is_ready():
    assert move_ready(mv(last_fired=0.0), now=NOW) is True


def test_cooldown_blocks_immediate_refire():
    """Without this a volatile token would ping on every single tick."""
    m = mv(cooldown=1800, last_fired=NOW)
    assert move_ready(m, now=NOW) is False
    assert move_ready(m, now=NOW + 1799) is False


def test_alert_rearms_after_cooldown():
    m = mv(cooldown=1800, last_fired=NOW)
    assert move_ready(m, now=NOW + 1800) is True


def test_zero_cooldown_always_ready():
    assert move_ready(mv(cooldown=0, last_fired=NOW), now=NOW) is True


@pytest.mark.parametrize("pct,change,expected", [
    (10, 12, True), (10, 8, False),
    (50, 60, True), (50, 49.99, False),
    (100, 150, True), (100, 99, False),
])
def test_threshold_boundaries(pct, change, expected):
    assert move_triggered("both", pct, change) is expected
