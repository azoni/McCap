import pytest

from mccapbot.helpers import humanize, is_solana_address, meets, parse_mc_input, to_lamports


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
    assert humanize(1_000_000_000) == "1.00B"


def test_meets_direction():
    assert meets("above", 100, 50) is True
    assert meets("above", 40, 50) is False
    assert meets("below", 40, 50) is True
    assert meets("below", 100, 50) is False
    # No market-cap data must never fire an alert.
    assert meets("above", None, 50) is False
    assert meets("below", None, 50) is False


def test_is_solana_address():
    assert is_solana_address("So11111111111111111111111111111111111111112")
    assert not is_solana_address("0x37cc340fab73ff508c085558f611403810e24444")
    assert not is_solana_address("")
    # 0, O, I and l are not in the base58 alphabet.
    assert not is_solana_address("0OIl" * 8)


def test_to_lamports():
    assert to_lamports(1) == 1_000_000_000
    assert to_lamports(0.25) == 250_000_000
