"""Defects found by an adversarial review of the whole session's diff.

The worst was silent data loss: one unparseable record truncated the whole
list in memory, and the next save wrote that truncation to disk.
"""

import asyncio
import json

import pytest

import mccapbot.alerts as A
from mccapbot import scan, storage
from mccapbot.cogs.alerts import _fit
from mccapbot.helpers import parse_mc_input
from mccapbot.models import MoveAlert

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def rec(i, **over):
    d = {
        "ca": f"CA{i}", "target_mc": 1_000_000.0, "direction": "above",
        "channel_id": 1, "creator_id": 2, "guild_id": 3,
        "name": f"T{i}", "symbol": f"S{i}", "id": f"id{i:04d}",
    }
    d.update(over)
    return d


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "REM_FILE", str(tmp_path / "reminders.json"))
    monkeypatch.setattr(storage, "MOVES_FILE", str(tmp_path / "moves.json"))
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    storage.reminders.clear()
    storage.move_alerts.clear()
    yield tmp_path
    storage.reminders.clear()
    storage.move_alerts.clear()


# ---------------- one bad record must not destroy the rest ----------------


def test_bad_record_does_not_truncate_the_list(store):
    """The whole point: a record missing a required field used to leave the
    in-memory list holding only what came before it, and the next save made
    that permanent."""
    data = [rec(i) for i in range(11)]
    del data[2]["direction"]  # required field
    (store / "moves.json").write_text(json.dumps([
        {**rec(i), "pct": 30, "window_sec": 3600} for i in range(11)
    ]), encoding="utf-8")

    (store / "reminders.json").write_text(json.dumps(data), encoding="utf-8")
    asyncio.run(storage.load_reminders())

    # 10 good records survive; only the malformed one is dropped.
    assert len(storage.reminders) == 10, "good records after the bad one were lost"
    ids = [r.id for r in storage.reminders]
    assert "id0000" in ids and "id0010" in ids


def test_damaged_file_is_preserved_before_any_overwrite(store):
    data = [rec(i) for i in range(5)]
    del data[1]["direction"]
    p = store / "reminders.json"
    p.write_text(json.dumps(data), encoding="utf-8")

    asyncio.run(storage.load_reminders())
    asyncio.run(storage.save_reminders())

    backup = store / "reminders.json.corrupt"
    assert backup.exists(), "the original must survive the truncating save"
    assert len(json.loads(backup.read_text(encoding="utf-8"))) == 5


def test_unreadable_file_leaves_memory_untouched(store):
    storage.move_alerts.append(MoveAlert(
        ca="KEEP", pct=30, window_sec=3600, direction="both", channel_id=1,
        creator_id=2, guild_id=3, name="K", symbol="K",
    ))
    (store / "moves.json").write_text("{ not json at all", encoding="utf-8")
    asyncio.run(storage.load_moves())
    assert len(storage.move_alerts) == 1, "a corrupt file silently emptied memory"


def test_non_array_json_is_refused(store):
    """A JSON object where a list belongs must not be iterated into garbage."""
    storage.move_alerts.append(MoveAlert(
        ca="KEEP", pct=30, window_sec=3600, direction="both", channel_id=1,
        creator_id=2, guild_id=3, name="K", symbol="K",
    ))
    (store / "moves.json").write_text('{"oops": true}', encoding="utf-8")
    asyncio.run(storage.load_moves())
    assert [m.ca for m in storage.move_alerts] == ["KEEP"], "memory was clobbered"


# ---------------- non-finite values ----------------


@pytest.mark.parametrize("v", ["nan", "inf", "-inf", "1e400"])
def test_non_finite_targets_are_rejected(v):
    """meets() can never be satisfied by nan, so the alert would sit forever;
    json.dump also refuses to serialise it."""
    with pytest.raises(ValueError):
        parse_mc_input(v)


def test_ordinary_targets_still_parse():
    assert parse_mc_input("250k") == 250_000
    assert parse_mc_input("2.5m") == 2_500_000


def test_atomic_write_rejects_non_finite(store):
    with pytest.raises(ValueError):
        storage._atomic_write(str(store / "x.json"), [{"v": float("nan")}])


# ---------------- message length ----------------


def test_removal_reply_fits_discord(store):
    """40 removals produced a 2015-character reply, which Discord rejects."""
    lines = ["🗑️ **Removed:**"] + [
        f"• `a1b2c{i:02d}` SomeTokenName (SYMBOL) — MC ≥ $12.35M" for i in range(40)
    ]
    out = _fit(lines)
    assert len(out) <= 2000
    assert "more line" in out, "truncation must be disclosed"


def test_short_reply_is_untouched():
    lines = ["🗑️ **Removed:**", "• `a1b2c3` Tok (TOK) — MC ≥ $1.00M"]
    assert _fit(lines) == "\n".join(lines)


# ---------------- phantom mints ----------------


def test_transaction_signature_is_not_mistaken_for_mints():
    """An 88-char signature was being chopped into two 44-char runs that both
    passed the address check."""
    class M:
        content = "tx 5VfydnLu4XPq5nDDrpEpDwmZmMYRHtaVJRd5oBNjTHnfXKAUFmJLcVvBmoTsjDrBhJnLbTQqPWnkPFVdMFXbLmqE"
        embeds = components = []
        author = None

    assert scan.extract_mints(M()) == []


def test_a_real_mint_is_still_found():
    class M:
        content = f"scanning {BONK}"
        embeds = components = []
        author = None

    assert scan.extract_mints(M()) == [BONK]


# ---------------- auto-alert expiry is not tied to the scan flag ----------------


def test_expiry_runs_from_the_watcher(store):
    """The sweep used to live only in the scan tracker, which never starts when
    SCAN_WATCH_ENABLE is off — so alerts armed earlier were immortal."""
    import time
    old = MoveAlert(
        ca="OLD", pct=30, window_sec=3600, direction="both", channel_id=1,
        creator_id=0, guild_id=3, name="O", symbol="O",
    )
    old.auto_expires_ts = time.time() - 86400
    keep = MoveAlert(
        ca="KEEP", pct=30, window_sec=3600, direction="both", channel_id=1,
        creator_id=7, guild_id=3, name="K", symbol="K",
    )
    storage.move_alerts.extend([old, keep])

    assert asyncio.run(A.expire_auto_alerts()) is True
    assert [m.id for m in storage.move_alerts] == [keep.id]


def test_user_alerts_never_expire(store):
    """auto_expires_ts 0 means user-created; the sweep must not touch it."""
    m = MoveAlert(
        ca="MINE", pct=30, window_sec=3600, direction="both", channel_id=1,
        creator_id=7, guild_id=3, name="M", symbol="M",
    )
    storage.move_alerts.append(m)
    assert asyncio.run(A.expire_auto_alerts()) is False
    assert storage.move_alerts == [m]


# ---------------- fired-alert history survives collection ----------------


def test_collect_keeps_tokens_still_shown_in_history():
    """_collect evicted the cache entry in the same tick an alert fired, so
    /mc_recent's Current column went blank immediately."""
    from mccapbot.cache import token_cache
    from mccapbot.models import AlertEvent, TokenSnapshot

    storage.alert_events.clear()
    token_cache.clear()
    token_cache["FIRED"] = TokenSnapshot(mc=5.0, url="u", updated_ts=0.0)
    storage.alert_events.append(AlertEvent(
        ts=1.0, ca="FIRED", name="F", symbol="F", direction="above",
        target_mc=1.0, current_mc=5.0, channel_id=1, guild_id=1, creator_id=1,
    ))

    asyncio.run(A._collect(set()))  # nothing is watched any more
    assert "FIRED" in token_cache, "history still displays this token"

    storage.alert_events.clear()
    asyncio.run(A._collect(set()))
    assert "FIRED" not in token_cache, "with no history left it should be dropped"
