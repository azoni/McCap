"""Storage must survive restarts and tolerate files written by older versions."""

import asyncio
import json

import pytest

from mccapbot import storage
from mccapbot.models import MoveAlert, Reminder, WatchItem


def mk(name="tok"):
    return Reminder(
        ca=f"CA-{name}", target_mc=1_000_000, direction="above", channel_id=1,
        creator_id=2, guild_id=3, name=name, symbol=name.upper(),
    )


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "REM_FILE", str(tmp_path / "reminders.json"))
    monkeypatch.setattr(storage, "MOVES_FILE", str(tmp_path / "moves.json"))
    monkeypatch.setattr(storage, "WATCH_FILE", str(tmp_path / "watchlists.json"))
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    for lst in (storage.reminders, storage.move_alerts, storage.watchlist, storage.alert_events):
        lst.clear()
    yield tmp_path
    for lst in (storage.reminders, storage.move_alerts, storage.watchlist, storage.alert_events):
        lst.clear()


def test_reminder_roundtrip(data_dir):
    storage.reminders.append(mk("alpha"))
    asyncio.run(storage.save_reminders())
    storage.reminders.clear()
    asyncio.run(storage.load_reminders())
    assert len(storage.reminders) == 1
    assert storage.reminders[0].name == "alpha"
    assert storage.reminders[0].id


def test_legacy_file_without_id_is_backfilled(data_dir):
    """Files written before alerts had stable ids must still load."""
    legacy = [{
        "ca": "CA-legacy", "target_mc": 500000.0, "direction": "above",
        "channel_id": 1, "creator_id": 2, "guild_id": 3,
        "name": "Legacy", "symbol": "LEG", "note": "",
    }]
    (data_dir / "reminders.json").write_text(json.dumps(legacy), encoding="utf-8")
    asyncio.run(storage.load_reminders())

    r = storage.reminders[0]
    assert r.id and r.created_ts > 0
    on_disk = json.loads((data_dir / "reminders.json").read_text(encoding="utf-8"))
    assert on_disk[0]["id"] == r.id


def test_legacy_file_without_spec_fields_loads(data_dir):
    """Relative-target fields were added after the first production deploy."""
    legacy = [{
        "ca": "CA-x", "target_mc": 1.0, "direction": "above", "channel_id": 1,
        "creator_id": 2, "guild_id": 3, "name": "X", "symbol": "X",
        "id": "abc123", "created_ts": 100.0,
    }]
    (data_dir / "reminders.json").write_text(json.dumps(legacy), encoding="utf-8")
    asyncio.run(storage.load_reminders())
    r = storage.reminders[0]
    assert r.spec == ""
    assert r.anchor_mc is None


def test_unknown_fields_are_ignored(data_dir):
    """Fields removed in a later version shouldn't break loading."""
    rec = [{
        "ca": "CA-x", "target_mc": 1.0, "direction": "above", "channel_id": 1,
        "creator_id": 2, "guild_id": 3, "name": "X", "symbol": "X",
        "some_removed_field": "boom",
    }]
    (data_dir / "reminders.json").write_text(json.dumps(rec), encoding="utf-8")
    asyncio.run(storage.load_reminders())
    assert len(storage.reminders) == 1


def test_missing_file_starts_empty(data_dir):
    asyncio.run(storage.load_reminders())
    asyncio.run(storage.load_moves())
    asyncio.run(storage.load_watchlist())
    assert storage.reminders == [] and storage.move_alerts == [] and storage.watchlist == []


def test_move_alert_roundtrip(data_dir):
    storage.move_alerts.append(
        MoveAlert(ca="CA-m", pct=30, window_sec=3600, direction="both",
                  channel_id=1, creator_id=2, guild_id=3, name="M", symbol="M")
    )
    asyncio.run(storage.save_moves())
    storage.move_alerts.clear()
    asyncio.run(storage.load_moves())
    assert len(storage.move_alerts) == 1
    m = storage.move_alerts[0]
    assert m.pct == 30 and m.window_sec == 3600 and m.direction == "both"


def test_move_cooldown_state_persists(data_dir):
    """last_fired_ts must survive a restart or a redeploy re-fires everything."""
    m = MoveAlert(ca="CA-m", pct=30, window_sec=3600, direction="both",
                  channel_id=1, creator_id=2, guild_id=3, name="M", symbol="M")
    m.last_fired_ts = 12345.0
    storage.move_alerts.append(m)
    asyncio.run(storage.save_moves())
    storage.move_alerts.clear()
    asyncio.run(storage.load_moves())
    assert storage.move_alerts[0].last_fired_ts == 12345.0


def test_watchlist_roundtrip(data_dir):
    storage.watchlist.append(
        WatchItem(ca="CA-w", guild_id=3, added_by=2, name="W", symbol="W", list_name="majors")
    )
    asyncio.run(storage.save_watchlist())
    storage.watchlist.clear()
    asyncio.run(storage.load_watchlist())
    assert storage.watchlist[0].list_name == "majors"


def test_write_is_atomic(data_dir):
    """A failed serialization must not truncate the previous good file."""
    storage.reminders.append(mk("keep"))
    asyncio.run(storage.save_reminders())
    good = (data_dir / "reminders.json").read_text(encoding="utf-8")

    class Unserializable:
        pass

    storage.reminders.append(Unserializable())  # type: ignore[arg-type]
    with pytest.raises(Exception):
        asyncio.run(storage.save_reminders())

    assert (data_dir / "reminders.json").read_text(encoding="utf-8") == good
    assert not list(data_dir.glob("*.tmp"))


def test_find_reminder_is_guild_scoped(data_dir):
    a = mk("a")
    storage.reminders.append(a)
    assert storage.find_reminder(a.id, guild_id=3) is a
    assert storage.find_reminder(a.id, guild_id=999) is None


def test_watched_addresses_unions_both_alert_types(data_dir):
    storage.reminders.append(mk("a"))          # CA-a
    storage.move_alerts.append(
        MoveAlert(ca="CA-a", pct=10, window_sec=3600, direction="up",
                  channel_id=1, creator_id=2, guild_id=3, name="A", symbol="A")
    )
    storage.move_alerts.append(
        MoveAlert(ca="CA-b", pct=10, window_sec=3600, direction="up",
                  channel_id=1, creator_id=2, guild_id=3, name="B", symbol="B")
    )
    addrs = sorted(storage.watched_addresses())
    assert addrs == ["CA-a", "CA-b"], "a shared token must only be polled once"
