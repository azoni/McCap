"""Defects found by auditing the tracking path. None were covered before.

The two serious ones both surfaced as "mc_list and tracking are broken":
a transient fetch failure blanked a live token's cached market cap, and an
alert that hit its target but could not be delivered was deleted anyway.
"""

import asyncio

import discord
import pytest

import mccapbot.alerts as A
from mccapbot import history
from mccapbot.cache import token_cache
from mccapbot.config import POLL_COLD_SECONDS, POLL_UNKNOWN_SECONDS
from mccapbot.models import MoveAlert, Reminder, TokenSnapshot
from mccapbot.scheduler import backoff_interval, due_addresses
from mccapbot.storage import alert_events, reminders


def rem(ca="CA1", target=1_000_000, direction="above", channel=555):
    return Reminder(
        ca=ca, target_mc=target, direction=direction, channel_id=channel,
        creator_id=7, guild_id=3, name="Tok", symbol="TOK",
    )


@pytest.fixture(autouse=True)
def clean():
    reminders.clear()
    alert_events.clear()
    token_cache.clear()
    A._last_checked.clear()
    A._no_data.clear()
    history.clear()
    yield
    reminders.clear()
    alert_events.clear()
    token_cache.clear()
    A._last_checked.clear()
    A._no_data.clear()
    history.clear()


# ---------------- a failed fetch must not blank a good market cap ----------------


def test_failed_fetch_leaves_the_previous_snapshot(monkeypatch):
    """get_json flattens 429/timeout/non-200 to None. Treating that as
    "no market cap" made /mc_list show — for a live token and demoted it to
    the slowest tier right when it might have been about to fire."""
    async def ok(_ca):
        return {"pairs": [{
            "chainId": "solana", "dexId": "raydium",
            "baseToken": {"address": "CA1", "symbol": "TOK", "name": "Tok"},
            "quoteToken": {"symbol": "SOL"},
            "fdv": 5_000_000, "liquidity": {"usd": 1000},
            "volume": {"h24": 10}, "txns": {"h24": {"buys": 1, "sells": 1}},
        }]}

    monkeypatch.setattr(A, "fetch_dex_token", ok)
    assert asyncio.run(A._refresh("CA1")) is True
    assert token_cache["CA1"].mc == 5_000_000

    async def fails(_ca):
        return None  # what a 429 or a timeout looks like here

    monkeypatch.setattr(A, "fetch_dex_token", fails)
    assert asyncio.run(A._refresh("CA1")) is False
    assert token_cache["CA1"].mc == 5_000_000, "a failed request wiped a good market cap"


def test_token_with_genuinely_no_pairs_is_recorded_as_none(monkeypatch):
    """An empty-but-successful response is real information and must land."""
    async def empty(_ca):
        return {"pairs": []}

    monkeypatch.setattr(A, "fetch_dex_token", empty)
    assert asyncio.run(A._refresh("CA1")) is True
    assert "CA1" in token_cache and token_cache["CA1"].mc is None


def test_failed_fetch_does_not_create_a_blank_entry(monkeypatch):
    async def fails(_ca):
        return None

    monkeypatch.setattr(A, "fetch_dex_token", fails)
    asyncio.run(A._refresh("NEW"))
    assert "NEW" not in token_cache


# ---------------- an undeliverable alert must not vanish ----------------


class Chan:
    def __init__(self, exc=None):
        self.exc = exc
        self.sent = 0

    async def send(self, *a, **k):
        if self.exc:
            raise self.exc
        self.sent += 1


class Client:
    def __init__(self, channel=None, fetch_exc=None):
        self.channel = channel or Chan()
        self.fetch_exc = fetch_exc
        self.user = None

    async def fetch_channel(self, _cid):
        if self.fetch_exc:
            raise self.fetch_exc
        return self.channel

    def get_user(self, _uid):
        return None

    async def fetch_user(self, _uid):
        return None


def snap():
    return TokenSnapshot(mc=2_000_000, url="u", updated_ts=0.0)


def test_successful_fire_is_removed_and_recorded():
    r = rem()
    reminders.append(r)
    c = Client()
    assert asyncio.run(A._check_levels(c, {"CA1": snap()})) is True
    assert reminders == []
    assert len(alert_events) == 1


def test_transient_send_failure_keeps_the_alert_armed():
    """A rate limit or gateway blip must not consume the alert."""
    r = rem()
    reminders.append(r)
    c = Client(channel=Chan(exc=RuntimeError("boom")))
    fired = asyncio.run(A._check_levels(c, {"CA1": snap()}))
    assert fired is False
    assert reminders == [r], "alert was consumed despite never being delivered"
    assert alert_events == [], "nothing was delivered, so nothing should be recorded"


def test_deleted_channel_retires_the_alert_but_records_it():
    """404 is permanent — retrying forever is pointless, but the fire happened
    and must still show up in /mc_recent."""
    r = rem()
    reminders.append(r)
    resp = type("R", (), {"status": 404, "reason": "Not Found"})()
    c = Client(fetch_exc=discord.NotFound(resp, "Unknown Channel"))
    fired = asyncio.run(A._check_levels(c, {"CA1": snap()}))
    assert fired is True
    assert reminders == []
    assert len(alert_events) == 1, "an undeliverable fire left no trace anywhere"


def test_missing_permission_retires_the_alert_but_records_it():
    r = rem()
    reminders.append(r)
    resp = type("R", (), {"status": 403, "reason": "Forbidden"})()
    c = Client(channel=Chan(exc=discord.Forbidden(resp, "Missing Permissions")))
    assert asyncio.run(A._check_levels(c, {"CA1": snap()})) is True
    assert reminders == []
    assert len(alert_events) == 1


def test_untriggered_alerts_are_untouched():
    r = rem(target=99_000_000)
    reminders.append(r)
    assert asyncio.run(A._check_levels(Client(), {"CA1": snap()})) is False
    assert reminders == [r]


# ---------------- dead tokens must back off ----------------


def test_backoff_grows_then_caps():
    assert backoff_interval(120, 0) == 120
    assert backoff_interval(120, 1) == 240
    assert backoff_interval(120, 3) == 960
    assert backoff_interval(120, 99) == 3600, "must cap, not grow forever"


def test_dead_token_is_eventually_polled_less_than_a_live_one():
    """The original bug: POLL_UNKNOWN (120s) < POLL_COLD (300s), so a token
    that can never fire was polled 2.5x MORE often than a live one."""
    assert POLL_UNKNOWN_SECONDS < POLL_COLD_SECONDS  # the trap still exists
    assert backoff_interval(POLL_UNKNOWN_SECONDS, 3) > POLL_COLD_SECONDS


def test_due_addresses_honours_the_backoff():
    r = rem(ca="DEAD")
    mc = {"DEAD": None}
    # Fresh: due at the unknown interval.
    assert due_addresses([r], [], mc, {"DEAD": 0.0}, POLL_UNKNOWN_SECONDS, {}) == ["DEAD"]
    # After repeated misses the same elapsed time is no longer enough.
    assert due_addresses([r], [], mc, {"DEAD": 0.0}, POLL_UNKNOWN_SECONDS, {"DEAD": 4}) == []


def test_momentum_alerts_are_never_backed_off():
    """Their window maths depends on a steady sample rate."""
    m = MoveAlert(
        ca="X", pct=30, window_sec=3600, direction="both",
        channel_id=1, creator_id=2, guild_id=3, name="X", symbol="X",
    )
    interval = 3600 // 12
    assert due_addresses([], [m], {"X": None}, {"X": 0.0}, interval, {"X": 99}) == ["X"]


def test_streak_resets_when_data_returns():
    token_cache["CA1"] = TokenSnapshot(mc=None, url="u", updated_ts=0.0)
    A._note_data_state("CA1", True)
    A._note_data_state("CA1", True)
    assert A._no_data["CA1"] == 2

    token_cache["CA1"] = TokenSnapshot(mc=1_000.0, url="u", updated_ts=0.0)
    A._note_data_state("CA1", True)
    assert "CA1" not in A._no_data


def test_request_failure_is_not_counted_against_the_token():
    """A 429 says nothing about whether the token is alive."""
    A._note_data_state("CA1", False)
    assert "CA1" not in A._no_data


# ---------------- state must be collected for every removal path ----------------


def test_collect_drops_state_for_unwatched_tokens():
    """Cleanup used to run only when a level alert fired, so /mc_remove and
    expiring auto-armed scan alerts leaked. token_cache was never pruned at all."""
    token_cache["GONE"] = TokenSnapshot(mc=1.0, url="u", updated_ts=0.0)
    token_cache["KEEP"] = TokenSnapshot(mc=1.0, url="u", updated_ts=0.0)
    A._last_checked.update({"GONE": 1.0, "KEEP": 1.0})
    A._no_data.update({"GONE": 3, "KEEP": 1})
    history.record("GONE", 1.0, 1.0)
    history.record("KEEP", 1.0, 1.0)

    asyncio.run(A._collect({"KEEP"}))

    assert "GONE" not in token_cache and "KEEP" in token_cache
    assert "GONE" not in A._last_checked and "KEEP" in A._last_checked
    assert "GONE" not in A._no_data and "KEEP" in A._no_data
    assert history.sample_count("GONE") == 0
    assert history.sample_count("KEEP") == 1


# ---------------- 1h volume on the snapshot, buttons only on Robinhood Chain alerts ----------------


def test_refresh_stores_the_hour_volume_and_tolerates_a_missing_one(monkeypatch):
    async def with_h1(_ca):
        return {"pairs": [{
            "chainId": "robinhood", "dexId": "uniswap",
            "baseToken": {"address": "0x" + "ab" * 20, "symbol": "TOK", "name": "Tok"},
            "quoteToken": {"symbol": "WETH"}, "fdv": 5_000_000, "liquidity": {"usd": 1000},
            "volume": {"h24": 10, "h1": 3.5}, "txns": {"h24": {"buys": 1, "sells": 1}},
        }]}
    monkeypatch.setattr(A, "fetch_dex_token", with_h1)
    asyncio.run(A._refresh("0x" + "ab" * 20))
    snap_ = token_cache["0x" + "ab" * 20]
    assert snap_.vol1h == 3.5 and snap_.chain == "robinhood"

    async def without_h1(_ca):
        return {"pairs": [{
            "chainId": "solana", "dexId": "raydium",
            "baseToken": {"address": "CA1", "symbol": "TOK", "name": "Tok"},
            "quoteToken": {"symbol": "SOL"}, "fdv": 5_000_000, "liquidity": {"usd": 1000},
            "volume": {"h24": 10}, "txns": {"h24": {"buys": 1, "sells": 1}},
        }]}
    monkeypatch.setattr(A, "fetch_dex_token", without_h1)
    asyncio.run(A._refresh("CA1"))
    assert token_cache["CA1"].vol1h == 0.0


class ChanKw(Chan):
    def __init__(self):
        super().__init__()
        self.kw = []

    async def send(self, *a, **k):
        self.kw.append(k)
        self.sent += 1


def test_alert_buttons_only_for_robinhood_chain_tokens(monkeypatch):
    calls = []
    monkeypatch.setattr(A.views, "alert_row", lambda ca: calls.append(ca) or "ROW", raising=False)
    evm = "0x" + "ab" * 20
    rh_snap = TokenSnapshot(mc=2_000_000, url="u", updated_ts=0.0, chain="robinhood")
    sol_snap = TokenSnapshot(mc=2_000_000, url="u", updated_ts=0.0, chain="solana")
    jup_snap = TokenSnapshot(mc=2_000_000, url="u", updated_ts=0.0, chain="")
    assert A._trade_row(evm, rh_snap) == "ROW" and calls == [evm]
    assert A._trade_row("CA1", rh_snap) is None, "a Solana-shaped address never gets EVM trade buttons"
    assert A._trade_row(evm, sol_snap) is None and A._trade_row(evm, jup_snap) is None and A._trade_row(evm, None) is None

    def boom(ca):
        raise RuntimeError("view bug")
    monkeypatch.setattr(A.views, "alert_row", boom, raising=False)
    assert A._trade_row(evm, rh_snap) is None, "a view bug must never break an alert"

    # The fired alert carries the row only when one was built.
    r = rem()
    r.ca = evm
    reminders.append(r)
    ch = ChanKw()
    monkeypatch.setattr(A.views, "alert_row", lambda ca: "ROW", raising=False)
    assert asyncio.run(A._check_levels(Client(channel=ch), {evm: rh_snap})) is True
    assert ch.kw[-1].get("view") == "ROW"
    r2 = rem()
    reminders.append(r2)
    assert asyncio.run(A._check_levels(Client(channel=ch), {"CA1": snap()})) is True
    assert "view" not in ch.kw[-1]


# ---------------- the snapshot carries the pair's context ----------------


def test_refresh_stores_pair_context_and_survives_a_missing_price_change_block(monkeypatch):
    evm = "0x" + "cd" * 20

    async def rich(_ca):
        return {"pairs": [{
            "chainId": "robinhood", "dexId": "uniswap", "pairAddress": "0xpool", "pairCreatedAt": 1_700_000_000_000,
            "baseToken": {"address": evm, "symbol": "TOK", "name": "Tok"}, "quoteToken": {"symbol": "USDG"},
            "fdv": 5_000_000, "liquidity": {"usd": 28_000}, "volume": {"h24": 10, "h1": 3.5, "m5": 900},
            "txns": {"m5": {"buys": 41, "sells": 6}, "h1": {"buys": 200, "sells": 80}, "h24": {"buys": 1, "sells": 1}},
            "priceChange": {"m5": "31", "h1": "12"},
        }]}
    monkeypatch.setattr(A, "fetch_dex_token", rich)
    asyncio.run(A._refresh(evm))
    s = token_cache[evm]
    assert s.liq_usd == 28_000 and s.buys_m5 == 41 and s.sells_m5 == 6 and s.change_h1 == 12.0 and s.vol_m5 == 900
    assert s.pair_created_ts == 1_700_000_000.0 and s.pair_address == "0xpool"
    assert A.context_line(s) == "liq $28K · 5m 41 buys / 6 sells · 1h +12%"

    async def no_change_block(_ca):
        return {"pairs": [{
            "chainId": "robinhood", "dexId": "uniswap",
            "baseToken": {"address": evm, "symbol": "TOK", "name": "Tok"}, "quoteToken": {"symbol": "USDG"},
            "fdv": 6_000_000, "liquidity": {"usd": 1000}, "volume": {"h24": 10}, "priceChange": "broken",
            "txns": {"h24": {"buys": 1, "sells": 1}},
        }]}
    monkeypatch.setattr(A, "fetch_dex_token", no_change_block)
    assert asyncio.run(A._refresh(evm)) is True
    s = token_cache[evm]
    assert s.mc == 6_000_000 and s.change_m5 is None and s.buys_m5 == 0, "a broken block never stops the market cap"
    assert A.context_line(s) == "liq $1K", "unknown parts are left out, never printed as zero"
    assert A.context_line(None) == ""
