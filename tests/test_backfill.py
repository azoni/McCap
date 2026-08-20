"""Seeding history from GeckoTerminal candles."""

import pytest

from mccapbot import history
from mccapbot.gecko import _timeframe_for


@pytest.fixture(autouse=True)
def clean():
    history.clear()
    yield
    history.clear()


# ---------------- timeframe selection ----------------


@pytest.mark.parametrize("window,expect_tf", [
    (900, "minute"),      # 15m
    (3600, "minute"),     # 1h
    (4 * 3600, "minute"),
    (86400, "hour"),      # 1d
    (5 * 86400, "hour"),
])
def test_timeframe_choice(window, expect_tf):
    tf, agg, limit = _timeframe_for(window)
    assert tf == expect_tf
    # Whatever it picks must actually span the requested window.
    seconds_per_candle = agg * (60 if tf == "minute" else 3600)
    assert seconds_per_candle * limit >= window, "chosen candles don't cover the window"


# ---------------- seeding ----------------


def test_seed_populates_an_empty_series():
    pts = [(1000.0 + i * 300, 100.0 + i) for i in range(12)]
    added = history.seed("A", pts)
    assert added == 12
    assert history.sample_count("A") == 12


def test_seed_enables_pct_change_immediately():
    """The whole point: a fresh alert should be armed, not warming up."""
    now = 100_000.0
    assert history.pct_change("A", 3600, now) is None

    pts = [(now - 3600 + i * 300, 100.0) for i in range(12)]
    pts.append((now, 125.0))
    history.seed("A", pts)

    change = history.pct_change("A", 3600, now)
    assert change is not None, "seeded history should arm the alert"
    assert change == pytest.approx(25.0)


def test_seed_keeps_series_ordered_when_merging_with_live_samples():
    """Backfilled candles are older than live samples and cannot just be
    appended — an unsorted series makes `baseline` read the wrong end."""
    now = 100_000.0
    history.record("A", 130.0, now)          # live sample arrives first
    history.seed("A", [(now - 3600, 100.0), (now - 1800, 110.0)])

    series = list(history._series["A"])
    assert [ts for ts, _ in series] == sorted(ts for ts, _ in series)
    assert history.pct_change("A", 3600, now) == pytest.approx(30.0)


def test_live_samples_win_over_backfilled_on_the_same_timestamp():
    now = 100_000.0
    history.record("A", 999.0, now)
    history.seed("A", [(now, 111.0), (now - 3600, 100.0)])
    assert history.latest("A")[1] == 999.0


def test_seed_ignores_nonpositive_values():
    history.seed("A", [(1.0, 0.0), (2.0, -5.0), (3.0, 10.0)])
    assert history.sample_count("A") == 1


def test_seed_respects_the_sample_cap():
    pts = [(float(i), 100.0) for i in range(history.HISTORY_MAX_SAMPLES + 300)]
    history.seed("A", pts)
    assert history.sample_count("A") <= history.HISTORY_MAX_SAMPLES


def test_empty_seed_is_harmless():
    assert history.seed("A", []) == 0
    assert history.pct_change("A", 3600, now=1000) is None
