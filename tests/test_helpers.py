import pytest

from mccapbot.helpers import (
    RelativeTargetError,
    human_window,
    humanize,
    is_solana_address,
    meets,
    parse_mc_input,
    parse_target,
    parse_window,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("250k", 250_000),
        ("2.5m", 2_500_000),
        ("1b", 1_000_000_000),
        ("1t", 1_000_000_000_000),
        ("2500000", 2_500_000),
        ("1,250,000", 1_250_000),
        ("  3.5M  ", 3_500_000),
        ("$750k", 750_000),
    ],
)
def test_parse_mc_input(raw, expected):
    assert parse_mc_input(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["abc", "", "12x", "k"])
def test_parse_mc_input_rejects_junk(raw):
    with pytest.raises(ValueError):
        parse_mc_input(raw)


def test_humanize():
    assert humanize(None) == "—"
    assert humanize(999) == "999.00"
    assert humanize(1_500) == "1.50K"
    assert humanize(2_500_000) == "2.50M"


def test_meets_direction():
    assert meets("above", 100, 50) is True
    assert meets("above", 40, 50) is False
    assert meets("below", 40, 50) is True
    # No market-cap data must never fire an alert.
    assert meets("above", None, 50) is False


def test_is_solana_address():
    assert is_solana_address("So11111111111111111111111111111111111111112")
    assert not is_solana_address("0x37cc340fab73ff508c085558f611403810e24444")
    assert not is_solana_address("")


# ---------------- relative targets ----------------


def test_absolute_target_has_no_spec():
    val, spec = parse_target("2.5m", current_mc=1_000_000)
    assert val == pytest.approx(2_500_000)
    assert spec == ""


@pytest.mark.parametrize("raw,mult,label", [("2x", 2, "2x"), ("x3", 3, "3x"), ("0.5x", 0.5, "0.5x")])
def test_multiplier_targets(raw, mult, label):
    val, spec = parse_target(raw, current_mc=1_000_000)
    assert val == pytest.approx(1_000_000 * mult)
    assert spec == label


def test_percent_targets():
    up, spec_up = parse_target("+50%", current_mc=1_000_000)
    assert up == pytest.approx(1_500_000)
    assert spec_up == "+50%"

    down, spec_down = parse_target("-30%", current_mc=1_000_000)
    assert down == pytest.approx(700_000)
    assert spec_down == "-30%"

    # A bare percentage reads as an increase.
    bare, _ = parse_target("25%", current_mc=1_000_000)
    assert bare == pytest.approx(1_250_000)


def test_relative_target_needs_an_anchor():
    """Without a current MC there is nothing to multiply, and silently guessing
    would create an alert at a meaningless number."""
    for raw in ("2x", "+50%", "-30%"):
        with pytest.raises(RelativeTargetError):
            parse_target(raw, current_mc=None)
        with pytest.raises(RelativeTargetError):
            parse_target(raw, current_mc=0)


def test_absolute_target_works_without_anchor():
    val, spec = parse_target("500k", current_mc=None)
    assert val == pytest.approx(500_000)
    assert spec == ""


def test_nonsense_relative_targets_rejected():
    with pytest.raises(ValueError):
        parse_target("-100%", current_mc=1_000_000)   # would be zero
    with pytest.raises(ValueError):
        parse_target("-150%", current_mc=1_000_000)   # would be negative
    with pytest.raises(ValueError):
        parse_target("0x", current_mc=1_000_000)


# ---------------- windows ----------------


@pytest.mark.parametrize(
    "raw,secs", [("15m", 900), ("1h", 3600), ("4h", 14400), ("1d", 86400), ("90m", 5400)]
)
def test_parse_window(raw, secs):
    assert parse_window(raw) == secs


@pytest.mark.parametrize("raw", ["", "abc", "30s", "0m", "8d", "1w", "5"])
def test_parse_window_rejects_out_of_range(raw):
    with pytest.raises(ValueError):
        parse_window(raw)


def test_human_window_roundtrip():
    for raw in ("15m", "1h", "4h", "1d"):
        assert human_window(parse_window(raw)) == raw
