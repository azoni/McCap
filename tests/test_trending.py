"""/rh_trending — movement data for Robinhood-listed coins.

Robinhood's API has no trending endpoint, so the pair list comes from Robinhood
(authoritative) and the movement from CoinGecko.
"""

import asyncio

import pytest

from mccapbot import coingecko, robinhood

ROW = {
    "symbol": "btc",
    "name": "Bitcoin",
    "current_price": 78148.0,
    "price_change_percentage_1h_in_currency": 0.1,
    "price_change_percentage_24h_in_currency": 0.75,
    "price_change_percentage_7d_in_currency": 1.4,
    "market_cap": 1_500_000_000_000,
}


@pytest.fixture(autouse=True)
def clear_cache():
    coingecko._cache.clear()
    coingecko._cached_at = 0.0
    yield
    coingecko._cache.clear()
    coingecko._cached_at = 0.0


# ---------------- parsing ----------------


def test_markets_are_keyed_by_upper_symbol(monkeypatch):
    async def fake(url, **kw):
        return [ROW]

    monkeypatch.setattr(coingecko, "get_json", fake)
    markets = asyncio.run(coingecko.top_markets())
    assert "BTC" in markets
    assert markets["BTC"].change_24h == pytest.approx(0.75)


def test_largest_market_cap_wins_a_ticker_collision(monkeypatch):
    """Tickers are not unique. Ordered by market cap, the first match is the
    real one — otherwise a micro-cap impostor sits next to a Robinhood listing."""
    async def fake(url, **kw):
        return [
            {**ROW, "name": "Bitcoin", "market_cap": 1e12, "current_price": 78148.0},
            {**ROW, "name": "Bitcoin Impostor", "market_cap": 1e4, "current_price": 0.01},
        ]

    monkeypatch.setattr(coingecko, "get_json", fake)
    markets = asyncio.run(coingecko.top_markets())
    assert markets["BTC"].name == "Bitcoin"
    assert markets["BTC"].price == 78148.0


def test_missing_change_fields_become_none_not_zero(monkeypatch):
    """A coin with no 7d figure must not be ranked as though it were flat."""
    async def fake(url, **kw):
        return [{"symbol": "eth", "name": "Ether", "current_price": 2456.0}]

    monkeypatch.setattr(coingecko, "get_json", fake)
    coin = asyncio.run(coingecko.top_markets())["ETH"]
    assert coin.change_24h is None and coin.change_7d is None


def test_junk_rows_are_skipped(monkeypatch):
    async def fake(url, **kw):
        return [ROW, "not-a-dict", {"no_symbol": True}]

    monkeypatch.setattr(coingecko, "get_json", fake)
    assert list(asyncio.run(coingecko.top_markets())) == ["BTC"]


def test_a_bad_response_keeps_the_previous_cache(monkeypatch):
    """A CoinGecko blip must not blank the data we already had."""
    async def good(url, **kw):
        return [ROW]

    monkeypatch.setattr(coingecko, "get_json", good)
    asyncio.run(coingecko.top_markets())

    async def bad(url, **kw):
        return {"error": "rate limited"}

    monkeypatch.setattr(coingecko, "get_json", bad)
    markets = asyncio.run(coingecko.top_markets(force=True))
    assert "BTC" in markets, "a failed refresh emptied the cache"


def test_results_are_cached(monkeypatch):
    calls = []

    async def counting(url, **kw):
        calls.append(1)
        return [ROW]

    monkeypatch.setattr(coingecko, "get_json", counting)
    asyncio.run(coingecko.top_markets())
    asyncio.run(coingecko.top_markets())
    assert len(calls) == 1, "second call should be served from cache"


def test_for_symbols_filters_to_what_was_asked(monkeypatch):
    async def fake(url, **kw):
        return [ROW, {**ROW, "symbol": "doge", "name": "Dogecoin"}]

    monkeypatch.setattr(coingecko, "get_json", fake)
    got = asyncio.run(coingecko.for_symbols(["BTC", "NOPE"]))
    assert [c.symbol for c in got] == ["BTC"]


# ---------------- the tradeable pair list ----------------


def test_without_credentials_the_fallback_is_flagged_unverified(monkeypatch):
    """The command must say the list is approximate rather than imply Robinhood
    confirmed it."""
    monkeypatch.setattr(robinhood, "RH_API_KEY", "")
    monkeypatch.setattr(robinhood, "RH_PRIVATE_KEY_B64", "")
    symbols, authoritative = asyncio.run(robinhood.get_trading_pairs())
    assert authoritative is False
    assert "BTC" in symbols and len(symbols) > 5


def test_live_pairs_are_marked_authoritative(monkeypatch):
    async def fake_request(method, path, body=None):
        return {"results": [
            {"symbol": "BTC-USD", "status": "tradable"},
            {"symbol": "ETH-USD", "status": "tradable"},
        ]}

    monkeypatch.setattr(robinhood, "RH_API_KEY", "k")
    monkeypatch.setattr(robinhood, "RH_PRIVATE_KEY_B64", "x")
    monkeypatch.setattr(robinhood, "_request", fake_request)
    symbols, authoritative = asyncio.run(robinhood.get_trading_pairs())
    assert authoritative is True
    assert symbols == ["BTC", "ETH"]


def test_untradeable_pairs_are_excluded(monkeypatch):
    """No point offering a mover you cannot actually buy."""
    async def fake_request(method, path, body=None):
        return {"results": [
            {"symbol": "BTC-USD", "status": "tradable"},
            {"symbol": "OLD-USD", "status": "delisted"},
        ]}

    monkeypatch.setattr(robinhood, "RH_API_KEY", "k")
    monkeypatch.setattr(robinhood, "RH_PRIVATE_KEY_B64", "x")
    monkeypatch.setattr(robinhood, "_request", fake_request)
    symbols, _ = asyncio.run(robinhood.get_trading_pairs())
    assert symbols == ["BTC"]


def test_a_failed_pairs_call_falls_back_rather_than_erroring(monkeypatch):
    async def boom(method, path, body=None):
        raise robinhood.RobinhoodError("upstream down")

    monkeypatch.setattr(robinhood, "RH_API_KEY", "k")
    monkeypatch.setattr(robinhood, "RH_PRIVATE_KEY_B64", "x")
    monkeypatch.setattr(robinhood, "_request", boom)
    symbols, authoritative = asyncio.run(robinhood.get_trading_pairs())
    assert authoritative is False and symbols
