import pytest

from mccapbot.helpers import (
    BAD,
    GOOD,
    NEUTRAL,
    SEP,
    UNKNOWN,
    RelativeTargetError,
    chunk_lines,
    colour_for,
    compact,
    eth_str,
    fit_lines,
    footer,
    human_window,
    humanize,
    is_solana_address,
    meets,
    mult,
    parse_mc_input,
    parse_target,
    parse_window,
    pct,
    plural,
    qty,
    usd,
    when,
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


def test_humanize_is_compact():
    """Three significant figures above a thousand, two decimals below: $380.12K
    and $221.93K side by side was the noise Charlton asked to lose."""
    assert humanize(None) == UNKNOWN == "—"
    assert humanize(999) == "999.00"
    assert humanize(1_500) == "1.5K"
    assert humanize(2_500_000) == "2.5M"
    assert humanize(221_930) == "222K"
    assert humanize(1_234_567) == "1.23M"
    assert compact(999_600) == "1M", "rounding past the unit boundary carries into the next unit"
    assert compact(2525.2) == "2.53K" and compact(-1500) == "-1.5K"


def test_money_percent_multiple_and_amount_formats():
    assert usd(4.79) == "$4.79" and usd(221_000) == "$221K" and usd(1_200_000) == "$1.2M"
    assert usd(4.95, signed=True) == "+$4.95" and usd(-3.2, signed=True) == "-$3.20" and usd(-3.2) == "-$3.20"
    assert usd(None) == UNKNOWN
    assert pct(37.5) == "+37.5%" and pct(-12.3) == "-12.3%" and pct(4.0) == "+4%"
    assert pct(30, signed=False) == "30%" and pct(-0.01) == "+0%" and pct(None) == UNKNOWN
    assert mult(0.958) == "0.96x" and mult(1.26) == "1.26x" and mult(4.0) == "4x"
    assert mult(12.34) == "12.3x" and mult(150) == "150x" and mult(None) == UNKNOWN
    assert qty(2062.09) == "2,062" and qty(36.0) == "36" and qty(3.33) == "3.33" and qty(0.00042) == "0.0004"
    assert eth_str(1.0) == "1" and eth_str(0.05) == "0.05" and eth_str(0.0181234567) == "0.018123"


def test_time_plural_footer_and_colour():
    assert when(1_700_000_000.9) == "<t:1700000000:R>"
    assert plural(1, "alert") == "1 alert" and plural(3, "alert") == "3 alerts" and plural(1200, "row") == "1,200 rows"
    assert footer("a", "", None, "b") == f"a{SEP}b" == "a · b"
    assert colour_for(None) == NEUTRAL and colour_for(0) == GOOD and colour_for(-0.01) == BAD


def test_chunk_and_fit_lines():
    assert chunk_lines(["aaa", "bbb", "ccc"], 7) == ["aaa\nbbb", "ccc"]
    assert chunk_lines([], 10) == []
    assert all(len(c) <= 1024 for c in chunk_lines(["x" * 300] * 20, 1024))
    assert len(chunk_lines(["y" * 5000], 1024)[0]) == 1024, "one oversized line is cut, not dropped"
    out = fit_lines(["x" * 100] * 30, 2000)
    assert len(out) <= 2000 and "more lines" in out, "truncation is disclosed"
    assert fit_lines(["a", "b"]) == "a\nb"


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


def test_age_reads_like_a_board():
    from mccapbot.helpers import age
    assert age(240) == "4m" and age(7200) == "2h" and age(3 * 86400 + 5) == "3d" and age(-5) == "0m"
