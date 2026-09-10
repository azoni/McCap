"""The signals a board row can carry without spending a request, and the one
that does spend two.

A trending board used to say what a token was worth and how much it had moved.
Neither tells a trader whether the token is worth buying now: a chart that is
40% under its own high with wallets still buying it is a different proposition
from the same market cap on the way down with nobody left. These are the
figures that separate the two, and every one of them has to be absent rather
than wrong when the data behind it is missing.
"""

import time

import pytest

from mccapbot import gecko, rhchain

NOW = 1_700_000_000.0


def pool(addr="0xp", *, quote="USDG", liq=100_000.0, mc=1_000_000.0, price=1.0,
         vol=None, chg=None, tx=None, created=NOW - 86_400):
    return rhchain.Pool(
        address=addr, name=f"TOK / {quote}", dex="Uniswap V3", base_symbol="TOK", base_name="Tok",
        base_address="0xtok", quote_symbol=quote, price_usd=price, liq_usd=liq, mc_usd=mc,
        volume=vol or {}, change=chg or {}, buys_h24=0, sells_h24=0, created_ts=created, tx=tx or {},
    )


def token(*pools):
    return rhchain.TokenActivity(symbol="TOK", name="Tok", address="0xtok", pools=list(pools) or [pool()])


# ---------------- off the high, from the windows already in hand ----------------


def test_the_market_cap_path_walks_back_through_every_window_that_has_a_change():
    t = token(pool(mc=1_000_000.0, chg={"m5": 25.0, "h1": 100.0}))
    path = dict(t.mc_path())
    assert path[0] == 1_000_000.0
    assert path[rhchain.WINDOW_SECONDS["m5"]] == pytest.approx(800_000.0)
    assert path[rhchain.WINDOW_SECONDS["h1"]] == pytest.approx(500_000.0)
    assert t.mc_high() == pytest.approx(1_000_000.0), "it has only gone up"
    assert t.off_high() == pytest.approx(0.0)


def test_a_token_well_under_its_recent_high_says_so_as_a_negative_percentage():
    t = token(pool(mc=600_000.0, chg={"h1": -40.0}))          # $1M an hour ago
    assert t.mc_high() == pytest.approx(1_000_000.0)
    assert t.off_high() == pytest.approx(-40.0)


def test_no_market_cap_means_no_path_and_no_off_high_rather_than_zero():
    t = token(pool(mc=None, chg={"h1": -40.0}))
    assert t.mc_path() == [] and t.mc_high() is None and t.off_high() is None


# ---------------- who is trading it, and how deep the pool is ----------------


def test_buyer_share_is_the_wallets_buying_out_of_everyone_who_traded():
    t = token(pool(tx={"h1": {"buyers": 30, "sellers": 10}}))
    assert t.buyer_share("h1") == pytest.approx(75.0)
    assert token(pool(tx={"h1": {"buyers": 0, "sellers": 0}})).buyer_share("h1") is None, "nobody traded"
    assert token(pool()).buyer_share("h1") is None


def test_turnover_is_how_many_times_the_pool_traded_itself_over():
    """A big pool nobody trades and a small one trading hard look identical by
    volume alone; the ratio is what separates them."""
    t = token(pool(liq=100_000.0, vol={"h24": 500_000.0}))
    assert t.turnover("h24") == pytest.approx(5.0)
    assert token(pool(liq=0.0, vol={"h24": 5.0})).turnover("h24") is None, "no pool, no ratio"


def test_depth_is_the_pool_as_a_share_of_the_market_cap():
    assert token(pool(liq=50_000.0, mc=1_000_000.0)).depth() == pytest.approx(5.0)
    assert token(pool(liq=50_000.0, mc=None)).depth() is None, "no cap to be a share of"
    assert token(pool(liq=0.0, mc=1_000_000.0)).depth() == 0.0, "an empty pool is a fact, not a gap"


# ---------------- the retrace board ----------------


def test_retrace_ranks_the_deepest_dips_that_still_have_buyers():
    """A board of tokens well off their highs is only useful if somebody is
    still trading them: a 90% drawdown nobody is buying is not a setup."""
    def row(sym, off_pct, buyers):
        p = pool(f"0x{sym}", mc=1_000_000.0, chg={"h1": off_pct}, tx={"h1": {"buyers": buyers, "sellers": 1}})
        return rhchain.TokenActivity(symbol=sym, name=sym, address=f"0x{sym}", pools=[p])

    deep_and_busy = row("DEEP", -60.0, 100)
    deep_and_dead = row("DEAD", -60.0, 0)
    shallow = row("SHAL", -25.0, 100)
    untouched = row("FLAT", 5.0, 100)
    ranked = [t.symbol for t in rhchain.rank([shallow, untouched, deep_and_dead, deep_and_busy], "h1", "retrace")]
    assert ranked == ["DEEP", "SHAL", "DEAD"], "a token at its high is not a retrace at all"
    assert "retrace" in rhchain.SORTS


def test_retrace_needs_a_real_drawdown_not_a_wobble():
    small = rhchain.TokenActivity(symbol="S", name="S", address="0xs",
                                  pools=[pool(mc=1_000_000.0, chg={"h1": -(rhchain.RETRACE_MIN_PCT - 1)})])
    assert rhchain.rank([small], "h1", "retrace") == []


# ---------------- the real highs, from hourly candles ----------------


@pytest.fixture(autouse=True)
def clear_highs():
    rhchain.clear_highs_cache()
    yield
    rhchain.clear_highs_cache()


def candles(*closes, start=NOW - 34 * 3600):
    """GeckoTerminal's [ts, open, high, low, close, volume], newest last."""
    return [[start + i * 3600, c, c * 1.1, c * 0.9, c, 1_000.0] for i, c in enumerate(closes)]


@pytest.mark.asyncio
async def test_highs_come_from_the_candles_and_carry_the_series_for_a_chart(monkeypatch):
    async def top_pool(ca, network="robinhood", **kw):
        return {"attributes": {"address": "0xpool"}}

    async def ohlcv(addr, tf, agg, limit, network="robinhood", **kw):
        return candles(1.0, 2.0, 5.0, 3.0, 2.0, 2.5)          # peak of 5.0 (high 5.5) mid-series

    monkeypatch.setattr(gecko, "top_pool", top_pool)
    monkeypatch.setattr(gecko, "ohlcv", ohlcv)
    h = await rhchain.price_highs("0xtok", now=NOW)
    assert h.price_now == pytest.approx(2.5), "the newest close"
    assert h.high_7d == pytest.approx(5.5) and h.hours == 6
    assert h.off_7d == pytest.approx((2.5 / 5.5 - 1) * 100)
    assert len(h.series) == 6 and h.series[0][0] < h.series[-1][0], "oldest first, for drawing left to right"
    assert h.series[-1][1] == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_a_past_price_converts_to_a_market_cap_against_the_series_own_close():
    h = rhchain.Highs(price_now=2.0, high_7d=4.0)
    assert h.mc_of(4.0, 1_000_000.0) == pytest.approx(2_000_000.0)
    assert h.mc_of(None, 1_000_000.0) is None and h.mc_of(4.0, None) is None
    assert rhchain.Highs(price_now=0.0).mc_of(1.0, 1.0) is None


@pytest.mark.asyncio
async def test_highs_are_cached_so_a_second_click_costs_nothing(monkeypatch):
    calls = []

    async def top_pool(ca, network="robinhood", **kw):
        calls.append(ca)
        return {"attributes": {"address": "0xpool"}}

    async def ohlcv(addr, tf, agg, limit, network="robinhood", **kw):
        return candles(*[1.0] * 8)

    monkeypatch.setattr(gecko, "top_pool", top_pool)
    monkeypatch.setattr(gecko, "ohlcv", ohlcv)
    assert await rhchain.price_highs("0xtok", now=NOW) is not None
    assert await rhchain.price_highs("0xtok", now=NOW + 60) is not None
    assert len(calls) == 1, "inside the cache window"
    assert await rhchain.price_highs("0xtok", now=NOW + rhchain.HIGHS_CACHE_SECONDS + 1) is not None
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_token_geckoterminal_cannot_answer_for_has_no_highs_and_is_not_cached(monkeypatch):
    async def no_pool(ca, network="robinhood", **kw):
        return None

    async def boom(ca, network="robinhood", **kw):
        raise RuntimeError("GeckoTerminal is down")

    monkeypatch.setattr(gecko, "top_pool", no_pool)
    assert await rhchain.price_highs("0xtok", now=NOW) is None
    monkeypatch.setattr(gecko, "top_pool", boom)
    assert await rhchain.price_highs("0xtok", now=NOW) is None, "never raises into a card"

    async def top_pool(ca, network="robinhood", **kw):
        return {"attributes": {"address": "0xpool"}}

    async def junk(addr, tf, agg, limit, network="robinhood", **kw):
        return [["not", "a", "candle"], None]

    monkeypatch.setattr(gecko, "top_pool", top_pool)
    monkeypatch.setattr(gecko, "ohlcv", junk)
    assert await rhchain.price_highs("0xtok", now=NOW) is None


@pytest.mark.asyncio
async def test_only_a_read_someone_is_waiting_on_asks_twice(monkeypatch):
    """A card was opened for this figure, so it is worth a second attempt. A
    feed post has another post coming, and a retry in the middle of its burst
    is what pushes the whole minute over the provider's line."""
    seen = {}

    async def top_pool(ca, network="robinhood", *, retry_429=0):
        seen["pool"] = retry_429
        return {"attributes": {"address": "0xpool"}}

    async def ohlcv(addr, tf, agg, limit, network="robinhood", *, retry_429=0):
        seen["ohlcv"] = retry_429
        return candles(*[1.0] * 8)

    monkeypatch.setattr(gecko, "top_pool", top_pool)
    monkeypatch.setattr(gecko, "ohlcv", ohlcv)
    await rhchain.price_highs("0xtok", now=NOW)
    assert seen == {"pool": 0, "ohlcv": 0}, "the feed's read takes what it gets"
    rhchain.clear_highs_cache()
    await rhchain.price_highs("0xtok", now=NOW, patient=True)
    assert seen == {"pool": 1, "ohlcv": 1}


def test_a_chart_down_ninety_nine_percent_is_a_rug_not_a_retrace():
    """The board's whole thesis is that something which ran once can run again.
    A token that went to zero still carries a day's worth of buyers, so without
    an upper bound it wins the ranking every time."""
    def row(sym, off_pct, buyers):
        p = pool(f"0x{sym}", mc=1_000_000.0, chg={"h1": off_pct}, tx={"h1": {"buyers": buyers, "sellers": 1}})
        return rhchain.TokenActivity(symbol=sym, name=sym, address=f"0x{sym}", pools=[p])

    dead = row("RUG", -99.9, 3_000)
    alive = row("DIP", -60.0, 30)
    assert [t.symbol for t in rhchain.rank([dead, alive], "h1", "retrace")] == ["DIP"]
    edge = row("EDGE", -rhchain.RETRACE_MAX_PCT, 1)
    assert [t.symbol for t in rhchain.rank([edge], "h1", "retrace")] == ["EDGE"], "the bound itself still counts"
