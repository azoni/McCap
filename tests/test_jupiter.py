"""Jupiter enrichment and the market-cap fallback.

DexScreener stops returning pairs for tokens whose pools thin out, which left a
third of the live alert set permanently unfireable. Jupiter still has a market
cap for most of them.
"""

import asyncio

import pytest

import mccapbot.alerts as A
from mccapbot import jupiter
from mccapbot.cache import token_cache
from mccapbot.jupiter import JupToken, _parse, risk_line
from mccapbot.models import Reminder
from mccapbot.storage import reminders

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
EVM = "0x37cc340fab73ff508c085558f611403810e24444"

RAW = {
    "id": BONK,
    "symbol": "Bonk",
    "name": "Bonk",
    "mcap": 232_000_000.0,
    "holderCount": 993958,
    "organicScoreLabel": "medium",
    "liquidity": 145117.0,
    "audit": {
        "topHoldersPercentage": 31.92,
        "mintAuthorityDisabled": True,
        "freezeAuthorityDisabled": True,
        "devMints": 1,
    },
}


@pytest.fixture(autouse=True)
def clean():
    reminders.clear()
    token_cache.clear()
    A.jup_cache.clear()
    yield
    reminders.clear()
    token_cache.clear()
    A.jup_cache.clear()


# ---------------- parsing ----------------


def test_parse_extracts_the_fields_we_display():
    t = _parse(RAW)
    assert t.ca == BONK and t.symbol == "Bonk"
    assert t.holders == 993958
    assert t.top10_pct == pytest.approx(31.92)
    assert t.mint_disabled is True and t.freeze_disabled is True
    assert t.dev_mints == 1 and t.organic == "medium"


def test_parse_tolerates_missing_audit():
    t = _parse({"id": BONK, "symbol": "X"})
    assert t is not None
    assert t.top10_pct is None and t.mint_disabled is None


def test_parse_rejects_a_record_with_no_mint():
    assert _parse({"symbol": "X"}) is None


def test_parse_survives_junk_numbers():
    t = _parse({"id": BONK, "mcap": "not-a-number", "holderCount": None})
    assert t.mcap is None and t.holders is None


# ---------------- risk line ----------------


def test_risk_line_reports_what_is_known():
    line = risk_line(_parse(RAW), warn_pct=50)
    assert "993,958 holders" in line
    assert "top 10 hold 32%" in line
    assert "⚠️" not in line, "32% is below the warn threshold"


def test_risk_line_flags_concentration():
    raw = {**RAW, "audit": {**RAW["audit"], "topHoldersPercentage": 65.0}}
    assert "⚠️ top 10 hold 65%" in risk_line(_parse(raw), warn_pct=50)


def test_risk_line_flags_live_authorities():
    raw = {**RAW, "audit": {**RAW["audit"], "mintAuthorityDisabled": False}}
    assert "⚠️ mint authority live" in risk_line(_parse(raw), warn_pct=50)


def test_risk_line_omits_unknown_fields_rather_than_zeroing_them():
    """A missing holder count must not render as '0 holders'."""
    line = risk_line(_parse({"id": BONK}), warn_pct=50)
    assert "holders" not in line
    assert "0" not in line


def test_risk_line_empty_without_data():
    assert risk_line(None, warn_pct=50) == ""


def test_concentrated_and_authorities_helpers():
    t = _parse(RAW)
    assert t.concentrated(50) is False
    assert t.concentrated(20) is True
    assert t.authorities_live() is False
    t.mint_disabled = False
    assert t.authorities_live() is True


# ---------------- batching ----------------


def test_evm_addresses_are_filtered_out(monkeypatch):
    """Jupiter is Solana-only; sending EVM mints would waste the request."""
    seen = {}

    async def fake_get_json(url, **kw):
        seen["query"] = kw.get("params", {}).get("query", "")
        return []

    monkeypatch.setattr(jupiter, "get_json", fake_get_json)
    asyncio.run(jupiter.fetch_many([BONK, EVM]))
    assert BONK in seen["query"]
    assert EVM not in seen["query"]


def test_no_request_when_nothing_is_solana(monkeypatch):
    called = False

    async def fake_get_json(url, **kw):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(jupiter, "get_json", fake_get_json)
    assert asyncio.run(jupiter.fetch_many([EVM])) == {}
    assert called is False


def test_batches_respect_the_size_limit(monkeypatch):
    """100 mints per request; 250 tokens must not become one giant URL."""
    calls = []

    async def fake_get_json(url, **kw):
        calls.append(kw["params"]["query"].split(","))
        return []

    monkeypatch.setattr(jupiter, "get_json", fake_get_json)
    # Base58 excludes 0, O, I and l — a suffix built from decimal digits would be
    # filtered out by is_solana_address before any request was made.
    b58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    mints = [
        BONK[:-2] + b58[i // len(b58)] + b58[i % len(b58)]
        for i in range(250)
    ]
    assert len({m for m in mints}) == 250, "fixture must be unique mints"
    asyncio.run(jupiter.fetch_many(mints))
    assert len(calls) == 3, f"250 mints should be 3 batches, got {len(calls)}"
    assert all(len(c) <= 100 for c in calls)
    assert sum(len(c) for c in calls) == 250, "no mint may be dropped"


def test_a_bad_response_yields_nothing_rather_than_raising(monkeypatch):
    async def fake_get_json(url, **kw):
        return {"error": "boom"}   # not a list

    monkeypatch.setattr(jupiter, "get_json", fake_get_json)
    assert asyncio.run(jupiter.fetch_many([BONK])) == {}


# ---------------- the fallback ----------------


def test_jupiter_supplies_a_market_cap_when_dexscreener_has_none(monkeypatch):
    """The whole point: revive alerts DexScreener stopped covering."""
    async def no_pairs(_ca):
        return {"pairs": []}

    monkeypatch.setattr(A, "fetch_dex_token", no_pairs)
    A.jup_cache[BONK] = _parse(RAW)

    assert asyncio.run(A._refresh(BONK)) is True
    snap = token_cache[BONK]
    assert snap.mc == pytest.approx(232_000_000.0)
    assert snap.source == "jupiter"


def test_dexscreener_wins_when_it_has_data(monkeypatch):
    """Jupiter is a fallback, not a replacement — consensus MC stays primary."""
    async def with_pairs(_ca):
        return {"pairs": [{
            "chainId": "solana", "dexId": "raydium",
            "baseToken": {"address": BONK, "symbol": "Bonk", "name": "Bonk"},
            "quoteToken": {"symbol": "SOL"}, "fdv": 5_000_000,
            "liquidity": {"usd": 1000}, "volume": {"h24": 10},
            "txns": {"h24": {"buys": 1, "sells": 1}},
        }]}

    monkeypatch.setattr(A, "fetch_dex_token", with_pairs)
    A.jup_cache[BONK] = _parse(RAW)

    asyncio.run(A._refresh(BONK))
    assert token_cache[BONK].mc == 5_000_000
    assert token_cache[BONK].source == "fdv"


def test_no_fallback_when_jupiter_also_has_nothing(monkeypatch):
    async def no_pairs(_ca):
        return {"pairs": []}

    monkeypatch.setattr(A, "fetch_dex_token", no_pairs)
    asyncio.run(A._refresh(BONK))
    assert token_cache[BONK].mc is None


def test_a_failed_dexscreener_request_still_does_not_write(monkeypatch):
    """A transport failure is not 'no market cap' — the earlier fix must hold."""
    async def fails(_ca):
        return None

    monkeypatch.setattr(A, "fetch_dex_token", fails)
    A.jup_cache[BONK] = _parse(RAW)
    assert asyncio.run(A._refresh(BONK)) is False
    assert BONK not in token_cache


# ---------------- sweep hygiene ----------------


def test_sweep_drops_tokens_nobody_watches(monkeypatch):
    async def fake_fetch_many(_mints):
        return {}

    monkeypatch.setattr(jupiter, "fetch_many", fake_fetch_many)
    A.jup_cache["GONE"] = JupToken(ca="GONE")
    reminders.append(Reminder(
        ca=BONK, target_mc=1.0, direction="above", channel_id=1,
        creator_id=1, guild_id=1, name="B", symbol="B",
    ))
    asyncio.run(A.jupiter_sweep())
    assert "GONE" not in A.jup_cache


def test_sweep_failure_is_swallowed(monkeypatch):
    """Enrichment must never stop an alert firing."""
    async def boom(_mints):
        raise RuntimeError("jupiter down")

    monkeypatch.setattr(jupiter, "fetch_many", boom)
    reminders.append(Reminder(
        ca=BONK, target_mc=1.0, direction="above", channel_id=1,
        creator_id=1, guild_id=1, name="B", symbol="B",
    ))
    assert asyncio.run(A.jupiter_sweep()) == 0
