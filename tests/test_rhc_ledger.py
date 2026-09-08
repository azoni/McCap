"""Per-user daily caps and the trade journal for Robinhood Chain."""

import os

import pytest

from mccapbot.rhc import ledger


@pytest.fixture(autouse=True)
def clean_files():
    for p in (ledger.RHC_LEDGER_FILE, ledger.RHC_JOURNAL_FILE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    yield
    for p in (ledger.RHC_LEDGER_FILE, ledger.RHC_JOURNAL_FILE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def test_caps_are_per_user(monkeypatch):
    monkeypatch.setattr(ledger, "RHC_MAX_TRADE_USD", 25.0)
    monkeypatch.setattr(ledger, "RHC_MAX_DAILY_USD", 50.0)
    t = 1_700_000_000.0
    assert ledger.check(1, 25.0, t) == (True, "")
    assert not ledger.check(1, 25.01, t)[0]
    assert not ledger.check(1, 0, t)[0]
    assert ledger.record(1, 25.0, t) == 25.0
    assert ledger.record(1, 20.0, t) == 45.0
    ok, why = ledger.check(1, 10.0, t)
    assert not ok and "daily cap" in why
    # Another user has their own budget.
    assert ledger.check(2, 25.0, t) == (True, "")
    assert ledger.remaining(1, t) == pytest.approx(5.0)
    # A new UTC day starts fresh.
    assert ledger.remaining(1, t + 86_400) == pytest.approx(50.0)


def test_unreadable_ledger_blocks_buying():
    os.makedirs(os.path.dirname(ledger.RHC_LEDGER_FILE), exist_ok=True)
    with open(ledger.RHC_LEDGER_FILE, "w", encoding="utf-8") as f:
        f.write("{broken")
    ok, why = ledger.check(1, 5.0)
    assert not ok and "unreadable" in why
    assert ledger.remaining(1) == 0.0


def test_journal_records_and_lists_tokens_most_recent_first():
    ledger.journal({"user_id": 1, "kind": "buy", "token": "0xAAA", "tx": "0x1"})
    ledger.journal({"user_id": 1, "kind": "buy", "token": "0xbbb", "tx": "0x2"})
    ledger.journal({"user_id": 2, "kind": "buy", "token": "0xccc", "tx": "0x3"})
    ledger.journal({"user_id": 1, "kind": "sell", "token": "0xaaa", "tx": "0x4"})
    assert [e["tx"] for e in ledger.entries_for(1)] == ["0x1", "0x2", "0x4"]
    assert ledger.tokens_touched(1) == ["0xaaa", "0xbbb"]
    assert ledger.tokens_touched(2) == ["0xccc"]


def test_journal_failure_never_raises(monkeypatch):
    def boom(path, data):
        raise OSError("disk full")
    monkeypatch.setattr(ledger, "_write", boom)
    ledger.journal({"user_id": 1, "kind": "buy", "token": "0xaaa", "tx": "0x1"})  # must not raise


def test_wrong_shape_files_are_kept_and_block_rather_than_overwritten():
    os.makedirs(os.path.dirname(ledger.RHC_LEDGER_FILE), exist_ok=True)
    with open(ledger.RHC_LEDGER_FILE, "w", encoding="utf-8") as f:
        f.write("[]")                       # a list where a dict belongs
    ok, why = ledger.check(1, 5.0)
    assert not ok and "unreadable" in why
    assert os.path.exists(ledger.RHC_LEDGER_FILE + ".corrupt")
    with open(ledger.RHC_JOURNAL_FILE, "w", encoding="utf-8") as f:
        f.write("{}")                       # a dict where a list belongs
    assert ledger.entries_for(1) == []
    ledger.journal({"user_id": 1, "kind": "buy", "tx": "0x1"})   # logs, does not raise, does not overwrite
    assert open(ledger.RHC_JOURNAL_FILE, encoding="utf-8").read() == "{}"
    assert os.path.exists(ledger.RHC_JOURNAL_FILE + ".corrupt")
    for p in (ledger.RHC_LEDGER_FILE + ".corrupt", ledger.RHC_JOURNAL_FILE + ".corrupt"):
        os.remove(p)


def test_unresolved_follows_the_latest_status_per_transaction():
    ledger.journal({"ts": 1.0, "user_id": 1, "kind": "buy", "token": "0xaaa", "tx": "0xA", "status": "submitted", "usd_in": 24.8})
    ledger.journal({"ts": 2.0, "user_id": 1, "kind": "resolution", "tx": "0xa", "status": "confirmed"})
    ledger.journal({"ts": 3.0, "user_id": 2, "kind": "withdraw", "tx": "0xB", "status": "submitted"})
    ledger.journal({"ts": 4.0, "user_id": 3, "kind": "buy", "token": "0xccc", "tx": "0xC", "status": "submitted"})
    ledger.journal({"ts": 5.0, "user_id": 3, "kind": "resolution", "tx": "0xC", "status": "dropped"})
    assert ledger.unresolved() == [(2, "0xb", 3.0)]
    assert ledger.entry_for_tx(1, "0xa")["kind"] == "buy"
    assert ledger.entry_for_tx(1, "0xzz") is None


def test_refund_never_goes_negative():
    t = 1_700_000_000.0
    ledger.record(1, 10.0, t)
    assert ledger.refund(1, 25.0, t) == 0.0
    assert ledger.spent_today(1, t) == 0.0
