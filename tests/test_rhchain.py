"""/rh_trending: DEX activity on the Robinhood chain, via GeckoTerminal."""

import asyncio

import pytest

from mccapbot import rhchain


def gt_pool(addr, base, quote="WETH", dex="uniswap-v3-robinhood", vol24=1_000.0, vol1=10.0,
            liq=100.0, mc=None, fdv=None, chg24=1.0, chg1=0.1, created="2026-09-07T01:03:52Z"):
    """One pool in GeckoTerminal's JSON:API shape."""
    return {
        "id": f"robinhood_{addr}",
        "type": "pool",
        "attributes": {
            "address": addr,
            "name": f"{base} / {quote}",
            "base_token_price_usd": "0.5",
            "reserve_in_usd": str(liq),
            "market_cap_usd": None if mc is None else str(mc),
            "fdv_usd": None if fdv is None else str(fdv),
            "volume_usd": {"m5": "0", "h1": str(vol1), "h6": str(vol1 * 3), "h24": str(vol24)},
            "price_change_percentage": {"h1": str(chg1), "h6": "0", "h24": None if chg24 is None else str(chg24)},
            "transactions": {"m5": {"buys": 2, "sells": 1, "buyers": 2, "sellers": 1},
                             "h1": {"buys": 4, "sells": 2, "buyers": 3, "sellers": 2},
                             "h24": {"buys": 5, "sells": 3, "buyers": 4, "sellers": 3}},
            "pool_created_at": created,
        },
        "relationships": {
            "base_token": {"data": {"id": f"robinhood_0x{base.lower()}", "type": "token"}},
            "quote_token": {"data": {"id": f"robinhood_0x{quote.lower()}", "type": "token"}},
            "dex": {"data": {"id": dex, "type": "dex"}},
        },
    }


def included(*syms, dexes=("uniswap-v3-robinhood",)):
    toks = [
        {"id": f"robinhood_0x{s.lower()}", "type": "token",
         "attributes": {"address": f"0x{s.lower()}", "name": f"{s} Token", "symbol": s}}
        for s in syms
    ]
    dx = [
        {"id": d, "type": "dex", "attributes": {"name": d.replace("-robinhood", "").replace("-", " ").title() + " (Robinhood)"}}
        for d in dexes
    ]
    return toks + dx


PAYLOAD = {
    "data": [
        gt_pool("0xa1", "USDG", vol24=550_000_000, liq=31_000_000, mc=3.2e9),
        gt_pool("0xb1", "PONS", vol24=39_000_000, liq=4_000_000, mc=509e6, chg24=-13.3),
        gt_pool("0xb2", "PONS", quote="USDG", dex="ramses-v3-robinhood", vol24=17_000_000, liq=700_000, mc=509e6, chg24=-9.0),
        gt_pool("0xc1", "Nasduck", vol24=63_000_000, liq=533_000, fdv=10.1e6, chg24=362_123.4, created="2026-09-07T01:03:52Z"),
        gt_pool("0xd1", "NVDA", quote="USDG", vol24=20_000_000, liq=6_800_000, mc=15.2e6, chg24=0.2, created="2026-07-21T11:02:06Z"),
        gt_pool("0xe1", "NOCHG", vol24=5_000, liq=50, mc=1e5, chg24=None),
    ],
    "included": included("USDG", "PONS", "Nasduck", "NVDA", "NOCHG", "WETH",
                         dexes=("uniswap-v3-robinhood", "ramses-v3-robinhood")),
}


@pytest.fixture(autouse=True)
def clear_cache():
    rhchain._cache.clear()
    rhchain._cached_at = 0.0
    yield
    rhchain._cache.clear()
    rhchain._cached_at = 0.0


# ---------------- parsing ----------------


def test_parse_resolves_tokens_and_dex_from_included():
    pools = rhchain.parse_pools(PAYLOAD)
    assert len(pools) == 6
    pons = next(p for p in pools if p.address == "0xb2")
    assert pons.base_symbol == "PONS" and pons.quote_symbol == "USDG"
    assert pons.dex == "Ramses V3"                    # "(Robinhood)" stripped
    assert pons.volume["h24"] == 17_000_000 and pons.volume["h1"] == 10.0
    assert pons.change["h24"] == pytest.approx(-9.0)
    assert pons.buys_h24 == 5 and pons.sells_h24 == 3
    assert pons.created_ts > 0


def test_parse_falls_back_to_fdv_when_market_cap_is_missing():
    pools = rhchain.parse_pools(PAYLOAD)
    duck = next(p for p in pools if p.base_symbol == "Nasduck")
    assert duck.mc_usd == pytest.approx(10.1e6)
    nochg = next(p for p in pools if p.base_symbol == "NOCHG")
    assert nochg.change["h24"] is None, "a missing change must not read as 0%"


def test_parse_tolerates_junk():
    assert rhchain.parse_pools(None) == []
    assert rhchain.parse_pools({"errors": [{"status": "429"}]}) == []
    assert rhchain.parse_pools({"data": ["nope", {"attributes": {}}]}) == []


# ---------------- aggregation ----------------


def test_aggregate_groups_pools_by_base_token():
    tokens = {t.symbol: t for t in rhchain.aggregate(rhchain.parse_pools(PAYLOAD))}
    pons = tokens["PONS"]
    assert len(pons.pools) == 2
    assert pons.volume("h24") == 56_000_000
    assert pons.liq_usd == 4_700_000
    assert pons.change("h24") == pytest.approx(-13.3), "change comes from the deepest pool"
    assert pons.venues == ["Ramses V3", "Uniswap V3"]
    assert pons.buys_h24 == 10


# ---------------- ranking ----------------


def test_default_board_is_by_volume_with_majors_hidden():
    tokens = rhchain.aggregate(rhchain.parse_pools(PAYLOAD))
    top = rhchain.rank(tokens, "h24", "volume")
    assert [t.symbol for t in top] == ["Nasduck", "PONS", "NVDA", "NOCHG"]
    with_majors = rhchain.rank(tokens, "h24", "volume", include_majors=True)
    assert with_majors[0].symbol == "USDG"


def test_gainers_and_losers_skip_tokens_without_a_change():
    tokens = rhchain.aggregate(rhchain.parse_pools(PAYLOAD))
    gainers = [t.symbol for t in rhchain.rank(tokens, "h24", "gainers")]
    assert gainers[0] == "Nasduck" and "NOCHG" not in gainers
    losers = [t.symbol for t in rhchain.rank(tokens, "h24", "losers")]
    assert losers[0] == "PONS"


def test_new_sorts_by_pool_creation():
    tokens = rhchain.aggregate(rhchain.parse_pools(PAYLOAD))
    newest = rhchain.rank(tokens, "h24", "new", n=2)
    assert "NVDA" not in [t.symbol for t in newest]


def test_count_is_respected():
    tokens = rhchain.aggregate(rhchain.parse_pools(PAYLOAD))
    assert len(rhchain.rank(tokens, "h24", "volume", n=2)) == 2


# ---------------- fetching ----------------


def test_top_pools_walks_pages_and_caches(monkeypatch):
    calls = []

    async def fake(url, **kw):
        calls.append(url)
        page = int(url.split("page=")[1].split("&")[0])
        if page == 1:
            return PAYLOAD
        return {"data": [gt_pool("0xf1", "LATE", vol24=100)], "included": included("LATE", "WETH")}

    monkeypatch.setattr(rhchain, "get_json", fake)
    monkeypatch.setattr(rhchain, "RHCHAIN_PAGES", 2)
    pools = asyncio.run(rhchain.top_pools())
    assert len(pools) == 7
    assert len(calls) == 2 and "networks/robinhood/pools" in calls[0]
    assert "sort=h24_volume_usd_desc" in calls[0]
    asyncio.run(rhchain.top_pools())
    assert len(calls) == 2, "second call should be served from cache"


def test_a_failed_refresh_keeps_the_previous_board(monkeypatch):
    async def good(url, **kw):
        return PAYLOAD

    monkeypatch.setattr(rhchain, "get_json", good)
    monkeypatch.setattr(rhchain, "RHCHAIN_PAGES", 1)
    assert asyncio.run(rhchain.top_pools())

    async def bad(url, **kw):
        return None  # 429 / timeout flattened by get_json

    monkeypatch.setattr(rhchain, "get_json", bad)
    assert len(asyncio.run(rhchain.top_pools(force=True))) == 6


def test_short_windows_and_implied_market_cap_before():
    p = gt_pool("0xm1", "MOVER", vol24=1_000_000, liq=50_000, mc=1_100_000, chg24=10.0)
    p["attributes"]["volume_usd"]["m15"] = "250000"
    p["attributes"]["price_change_percentage"]["m15"] = "37.5"       # $800K -> $1.1M inside 15 minutes
    p["attributes"]["price_change_percentage"]["m5"] = "5"
    pools = rhchain.parse_pools({"data": [p], "included": included("MOVER", "WETH")})
    tok = rhchain.aggregate(pools)[0]
    assert tok.volume("m15") == 250_000 and tok.change("m15") == pytest.approx(37.5)
    assert tok.mc_before("m15") == pytest.approx(800_000)
    assert tok.mc_before("m30") is None, "no change figure, no implied figure"
    assert rhchain.WINDOW_LABELS["m15"] == "15m"
    assert [t.symbol for t in rhchain.rank([tok], "m15", "gainers")] == ["MOVER"]


def test_new_pools_feed_is_newest_first_and_cached(monkeypatch):
    calls = []

    async def fake(url, **kw):
        calls.append(url)
        return {"data": [
            gt_pool("0xn1", "OLDER", vol24=10, liq=2_000, created="2026-09-08T13:00:00Z"),
            gt_pool("0xn2", "NEWER", vol24=10, liq=3_000, created="2026-09-08T13:55:00Z"),
        ], "included": included("OLDER", "NEWER", "WETH")}
    monkeypatch.setattr(rhchain, "get_json", fake)
    rhchain._new_cache.clear()
    rhchain._new_cached_at = 0.0
    pools = asyncio.run(rhchain.new_pools())
    assert "networks/robinhood/new_pools" in calls[0]
    assert [p.base_symbol for p in pools] == ["NEWER", "OLDER"]
    asyncio.run(rhchain.new_pools())
    assert len(calls) == 1
    rhchain._new_cache.clear()


def test_age_str():
    now = 1_700_000_000.0
    assert rhchain.age_str(now - 120, now) == "2m"
    assert rhchain.age_str(now - 7_200, now) == "2h"
    assert rhchain.age_str(now - 3 * 86_400, now) == "3d"
    assert rhchain.age_str(0, now) == "?"


def test_an_empty_first_page_stops_paging(monkeypatch):
    calls = []

    async def empty(url, **kw):
        calls.append(url)
        return {"data": []}

    monkeypatch.setattr(rhchain, "get_json", empty)
    monkeypatch.setattr(rhchain, "RHCHAIN_PAGES", 3)
    assert asyncio.run(rhchain.top_pools()) == []
    assert len(calls) == 1


# ---------------- per-window trades and unique wallets ----------------


def test_parse_keeps_per_window_trade_counts_and_unique_wallets():
    pools = rhchain.parse_pools(PAYLOAD)
    p = pools[0]
    assert p.count("m5", "buyers") == 2 and p.count("h1", "sells") == 2 and p.count("h24", "sellers") == 3
    assert p.count("m30", "buys") == 0, "a window the payload did not carry reads as zero, never as an error"
    assert p.buys_h24 == 5 and p.sells_h24 == 3, "the old fields still say what they said"


def test_token_activity_sums_windows_and_measures_volume_pace():
    tokens = {t.symbol: t for t in rhchain.aggregate(rhchain.parse_pools(PAYLOAD))}
    pons = tokens["PONS"]
    assert pons.buyers("m5") == 4 and pons.sells("h1") == 4 and pons.buys("h24") == pons.buys_h24
    # The fixture has no 5m volume, so the pace over the hour is zero, and a
    # token with no hourly volume has no pace at all rather than a division error.
    assert pons.volume_pace("m5", "h1") == 0.0
    quiet = rhchain.TokenActivity(symbol="Q", name="Q", address="0xq")
    assert quiet.volume_pace() is None
    pons.pools[0].volume["m5"] = pons.volume("h1") / 12 * 3     # five minutes at three times the hour's pace
    assert pons.volume_pace("m5", "h1") == pytest.approx(3.0)

