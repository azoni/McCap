"""Seeding history from GeckoTerminal candles."""

import pytest

from mccapbot import history
from mccapbot.gecko import _timeframe_for


@pytest.fixture(autouse=True)
def clean():
    history.clear()
    yield
    history.clear()


# ---------------- timeframe selection ----------------


@pytest.mark.parametrize("window,expect_tf", [
    (900, "minute"),      # 15m
    (3600, "minute"),     # 1h
    (4 * 3600, "minute"),
    (86400, "hour"),      # 1d
    (5 * 86400, "hour"),
])
def test_timeframe_choice(window, expect_tf):
    tf, agg, limit = _timeframe_for(window)
    assert tf == expect_tf
    # Whatever it picks must actually span the requested window.
    seconds_per_candle = agg * (60 if tf == "minute" else 3600)
    assert seconds_per_candle * limit >= window, "chosen candles don't cover the window"


# ---------------- seeding ----------------


def test_seed_populates_an_empty_series():
    pts = [(1000.0 + i * 300, 100.0 + i) for i in range(12)]
    added = history.seed("A", pts)
    assert added == 12
    assert history.sample_count("A") == 12


def test_seed_enables_pct_change_immediately():
    """The whole point: a fresh alert should be armed, not warming up."""
    now = 100_000.0
    assert history.pct_change("A", 3600, now) is None

    pts = [(now - 3600 + i * 300, 100.0) for i in range(12)]
    pts.append((now, 125.0))
    history.seed("A", pts)

    change = history.pct_change("A", 3600, now)
    assert change is not None, "seeded history should arm the alert"
    assert change == pytest.approx(25.0)


def test_seed_keeps_series_ordered_when_merging_with_live_samples():
    """Backfilled candles are older than live samples and cannot just be
    appended — an unsorted series makes `baseline` read the wrong end."""
    now = 100_000.0
    history.record("A", 130.0, now)          # live sample arrives first
    history.seed("A", [(now - 3600, 100.0), (now - 1800, 110.0)])

    series = list(history._series["A"])
    assert [ts for ts, _ in series] == sorted(ts for ts, _ in series)
    assert history.pct_change("A", 3600, now) == pytest.approx(30.0)


def test_live_samples_win_over_backfilled_on_the_same_timestamp():
    now = 100_000.0
    history.record("A", 999.0, now)
    history.seed("A", [(now, 111.0), (now - 3600, 100.0)])
    assert history.latest("A")[1] == 999.0


def test_seed_ignores_nonpositive_values():
    history.seed("A", [(1.0, 0.0), (2.0, -5.0), (3.0, 10.0)])
    assert history.sample_count("A") == 1


def test_seed_respects_the_sample_cap():
    pts = [(float(i), 100.0) for i in range(history.HISTORY_MAX_SAMPLES + 300)]
    history.seed("A", pts)
    assert history.sample_count("A") <= history.HISTORY_MAX_SAMPLES


def test_empty_seed_is_harmless():
    assert history.seed("A", []) == 0
    assert history.pct_change("A", 3600, now=1000) is None


# ---------------- which network a token lives on ----------------


@pytest.mark.asyncio
async def test_backfill_asks_geckoterminal_about_the_right_network(monkeypatch):
    """Every Robinhood momentum alert used to be blind for half its window after a
    restart: backfill was hard-wired to Solana, so a 0x token found no pool."""
    from mccapbot import gecko
    urls = []

    async def fake_get_json(url, limiter=None, **kw):
        urls.append(url)
        if "/tokens/" in url:
            return {"data": [{"attributes": {"address": "pool1", "name": "TOK / USDG", "reserve_in_usd": "1000"}}]}
        return {"data": {"attributes": {"ohlcv_list": [[2000, 1, 1, 1, 2.0, 5], [1000, 1, 1, 1, 1.0, 5]]}}}
    monkeypatch.setattr(gecko, "get_json", fake_get_json)

    evm = "0x" + "ab" * 20
    assert gecko.network_for(evm) == "robinhood" and gecko.network_for("So11111111111111111111111111111111111111112") == "solana"
    assert await gecko.backfill(evm, 3600, 100.0) == 2
    assert all("/networks/robinhood/" in u for u in urls), urls
    urls.clear()
    await gecko.backfill("So11111111111111111111111111111111111111112", 3600, 100.0)
    assert all("/networks/solana/" in u for u in urls), urls
    urls.clear()
    await gecko.backfill(evm, 3600, 100.0, network="base")
    assert all("/networks/base/" in u for u in urls), "an explicit network wins"


@pytest.mark.asyncio
async def test_tokens_multi_batches_addresses_into_one_request(monkeypatch):
    from mccapbot import gecko
    urls = []

    async def fake_get_json(url, limiter=None, **kw):
        urls.append(url)
        return {"data": [
            {"id": "robinhood_0xa", "type": "token",
             "attributes": {"address": "0xA", "symbol": "A", "total_reserve_in_usd": "12000.5", "decimals": 18}},
            "junk",
            {"attributes": {"symbol": "no-address"}},
        ]}
    monkeypatch.setattr(gecko, "get_json", fake_get_json)

    got = await gecko.tokens_multi(["0xA", "0xb", "0xa", ""])
    assert len(urls) == 1 and urls[0].endswith("/networks/robinhood/tokens/multi/0xa,0xb"), "deduped, lowercased, one call"
    assert got == {"0xa": {"address": "0xA", "symbol": "A", "total_reserve_in_usd": "12000.5", "decimals": 18}}
    assert "0xb" not in got, "a token GeckoTerminal does not know is simply absent"

    await gecko.tokens_multi([f"0x{i:040x}" for i in range(45)], network="base")
    assert "/networks/base/" in urls[1] and urls[1].rsplit("/", 1)[1].count(",") == gecko.TOKENS_MULTI_MAX - 1

    assert await gecko.tokens_multi([]) == {} and len(urls) == 2, "nothing to ask, no request"

    async def down(url, limiter=None, **kw):
        return None
    monkeypatch.setattr(gecko, "get_json", down)
    assert await gecko.tokens_multi(["0xa"]) is None, "a failed request is None, not an empty answer"


@pytest.mark.asyncio
async def test_top_pool_treats_usdg_as_a_major_quote(monkeypatch):
    from mccapbot import gecko

    async def fake_get_json(url, limiter=None, **kw):
        return {"data": [
            {"attributes": {"address": "weth", "name": "TOK / WETH", "reserve_in_usd": "20000"}},
            {"attributes": {"address": "usdg", "name": "TOK / USDG", "reserve_in_usd": "500000"}},
            {"attributes": {"address": "junk", "name": "TOK / NVDA", "reserve_in_usd": "900000"}},
        ]}
    monkeypatch.setattr(gecko, "get_json", fake_get_json)
    pool = await gecko.top_pool("0x" + "ab" * 20, "robinhood")
    assert pool["attributes"]["address"] == "usdg", "the deepest MAJOR-quoted pool, not the deepest pool"
