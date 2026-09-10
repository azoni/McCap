"""Scan persistence, dedupe and the auto-alert expiry that protects the budget."""

import asyncio
import json
import time

import pytest

from mccapbot import storage
from mccapbot.models import MoveAlert, ScanEvent

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def ev(ca=BONK, guild=1, ts=None, mc=100.0):
    return ScanEvent(
        ca=ca, guild_id=guild, channel_id=2, scanner_id=3,
        name="Bonk", symbol="BONK", mc_at_scan=mc, ts=ts if ts is not None else time.time(),
    )


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "SCANS_FILE", str(tmp_path / "scans.json"))
    monkeypatch.setattr(storage, "MOVES_FILE", str(tmp_path / "moves.json"))
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    storage.scan_events.clear()
    storage.move_alerts.clear()
    yield tmp_path
    storage.scan_events.clear()
    storage.move_alerts.clear()


def test_roundtrip(data_dir):
    e = ev()
    e.peak_mc, e.last_mc = 300.0, 200.0
    storage.scan_events.append(e)
    asyncio.run(storage.save_scans())

    storage.scan_events.clear()
    asyncio.run(storage.load_scans())

    assert len(storage.scan_events) == 1
    got = storage.scan_events[0]
    assert got.ca == BONK
    assert got.peak_mc == 300.0
    assert got.multiple() == pytest.approx(3.0)


def test_missing_file_starts_empty(data_dir):
    asyncio.run(storage.load_scans())
    assert storage.scan_events == []


def test_unknown_fields_tolerated(data_dir):
    """Forward compatibility, same as every other store."""
    rec = [{
        "ca": BONK, "guild_id": 1, "channel_id": 2, "scanner_id": 3,
        "name": "B", "symbol": "B", "mc_at_scan": 1.0,
        "a_field_from_the_future": True,
    }]
    (data_dir / "scans.json").write_text(json.dumps(rec), encoding="utf-8")
    asyncio.run(storage.load_scans())
    assert len(storage.scan_events) == 1


# ---------------- dedupe ----------------


def test_recent_scan_detects_a_duplicate(data_dir):
    now = 1_000_000.0
    storage.scan_events.append(ev(ts=now - 60))
    assert storage.recent_scan(BONK, 1, within_sec=900, now=now) is not None


def test_recent_scan_expires(data_dir):
    now = 1_000_000.0
    storage.scan_events.append(ev(ts=now - 5000))
    assert storage.recent_scan(BONK, 1, within_sec=900, now=now) is None


def test_dedupe_is_per_guild(data_dir):
    """The same token scanned in two servers is two separate calls."""
    now = 1_000_000.0
    storage.scan_events.append(ev(guild=1, ts=now - 10))
    assert storage.recent_scan(BONK, 1, 900, now) is not None
    assert storage.recent_scan(BONK, 2, 900, now) is None


def test_dedupe_is_per_token(data_dir):
    now = 1_000_000.0
    storage.scan_events.append(ev(ca="OtherMint1111111111111111111111111111111", ts=now - 10))
    assert storage.recent_scan(BONK, 1, 900, now) is None


# ---------------- tracking window ----------------


def test_scans_to_track_excludes_stale(data_dir):
    now = 1_000_000.0
    fresh = ev(ts=now - 3600)
    stale = ev(ca="Old1111111111111111111111111111111111111", ts=now - 200_000)
    storage.scan_events.extend([fresh, stale])
    tracked = storage.scans_to_track(track_seconds=48 * 3600, now=now)
    assert fresh in tracked and stale not in tracked


def test_tracking_stops_so_dead_tokens_are_not_polled_forever(data_dir):
    now = 1_000_000.0
    storage.scan_events.append(ev(ts=now - 49 * 3600))
    assert storage.scans_to_track(48 * 3600, now) == []


# ---------------- auto-armed alert expiry ----------------


def mv(ca=BONK, expires=0.0):
    m = MoveAlert(
        ca=ca, pct=30, window_sec=3600, direction="both",
        channel_id=1, creator_id=2, guild_id=3, name="B", symbol="B",
    )
    m.auto_expires_ts = expires
    return m


def test_auto_expiry_field_defaults_to_never(data_dir):
    """User-created alerts must never silently expire."""
    assert mv().auto_expires_ts == 0.0


def test_auto_expiry_survives_a_restart(data_dir):
    storage.move_alerts.append(mv(expires=1_234_567.0))
    asyncio.run(storage.save_moves())
    storage.move_alerts.clear()
    asyncio.run(storage.load_moves())
    assert storage.move_alerts[0].auto_expires_ts == 1_234_567.0


def test_legacy_move_alert_without_expiry_loads(data_dir):
    """moves.json written before auto-arming existed must still load."""
    rec = [{
        "ca": BONK, "pct": 30, "window_sec": 3600, "direction": "both",
        "channel_id": 1, "creator_id": 2, "guild_id": 3, "name": "B", "symbol": "B",
        "id": "abc123",
    }]
    (data_dir / "moves.json").write_text(json.dumps(rec), encoding="utf-8")
    asyncio.run(storage.load_moves())
    assert storage.move_alerts[0].auto_expires_ts == 0.0


def test_separating_auto_from_user_alerts(data_dir):
    """The cap and the expiry sweep both key off auto_expires_ts being set."""
    storage.move_alerts.extend([mv(expires=0.0), mv(ca="A" * 40, expires=999.0)])
    auto = [m for m in storage.move_alerts if m.auto_expires_ts]
    assert len(auto) == 1


# ---------------- the discovery feed's document ----------------


@pytest.fixture
def feed_file(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "FEED_FILE", str(tmp_path / "feed.json"))
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    return tmp_path / "feed.json"


def test_feed_document_round_trips(feed_file):
    payload = {"configs": [{"guild_id": 1, "channel_id": 2}], "seen": {"spike:0xabc": 1.5},
               "pending": {"0xabc": {"ca": "0xabc", "tries": 2}}, "posted": [{"ca": "0xabc", "ts": 3.0}]}
    asyncio.run(storage.save_feed(payload))
    assert feed_file.exists()
    assert asyncio.run(storage.load_feed()) == payload


def test_feed_document_missing_file_is_the_empty_shape(feed_file):
    got = asyncio.run(storage.load_feed())
    assert got == storage.empty_feed() and set(got) == set(storage.FEED_KEYS)


def test_feed_document_junk_is_kept_aside_not_replaced(feed_file):
    feed_file.write_text("{not json", encoding="utf-8")
    assert asyncio.run(storage.load_feed()) is None, "unreadable: leave the in-memory state alone"
    assert feed_file.with_suffix(".json.corrupt").exists()
    feed_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert asyncio.run(storage.load_feed()) is None


def test_feed_document_fills_missing_and_mistyped_keys(feed_file):
    feed_file.write_text(json.dumps({"configs": "nope", "seen": {"a": 1.0}}), encoding="utf-8")
    got = asyncio.run(storage.load_feed())
    assert got["configs"] == [] and got["seen"] == {"a": 1.0} and got["pending"] == {} and got["posted"] == []
