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


# ---------------- auto-orders ----------------


def test_auto_order_roundtrip_keeps_zero_valued_fields(data_dir, monkeypatch):
    from mccapbot.models import AutoOrder
    monkeypatch.setattr(storage, "RHC_ORDERS_FILE", str(data_dir / "rhc_orders.json"))
    storage.auto_orders.clear()
    o = AutoOrder(ca="0x" + "ab" * 20, symbol="PONS", decimals=18, side="sell", metric="mc", direction="above",
                  target=500_000.0, size=50.0, slippage_bps=200, user_id=7, guild_id=0, channel_id=99,
                  expires_ts=1_800_000_000.0, spec="", anchor="", private=False, attempts=0)
    storage.auto_orders.append(o)
    asyncio.run(storage.save_orders())
    storage.auto_orders.clear()
    asyncio.run(storage.load_orders())
    got = storage.auto_orders[0]
    assert got.id == o.id and got.guild_id == 0 and got.spec == "" and got.attempts == 0 and got.private is False
    assert got.status == "armed" and got.target_mc == 500_000.0
    storage.auto_orders.clear()


def test_watched_addresses_and_scheduler_proxies_include_armed_orders(data_dir):
    from mccapbot.models import AutoOrder
    storage.auto_orders.clear()
    armed = AutoOrder(ca="0xORDER", symbol="X", decimals=18, side="buy", metric="vol1h", direction="above", target=50_000.0,
                      size=10.0, slippage_bps=200, user_id=7, guild_id=1, channel_id=1, expires_ts=9e9)
    done = AutoOrder(ca="0xDONE", symbol="Y", decimals=18, side="sell", metric="mc", direction="below", target=1.0,
                     size=100.0, slippage_bps=200, user_id=7, guild_id=1, channel_id=1, expires_ts=9e9, status="pending")
    mc_order = AutoOrder(ca="0xMC", symbol="Z", decimals=18, side="sell", metric="mc", direction="above", target=2e6,
                         size=50.0, slippage_bps=200, user_id=8, guild_id=1, channel_id=1, expires_ts=9e9)
    storage.auto_orders.extend([armed, done, mc_order])
    storage.reminders.append(mk("alpha"))
    assert set(storage.watched_addresses()) == {"CA-alpha", "0xORDER", "0xMC"}, "pending orders need no price feed"
    levels, moves = storage.order_watchers()
    assert [m.ca for m in moves] == ["0xORDER", "0xMC"] and all(m.window_sec == storage.ORDER_POLL_WINDOW_SEC for m in moves)
    assert [(lv.ca, lv.direction, lv.target_mc) for lv in levels] == [("0xMC", "above", 2e6)], "only market-cap rules get a level proxy"
    assert storage.find_order(mc_order.id, 8) is mc_order and storage.find_order(mc_order.id, 7) is None
    assert storage.orders_for(7) == [armed, done]
    storage.auto_orders.clear()
