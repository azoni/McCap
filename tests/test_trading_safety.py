"""Guards around real-money order placement.

These matter more than any feature in the bot. A bug in an alert costs a missed
ping; a bug here costs money.
"""

import asyncio
import base64
import json

import pytest

from mccapbot import robinhood, spend
from mccapbot.cogs.trade import _gate
from mccapbot.robinhood import RobinhoodError, quantity_for_usd, sign_message


# ---------------- request signing ----------------


def test_signature_covers_exactly_the_documented_message():
    """Robinhood signs api_key + timestamp + path + method + body, in that
    order. A wrong order means every request is rejected, so pin it against a
    real verify rather than trusting the concatenation by eye."""
    from nacl.signing import SigningKey

    key = SigningKey(b"\x01" * 32)
    sig = sign_message("apikey", 1700000000, "/api/v1/x/", "POST", '{"a":1}', key)

    expected = b"apikey1700000000/api/v1/x/POST" + b'{"a":1}'
    key.verify_key.verify(expected, base64.b64decode(sig))  # raises if wrong


def test_signature_changes_with_every_component():
    from nacl.signing import SigningKey

    key = SigningKey(b"\x02" * 32)
    base = sign_message("k", 1, "/p/", "GET", "", key)
    assert sign_message("k2", 1, "/p/", "GET", "", key) != base
    assert sign_message("k", 2, "/p/", "GET", "", key) != base
    assert sign_message("k", 1, "/q/", "GET", "", key) != base
    assert sign_message("k", 1, "/p/", "POST", "", key) != base
    assert sign_message("k", 1, "/p/", "GET", "{}", key) != base


def test_empty_body_is_omitted_not_rendered():
    """A GET must sign the empty string, not 'None' or '{}'."""
    from nacl.signing import SigningKey

    key = SigningKey(b"\x03" * 32)
    sig = sign_message("k", 5, "/p/", "GET", "", key)
    key.verify_key.verify(b"k5/p/GET", base64.b64decode(sig))


# ---------------- order sizing ----------------


def test_quantity_never_exceeds_the_authorised_amount():
    """Rounding UP would breach the very cap the amount was checked against."""
    for usd, price in [(25.0, 3.0), (10.0, 7.0), (100.0, 63999.13), (5.0, 0.0000071)]:
        qty_s, actual = quantity_for_usd(usd, price)
        assert float(qty_s) * price <= usd + 1e-9, (usd, price, qty_s)
        assert actual <= usd + 1e-9


def test_quantity_is_a_string_to_avoid_float_drift():
    qty, _ = quantity_for_usd(25.0, 3.0)
    assert isinstance(qty, str)


def test_zero_or_negative_price_is_refused():
    with pytest.raises(RobinhoodError):
        quantity_for_usd(10.0, 0)
    with pytest.raises(RobinhoodError):
        quantity_for_usd(10.0, -5)


def test_amount_too_small_to_buy_anything_is_refused():
    """Better an explicit refusal than a zero-quantity order."""
    with pytest.raises(RobinhoodError):
        quantity_for_usd(0.000000001, 100_000.0)


def test_invalid_side_is_refused():
    async def go():
        await robinhood.place_market_order("BTC-USD", "sideways", "1")

    with pytest.raises(RobinhoodError):
        asyncio.run(go())


# ---------------- the owner gate ----------------


def test_trading_is_off_by_default(monkeypatch):
    import mccapbot.cogs.trade as t

    monkeypatch.setattr(t, "RH_TRADING_ENABLE", False)
    assert "disabled" in _gate().lower()


def test_unset_owner_blocks_everyone(monkeypatch):
    """An empty owner must never be read as 'anyone may trade'."""
    import mccapbot.cogs.trade as t

    monkeypatch.setattr(t, "RH_TRADING_ENABLE", True)
    monkeypatch.setattr(t, "RH_OWNER_ID", 0)
    monkeypatch.setattr(robinhood, "RH_API_KEY", "k")
    monkeypatch.setattr(robinhood, "RH_PRIVATE_KEY_B64", "x")
    reason = _gate()
    assert reason and "owner" in reason.lower()


def test_missing_credentials_block_trading(monkeypatch):
    import mccapbot.cogs.trade as t

    monkeypatch.setattr(t, "RH_TRADING_ENABLE", True)
    monkeypatch.setattr(t, "RH_OWNER_ID", 123)
    monkeypatch.setattr(robinhood, "RH_API_KEY", "")
    monkeypatch.setattr(robinhood, "RH_PRIVATE_KEY_B64", "")
    assert "credentials" in _gate().lower()


def test_fully_configured_gate_opens(monkeypatch):
    import mccapbot.cogs.trade as t

    monkeypatch.setattr(t, "RH_TRADING_ENABLE", True)
    monkeypatch.setattr(t, "RH_OWNER_ID", 123)
    monkeypatch.setattr(robinhood, "RH_API_KEY", "k")
    monkeypatch.setattr(robinhood, "RH_PRIVATE_KEY_B64", "x")
    assert _gate() is None


def test_unconfigured_client_refuses_to_request(monkeypatch):
    monkeypatch.setattr(robinhood, "RH_API_KEY", "")
    monkeypatch.setattr(robinhood, "RH_PRIVATE_KEY_B64", "")

    async def go():
        await robinhood.get_account()

    with pytest.raises(RobinhoodError):
        asyncio.run(go())


# ---------------- spend caps ----------------


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(spend, "RH_SPEND_FILE", str(tmp_path / "rh_spend.json"))
    monkeypatch.setattr(spend, "RH_MAX_TRADE_USD", 25.0)
    monkeypatch.setattr(spend, "RH_MAX_DAILY_USD", 100.0)
    return tmp_path


def test_within_caps_is_allowed(ledger):
    assert spend.check(20.0)[0] is True


def test_per_trade_cap_is_enforced(ledger):
    ok, why = spend.check(25.01)
    assert ok is False and "per-trade" in why


def test_zero_and_negative_are_refused(ledger):
    assert spend.check(0)[0] is False
    assert spend.check(-5)[0] is False


def test_daily_cap_accumulates(ledger):
    for _ in range(4):
        assert spend.check(25.0)[0] is True
        spend.record(25.0)
    assert spend.spent_today() == 100.0
    ok, why = spend.check(1.0)
    assert ok is False and "daily cap" in why


def test_spend_survives_a_restart(ledger):
    """An in-memory counter would reset on every deploy — that is not a cap."""
    spend.record(40.0)
    assert spend.spent_today() == 40.0
    # A "restart" is just reading the file again; nothing is cached in-process.
    assert json.loads((ledger / "rh_spend.json").read_text(encoding="utf-8"))
    assert spend.spent_today() == 40.0
    assert spend.remaining() == 60.0


def test_unreadable_ledger_fails_closed(ledger, monkeypatch):
    """If we cannot tell what has been spent, refuse rather than assume zero."""
    (ledger / "rh_spend.json").write_text("{ not json", encoding="utf-8")
    ok, why = spend.check(1.0)
    assert ok is False
    assert "ledger" in why.lower()


def test_a_new_day_resets_the_budget(ledger):
    day1 = 1_700_000_000.0            # some UTC day
    day2 = day1 + 86_400
    spend.record(100.0, now=day1)
    assert spend.check(10.0, now=day1)[0] is False
    assert spend.check(10.0, now=day2)[0] is True


def test_ledger_does_not_grow_forever(ledger):
    for i in range(60):
        spend.record(1.0, now=1_700_000_000.0 + i * 86_400)
    data = json.loads((ledger / "rh_spend.json").read_text(encoding="utf-8"))
    assert len(data) <= 31, "old days should be pruned"


def test_remaining_never_goes_negative(ledger):
    spend.record(500.0)
    assert spend.remaining() == 0.0
