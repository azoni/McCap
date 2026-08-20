"""Momentum alerts live or die on this module reporting honestly.

The dangerous failure is reporting 0% when it simply lacks data: that silently
suppresses every alert. It must return None until the window has really filled.
"""

import pytest

from mccapbot import history


@pytest.fixture(autouse=True)
def clean():
    history.clear()
    yield
    history.clear()


def fill(ca, points):
    for ts, mc in points:
        history.record(ca, mc, ts)


def test_no_history_returns_none():
    assert history.pct_change("NOPE", 3600, now=1000) is None


def test_single_sample_returns_none():
    history.record("A", 100.0, 1000)
    assert history.pct_change("A", 3600, now=1000) is None


def test_short_history_returns_none_not_zero():
    """Two samples 60s apart cannot answer 'how much in the last hour'."""
    fill("A", [(1000, 100.0), (1060, 100.0)])
    assert history.pct_change("A", 3600, now=1060) is None


def test_measures_gain_across_window():
    fill("A", [(0, 100.0), (1800, 120.0), (3600, 150.0)])
    change = history.pct_change("A", 3600, now=3600)
    assert change == pytest.approx(50.0)


def test_measures_loss_across_window():
    fill("A", [(0, 200.0), (1800, 150.0), (3600, 100.0)])
    change = history.pct_change("A", 3600, now=3600)
    assert change == pytest.approx(-50.0)


def test_baseline_only_considers_samples_inside_window():
    # An ancient spike must not become the baseline for a 1h window.
    fill("A", [(0, 1000.0), (7200, 100.0), (10800, 110.0)])
    change = history.pct_change("A", 3600, now=10800)
    assert change == pytest.approx(10.0)


def test_none_market_caps_are_not_recorded_as_zero():
    """A failed fetch returning None must not read as a 100% crash."""
    history.record("A", 100.0, 0)
    history.record("A", None, 60)
    history.record("A", 100.0, 3600)
    assert history.sample_count("A") == 2
    assert history.pct_change("A", 3600, now=3600) == pytest.approx(0.0)


def test_zero_market_cap_ignored():
    history.record("A", 0, 0)
    assert history.sample_count("A") == 0


def test_series_is_bounded():
    for i in range(history.HISTORY_MAX_SAMPLES + 200):
        history.record("A", 100.0 + i, i)
    assert history.sample_count("A") <= history.HISTORY_MAX_SAMPLES


def test_prune_drops_old_samples():
    fill("A", [(0, 100.0), (1000, 110.0), (2000, 120.0)])
    history.prune("A", older_than_ts=1500)
    assert history.sample_count("A") == 1


def test_forget_drops_untracked_tokens():
    fill("A", [(0, 1.0), (10, 2.0)])
    fill("B", [(0, 1.0), (10, 2.0)])
    history.forget(keep={"A"})
    assert history.sample_count("A") == 2
    assert history.sample_count("B") == 0


def test_span_seconds():
    fill("A", [(100, 1.0), (700, 2.0)])
    assert history.span_seconds("A") == pytest.approx(600)
