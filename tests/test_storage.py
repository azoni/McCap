"""Storage must survive restarts and tolerate files written by the old schema."""

import asyncio
import json

import pytest

from mccapbot import storage
from mccapbot.models import Reminder


def mk(name="tok"):
    return Reminder(
        ca=f"CA-{name}",
        target_mc=1_000_000,
        direction="above",
        channel_id=1,
        creator_id=2,
        guild_id=3,
        name=name,
        symbol=name.upper(),
    )


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "REM_FILE", str(tmp_path / "reminders.json"))
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    storage.reminders.clear()
    yield tmp_path
    storage.reminders.clear()


def test_roundtrip(data_dir):
    storage.reminders.append(mk("alpha"))
    asyncio.run(storage.save_reminders())

    storage.reminders.clear()
    asyncio.run(storage.load_reminders())

    assert len(storage.reminders) == 1
    assert storage.reminders[0].name == "alpha"
    assert storage.reminders[0].id


def test_legacy_file_without_id_is_backfilled(data_dir):
    """Files written before alerts had stable ids must still load."""
    legacy = [
        {
            "ca": "CA-legacy",
            "target_mc": 500000.0,
            "direction": "above",
            "channel_id": 1,
            "creator_id": 2,
            "guild_id": 3,
            "name": "Legacy",
            "symbol": "LEG",
            "note": "",
        }
    ]
    (data_dir / "reminders.json").write_text(json.dumps(legacy), encoding="utf-8")

    asyncio.run(storage.load_reminders())

    assert len(storage.reminders) == 1
    r = storage.reminders[0]
    assert r.name == "Legacy"
    assert r.id, "an id should have been generated"
    assert r.created_ts > 0

    # And the backfill is written back to disk.
    on_disk = json.loads((data_dir / "reminders.json").read_text(encoding="utf-8"))
    assert on_disk[0]["id"] == r.id


def test_unknown_fields_are_ignored(data_dir):
    """A field removed in a later version shouldn't break loading."""
    rec = [
        {
            "ca": "CA-x",
            "target_mc": 1.0,
            "direction": "above",
            "channel_id": 1,
            "creator_id": 2,
            "guild_id": 3,
            "name": "X",
            "symbol": "X",
            "some_removed_field": "boom",
        }
    ]
    (data_dir / "reminders.json").write_text(json.dumps(rec), encoding="utf-8")
    asyncio.run(storage.load_reminders())
    assert len(storage.reminders) == 1


def test_missing_file_starts_empty(data_dir):
    asyncio.run(storage.load_reminders())
    assert storage.reminders == []


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

    # Original content intact, and no stray temp files left behind.
    assert (data_dir / "reminders.json").read_text(encoding="utf-8") == good
    assert not list(data_dir.glob("*.tmp"))


def test_find_reminder_is_guild_scoped(data_dir):
    a = mk("a")
    storage.reminders.append(a)
    assert storage.find_reminder(a.id, guild_id=3) is a
    assert storage.find_reminder(a.id, guild_id=999) is None
    assert storage.find_reminder("nope", guild_id=3) is None
