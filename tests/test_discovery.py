"""The discovery feed: evaluation rules, the second look, the confirm read,
the hourly cap, persistence before send, and the shared tracker that grades
its posts. Nothing here touches the network: the GeckoTerminal fetchers,
DexScreener's refresh and the multi-token lookup are all stand-ins."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from mccapbot import alerts, discovery, gecko, rhchain, storage, tracker
from mccapbot.cache import token_cache
from mccapbot.discovery import Candidate, FeedConfig, evaluate, rank
from mccapbot.models import ScanEvent, TokenSnapshot
from rhc_fakes import Chan, FakeBot

NOW = 1_800_000_000.0
# The real fetchers, captured before any test swaps them for stand-ins.
REAL_FETCHERS = {name: getattr(rhchain, name) for name in ("new_pools", "trending_pools", "top_pools")}
CA1 = "0x" + "11" * 20
CA2 = "0x" + "22" * 20
CA3 = "0x" + "33" * 20
CA4 = "0x" + "44" * 20


def pool(addr, sym, ca, *, quote="WETH", liq=30_000.0, mc=400_000.0, vol_m5=0.0, vol_h1=0.0, chg_m5=None,
         buys=0, sells=0, buyers=0, created=NOW - 120, dex="Uniswap V3", decimals=18, image=""):
    return rhchain.Pool(
        address=addr, name=f"{sym} / {quote}", dex=dex, base_symbol=sym, base_name=sym.title(), base_address=ca,
        quote_symbol=quote, price_usd=1.0, liq_usd=liq, mc_usd=mc,
        volume={"m5": vol_m5, "h1": vol_h1, "h24": vol_h1 * 4}, change={"m5": chg_m5},
        buys_h24=buys, sells_h24=sells, created_ts=created,
        tx={"m5": {"buys": buys, "sells": sells, "buyers": buyers, "sellers": max(0, sells - 1)}},
        base_decimals=decimals, base_image_url=image,
    )


def new_pair_pool(**kw):
    base = dict(liq=8_000.0, buys=20, sells=3, buyers=12, created=NOW - 300)
    base.update(kw)
    return pool("0xp1", "PONS", CA1, **base)


def spike_pool(**kw):
    base = dict(liq=50_000.0, vol_m5=5_000.0, vol_h1=12_000.0, buyers=15, buys=30, sells=10, created=NOW - 86_400)
    base.update(kw)
    return pool("0xs1", "SPK", CA2, **base)                                  # pace = 5000*12/12000 = 5x


def mover_pool(**kw):
    base = dict(quote="USDG", liq=50_000.0, chg_m5=40.0, buyers=10, buys=12, sells=4, created=NOW - 86_400)
    base.update(kw)
    return pool("0xm1", "MOV", CA3, **base)


def tokens(*pools):
    return rhchain.aggregate(pools)


def cfg(**kw):
    base = dict(guild_id=1, channel_id=99)
    base.update(kw)
    return FeedConfig(**base)


def candidate(kind, ca, symbol, *, buyers=10, pace=1.0, age=3600.0, liq=50_000.0):
    """A Candidate straight from its fields, for the selector's own tests: what
    it weighs is ``score()`` = buyers x pace, and nothing else here matters."""
    return discovery.Candidate(
        kind=kind, ca=ca, symbol=symbol, name=symbol.title(), pool=f"pool-{symbol}", decimals=18,
        mc=400_000.0, mc_before=None, liq=liq, age_sec=age, buyers_m5=buyers, buys_m5=buyers, sells_m5=1,
        vol_m5=1_000.0, vol_h1=2_000.0, pace=pace, change_m5=None, venue="Uniswap V3", quote="WETH", image_url="",
    )


def kinds(cands):
    return [(c.kind, c.symbol) for c in cands]


# ---------------- evaluate: each kind, each rejection ----------------


def test_new_pair_qualifies_and_every_rejection_reason():
    c = cfg()
    assert kinds(evaluate(tokens(new_pair_pool()), c, {}, NOW)) == [("new_pair", "PONS")]
    cand = evaluate(tokens(new_pair_pool()), c, {}, NOW)[0]
    assert cand.pool == "0xp1" and cand.decimals == 18 and cand.age_sec == pytest.approx(300)
    assert cand.buyers_m5 == 12 and cand.buys_m5 == 20 and cand.sells_m5 == 3 and cand.venue == "Uniswap V3"
    assert evaluate(tokens(new_pair_pool(buyers=7)), c, {}, NOW) == [], "buyers"
    assert evaluate(tokens(new_pair_pool(liq=4_999)), c, {}, NOW) == [], "liquidity"
    assert evaluate(tokens(new_pair_pool(buys=10, sells=6)), c, {}, NOW) == [], "buys must be 2x sells"
    assert evaluate(tokens(new_pair_pool(created=NOW - 901)), c, {}, NOW) == [], "age"
    assert evaluate(tokens(new_pair_pool()), c, {"new:0xp1": NOW - 3600}, NOW) == [], "seen"
    assert evaluate(tokens(new_pair_pool()), c, {}, NOW, pending_cas=[CA1]) == [], "already pending"
    assert evaluate(tokens(new_pair_pool()), cfg(muted={CA1: NOW + 60}), {}, NOW) == [], "muted"
    assert kinds(evaluate(tokens(new_pair_pool()), cfg(muted={CA1: NOW - 1}), {}, NOW)) == [("new_pair", "PONS")], "mute expired"
    assert evaluate(tokens(new_pair_pool()), cfg(new_pairs=False), {}, NOW) == [], "kind off"
    assert evaluate(tokens(pool("0xw1", "WETH", "0xweth", liq=1e6, buys=50, sells=1, buyers=40)), c, {}, NOW) == [], "majors"
    assert evaluate(tokens(new_pair_pool(created=0)), c, {}, NOW) == [], "unknown creation time is not new"


def test_spike_qualifies_and_every_rejection_reason():
    c = cfg()
    cands = evaluate(tokens(spike_pool()), c, {}, NOW)
    assert kinds(cands) == [("spike", "SPK")]
    assert cands[0].pace == pytest.approx(5.0) and cands[0].vol_h1 == 12_000 and cands[0].score() == pytest.approx(75.0)
    assert evaluate(tokens(spike_pool(vol_m5=2_500.0)), c, {}, NOW) == [], "pace 2.5x under 3x"
    assert evaluate(tokens(spike_pool(vol_m5=1_500.0, vol_h1=1_000.0)), c, {}, NOW) == [], "$2K 5m floor"
    assert evaluate(tokens(spike_pool(liq=19_000.0)), cfg(min_liq=1_000), {}, NOW) == [], "$20K liquidity floor"
    assert evaluate(tokens(spike_pool(buyers=7)), c, {}, NOW) == [], "buyers"
    assert evaluate(tokens(spike_pool()), c, {f"spike:{CA2}": NOW - 3600}, NOW) == [], "6h cooldown"
    assert kinds(evaluate(tokens(spike_pool()), c, {f"spike:{CA2}": NOW - 21_601}, NOW)) == [("spike", "SPK")]
    assert evaluate(tokens(spike_pool()), c, {"new:0xs1": NOW - 300}, NOW) == [], "no spike inside 10 min of a new-pair post"
    assert evaluate(tokens(spike_pool()), cfg(spikes=False), {}, NOW) == [], "kind off"
    assert evaluate(tokens(spike_pool(vol_h1=0.0)), c, {}, NOW) == [], "no hourly volume, no pace"


def test_mover_qualifies_and_every_rejection_reason():
    c = cfg()
    cands = evaluate(tokens(mover_pool()), c, {}, NOW)
    assert kinds(cands) == [("mover", "MOV")]
    assert cands[0].change_m5 == 40.0 and cands[0].mc_before == pytest.approx(400_000 / 1.4) and cands[0].quote == "USDG"
    assert evaluate(tokens(mover_pool(chg_m5=24.9)), c, {}, NOW) == [], "move under threshold"
    assert evaluate(tokens(mover_pool(chg_m5=None)), c, {}, NOW) == [], "no change figure"
    assert evaluate(tokens(mover_pool(quote="NVDA")), c, {}, NOW) == [], "stock-quoted pool never prints a move"
    assert evaluate(tokens(mover_pool(liq=15_000.0)), c, {}, NOW) == [], "$20K liquidity floor"
    assert evaluate(tokens(mover_pool(buyers=3)), c, {}, NOW) == [], "buyers"
    assert evaluate(tokens(mover_pool()), c, {f"move:{CA3}": NOW - 100}, NOW) == [], "6h cooldown"
    assert evaluate(tokens(mover_pool()), cfg(movers=False), {}, NOW) == [], "kind off"


def test_one_candidate_per_token_and_thresholds_are_per_server():
    both = pool("0xb1", "BOTH", CA4, liq=50_000.0, vol_m5=5_000.0, vol_h1=12_000.0, chg_m5=60.0, quote="USDG",
                buyers=20, buys=40, sells=5, created=NOW - 100)
    cands = evaluate(tokens(both), cfg(), {}, NOW)
    assert kinds(cands) == [("new_pair", "BOTH")], "a new pair outranks its own spike and move"
    assert kinds(evaluate(tokens(both), cfg(new_pairs=False), {}, NOW)) == [("spike", "BOTH")]
    assert kinds(evaluate(tokens(both), cfg(new_pairs=False, spikes=False), {}, NOW)) == [("mover", "BOTH")]
    strict = cfg(min_buyers=30, min_liq=60_000)
    assert evaluate(tokens(new_pair_pool(), spike_pool(), mover_pool()), strict, {}, NOW) == []


def test_rank_is_by_score_then_youngest():
    def cand(sym, buyers, pace, age):
        return Candidate(kind="spike", ca=sym, symbol=sym, name=sym, pool=sym, decimals=18, mc=1.0, mc_before=None,
                         liq=1.0, age_sec=age, buyers_m5=buyers, buys_m5=0, sells_m5=0, vol_m5=0.0, vol_h1=0.0,
                         pace=pace, change_m5=None, venue="", quote="", image_url="")
    a, b, c, d = cand("A", 10, 3.0, 500), cand("B", 20, None, 100), cand("C", 20, 1.0, 50), cand("D", 5, 0.5, 1)
    assert [x.symbol for x in rank([a, b, c, d], 10)] == ["A", "C", "B", "D"]
    assert [x.symbol for x in rank([a, b, c, d], 2)] == ["A", "C"]
    assert rank([a, b], 0) == [] and rank([a], -1) == []


# ---------------- the poller ----------------


def snapshot(ca, mc=420_000.0, liq=30_000.0, chain="robinhood", image=""):
    return TokenSnapshot(mc=mc, url=f"https://dexscreener.com/robinhood/{ca}", updated_ts=NOW, chain=chain,
                         liq_usd=liq, image_url=image, change_m5=5.0, buys_m5=10, sells_m5=2)


class Market:
    """The three GeckoTerminal lists, the multi-token lookup and DexScreener's refresh, all fakes."""

    def __init__(self):
        self.new = []
        self.trending = []
        self.top = []
        self.snaps = {}              # ca -> TokenSnapshot | None (no pairs yet) | "fail" (request failed)
        self.reserves = {}           # ca -> total_reserve_in_usd for the second look
        self.multi_fail = False
        self.refreshed = []
        self.multi_calls = []

    def install(self, monkeypatch):
        m = self

        async def new_pools(force=False, pages=None):
            return list(m.new)

        async def trending_pools(duration="5m", force=False):
            return list(m.trending)

        async def top_pools(force=False):
            return list(m.top)

        async def refresh(ca):
            m.refreshed.append(ca)
            snap = m.snaps.get(ca)
            if snap == "fail":
                return False
            token_cache[ca] = snap if snap is not None else TokenSnapshot(mc=None, url="", updated_ts=NOW)
            return True

        async def multi(addrs, network="robinhood"):
            m.multi_calls.append(list(addrs))
            if m.multi_fail:
                return None
            return {a: {"address": a, "total_reserve_in_usd": str(m.reserves[a])} for a in addrs if a in m.reserves}

        monkeypatch.setattr(rhchain, "new_pools", new_pools)
        monkeypatch.setattr(rhchain, "trending_pools", trending_pools)
        monkeypatch.setattr(rhchain, "top_pools", top_pools)
        monkeypatch.setattr(alerts, "_refresh", refresh)
        monkeypatch.setattr(gecko, "tokens_multi", multi)


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Clean feed state, a full GeckoTerminal bucket, redirected files, a Market and a bot with one channel."""
    monkeypatch.setattr(storage, "FEED_FILE", str(tmp_path / "feed.json"))
    monkeypatch.setattr(storage, "SCANS_FILE", str(tmp_path / "scans.json"))
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(discovery, "FEED_ENABLE", True)
    monkeypatch.setattr(discovery, "FETCH_SPACING", 0.0)   # the real one paces GeckoTerminal, not the suite
    discovery.configs.clear()
    discovery.seen.clear()
    discovery.pending.clear()
    discovery.posted.clear()
    storage.scan_events.clear()
    token_cache.clear()
    rhchain.clear_caches()
    gecko.gecko_limiter.tokens = float(gecko.gecko_limiter.capacity)
    gecko.gecko_limiter.updated = time.monotonic()

    async def no_risk(ca):
        return "Risk: —"
    monkeypatch.setattr(discovery, "_risk_line", no_risk)

    market = Market()
    market.install(monkeypatch)
    chan = Chan(channel_id=99)
    bot = FakeBot(None, chan)
    feed = discovery.Feed(bot)
    discovery.configs[1] = cfg()
    yield SimpleNamespace(market=market, chan=chan, bot=bot, feed=feed, dir=tmp_path)
    discovery.configs.clear()
    discovery.seen.clear()
    discovery.pending.clear()
    discovery.posted.clear()
    storage.scan_events.clear()
    token_cache.clear()
    rhchain.clear_caches()


def run(coro):
    return asyncio.run(coro)


def embeds(chan):
    return [kw["embed"] for _, kw in chan.sent]


@pytest.mark.asyncio
async def test_new_pair_is_pending_on_first_sight_then_posts_after_a_passing_second_look(world):
    m, chan, feed = world.market, world.chan, world.feed
    m.new = [new_pair_pool()]
    m.reserves[CA1] = 8_000.0
    m.snaps[CA1] = snapshot(CA1, liq=8_000.0)

    await feed.tick(NOW)
    assert chan.sent == [], "never posted on first sight"
    assert CA1 in discovery.pending and discovery.pending[CA1].first_liq == 8_000.0
    assert m.multi_calls == [], "the second look waits a minute"
    assert (world.dir / "feed.json").exists(), "pending is on disk"

    await feed.tick(NOW + 30)
    assert chan.sent == [] and m.multi_calls == []

    await feed.tick(NOW + 61)
    assert m.multi_calls == [[CA1]]
    assert len(chan.sent) == 1
    e = embeds(chan)[0]
    assert e.title == "🆕 New pair · PONS"
    assert "second look passed" in e.footer.text and "GeckoTerminal" in e.footer.text
    assert CA1 not in discovery.pending and discovery.seen["new:0xp1"] == NOW + 61
    assert discovery.recent_tokens() == {CA1: ("PONS", 18)}

    await feed.tick(NOW + 122)
    assert len(chan.sent) == 1, "seen: no repeat"


@pytest.mark.asyncio
async def test_second_look_drops_a_pair_whose_liquidity_fell(world):
    m, chan, feed = world.market, world.chan, world.feed
    m.new = [new_pair_pool()]
    m.reserves[CA1] = 5_000.0                       # 62% of what we first saw
    m.snaps[CA1] = snapshot(CA1, liq=8_000.0)
    await feed.tick(NOW)
    await feed.tick(NOW + 61)
    assert chan.sent == [] and CA1 not in discovery.pending
    assert discovery.seen["new:0xp1"] == NOW + 61, "marked seen so it is never re-queued"
    assert m.refreshed == [], "no DexScreener read for a pair that failed the second look"
    await feed.tick(NOW + 122)
    assert CA1 not in discovery.pending and chan.sent == []


@pytest.mark.asyncio
async def test_second_look_waits_out_a_geckoterminal_blip_and_an_unindexed_token(world):
    m, chan, feed = world.market, world.chan, world.feed
    m.new = [new_pair_pool()]
    m.snaps[CA1] = snapshot(CA1, liq=8_000.0)
    await feed.tick(NOW)
    m.multi_fail = True
    await feed.tick(NOW + 61)
    assert CA1 in discovery.pending and chan.sent == [], "a failed lookup is not a failed pair"
    m.multi_fail = False                             # answers, but without this token
    await feed.tick(NOW + 122)
    assert CA1 in discovery.pending and chan.sent == []
    m.reserves[CA1] = 8_000.0
    await feed.tick(NOW + 183)
    assert len(chan.sent) == 1


@pytest.mark.asyncio
async def test_dexscreener_lag_keeps_a_new_pair_pending_for_ten_tries_then_drops_it(world):
    m, chan, feed = world.market, world.chan, world.feed
    m.new = [new_pair_pool()]
    m.reserves[CA1] = 8_000.0
    m.snaps[CA1] = None                              # DexScreener has no pairs yet
    await feed.tick(NOW)
    t = NOW
    for i in range(1, 10):
        t += 61
        await feed.tick(t)
        assert CA1 in discovery.pending, f"try {i}"
        assert discovery.pending[CA1].tries == i
        assert chan.sent == []
    t += 61
    await feed.tick(t)
    assert CA1 not in discovery.pending and discovery.seen["new:0xp1"] == t
    assert chan.sent == [] and len(m.refreshed) == 10


@pytest.mark.asyncio
async def test_dexscreener_lag_on_a_new_pair_recovers_when_the_pair_appears(world):
    m, chan, feed = world.market, world.chan, world.feed
    m.new = [new_pair_pool()]
    m.reserves[CA1] = 8_000.0
    m.snaps[CA1] = "fail"
    await feed.tick(NOW)
    await feed.tick(NOW + 61)
    await feed.tick(NOW + 122)
    assert discovery.pending[CA1].tries == 2 and chan.sent == []
    m.snaps[CA1] = snapshot(CA1, liq=8_000.0)
    await feed.tick(NOW + 183)
    assert len(chan.sent) == 1 and CA1 not in discovery.pending


@pytest.mark.asyncio
async def test_spike_with_no_dexscreener_data_is_skipped_without_seen(world):
    m, chan, feed = world.market, world.chan, world.feed
    m.trending = [spike_pool()]
    m.snaps[CA2] = None
    await feed.tick(NOW)
    assert chan.sent == [] and f"spike:{CA2}" not in discovery.seen and m.refreshed == [CA2]
    m.snaps[CA2] = snapshot(CA2, liq=50_000.0)
    await feed.tick(NOW + 60)
    assert len(chan.sent) == 1 and discovery.seen[f"spike:{CA2}"] == NOW + 60
    e = embeds(chan)[0]
    assert e.title == "📈 Volume spike · SPK"
    assert "Pace **5x** the hour's rate · vol 1h $12K" in e.description


@pytest.mark.asyncio
async def test_confirm_requires_the_chain_and_seventy_percent_of_the_liquidity_floor(world):
    m, chan, feed = world.market, world.chan, world.feed
    m.trending = [spike_pool(), mover_pool()]
    m.snaps[CA2] = snapshot(CA2, chain="solana")
    m.snaps[CA3] = snapshot(CA3, liq=3_400.0)        # under 0.7 * $5K
    await feed.tick(NOW)
    assert chan.sent == [] and not discovery.seen
    m.snaps[CA3] = snapshot(CA3, liq=3_600.0)
    await feed.tick(NOW + 60)
    assert [e.title for e in embeds(chan)] == ["🚀 Mover · MOV"]
    e = embeds(chan)[0]
    assert "MC **$400K** (was $286K 5m ago)" in e.description and "5m **+40%**" in e.description
    assert "Liq $50K · age 1d · Uniswap V3 · quote USDG" in e.description
    assert "5m: **10 buyers** · 12 buys / 4 sells · vol $0.00" in e.description
    assert f"`{CA3}`" in e.description and "Risk: —" in e.description
    assert e.footer.text == "GeckoTerminal · most new pairs are dust: check the sell-back line before buying"


@pytest.mark.asyncio
async def test_hourly_cap_posts_the_strongest_and_reports_capped(world):
    m, chan, feed = world.market, world.chan, world.feed
    discovery.configs[1] = cfg(max_per_hour=2)
    weak = spike_pool()                                                            # score 75
    strong = pool("0xs2", "BIG", CA4, liq=50_000.0, vol_m5=8_000.0, vol_h1=12_000.0, buyers=30, buys=50, sells=10,
                  created=NOW - 86_400)                                             # score 240
    mid = mover_pool(buyers=20)                                                    # score 20 (no pace)
    m.trending = [weak, strong, mid]
    for ca in (CA2, CA3, CA4):
        m.snaps[ca] = snapshot(ca, liq=50_000.0)
    await feed.tick(NOW)
    assert [e.title for e in embeds(chan)] == ["📈 Volume spike · BIG", "📈 Volume spike · SPK"]
    st = feed.status(1, NOW)
    assert st["capped"] and "over cap" in st["text"] and st["posts_last_hour"] == 2
    assert st["held"] == ["MOV"] and "waiting on the bar: MOV" in st["text"]
    assert f"move:{CA3}" not in discovery.seen, "the held one may re-qualify next tick"
    await feed.tick(NOW + 60)
    assert len(chan.sent) == 2, "past the cap a weaker find still does not clear the bar"
    await feed.tick(NOW + 3601)
    assert [e.title for e in embeds(chan)][-1] == "🚀 Mover · MOV"
    assert not feed.status(1, NOW + 3601)["capped"]


def test_past_the_cap_only_a_stronger_find_gets_through(world):
    """The cap is a bar, not a shutter: what the first hour posted sets the
    price of admission, and nothing weaker than that buys its way in."""
    c = cfg(max_per_hour=2)
    discovery.posted.extend([
        discovery.PostedToken(ca=CA2, symbol="A", decimals=18, ts=NOW, kind="spike", guild_id=1, score=100.0),
        discovery.PostedToken(ca=CA3, symbol="B", decimals=18, ts=NOW, kind="spike", guild_id=1, score=200.0),
    ])
    weak = candidate("spike", CA4, "WEAK", buyers=10, pace=1.0)          # score 10
    strong = candidate("spike", CA4, "STRONG", buyers=40, pace=10.0)     # score 400

    picked, held = discovery.select([weak], c, NOW)
    assert [x.symbol for x in picked] == [] and [x.symbol for x in held] == ["WEAK"]

    picked, held = discovery.select([strong], c, NOW)
    assert [x.symbol for x in picked] == ["STRONG"], "225 is the bar; 400 clears it"
    assert held == []


def test_an_hour_climbs_past_the_cap_one_stronger_find_at_a_time(world):
    """Within one tick only the best find can clear the bar it sets, so a burst
    of equally good tokens cannot all pile through at once."""
    c = cfg(max_per_hour=2)
    same = [candidate("spike", f"0x{i:040x}", f"T{i}", buyers=100, pace=100.0) for i in range(6)]
    picked, held = discovery.select(same, c, NOW)
    assert len(picked) == 2 and len(held) == 4, "nothing is stronger than itself"

    discovery.posted.extend([
        discovery.PostedToken(ca=CA2, symbol="A", decimals=18, ts=NOW, kind="spike", guild_id=1, score=100.0),
        discovery.PostedToken(ca=CA3, symbol="B", decimals=18, ts=NOW, kind="spike", guild_id=1, score=100.0),
    ])
    # The hour's median is 100, so admission costs 150: a near-miss waits, a
    # find several times better than the hour's typical post goes straight out.
    picked, held = discovery.select([candidate("spike", CA4, "NEARLY", buyers=12, pace=10.0)], c, NOW)
    assert picked == [] and [x.symbol for x in held] == ["NEARLY"], "120 does not clear 150"
    picked, held = discovery.select([candidate("spike", CA4, "UP", buyers=40, pace=10.0)], c, NOW)
    assert [x.symbol for x in picked] == ["UP"] and held == []


def test_no_hour_can_run_past_twice_the_cap(world):
    """However good the tape gets, the ceiling holds: a manager who asked for 2
    an hour never wakes up to twenty."""
    c = cfg(max_per_hour=2)
    discovery.posted.extend([
        discovery.PostedToken(ca=f"0x{i:040x}", symbol=f"P{i}", decimals=18, ts=NOW, kind="spike",
                              guild_id=1, score=score)
        for i, score in enumerate((100.0, 100.0, 400.0, 1_000.0))       # the hour already climbed to the ceiling
    ])
    huge = candidate("spike", CA4, "HUGE", buyers=100, pace=100.0)       # 10,000, and still not posted
    picked, held = discovery.select([huge], c, NOW)
    assert picked == [] and [x.symbol for x in held] == ["HUGE"]
    assert len(discovery.hour_scores(1, NOW)) == 4 == int(c.max_per_hour * discovery.OVERFLOW_MULT)


def test_an_empty_hour_spends_its_whole_budget_before_raising_the_bar(world):
    c = cfg(max_per_hour=3)
    cands = [candidate("spike", f"0x{i:040x}", f"T{i}", buyers=10 - i, pace=1.0) for i in range(3)]
    picked, held = discovery.select(cands, c, NOW)
    assert [x.symbol for x in picked] == ["T0", "T1", "T2"] and held == []


def test_a_cap_of_zero_silences_the_feed_completely(world):
    picked, held = discovery.select([candidate("spike", CA2, "ANY", buyers=99, pace=99.0)], cfg(max_per_hour=0), NOW)
    assert picked == [] and len(held) == 1


@pytest.mark.asyncio
async def test_feed_file_round_trip_reposts_nothing_after_a_restart(world):
    m, chan, feed = world.market, world.chan, world.feed
    m.trending = [spike_pool()]
    m.new = [new_pair_pool()]
    m.snaps[CA2] = snapshot(CA2, liq=50_000.0)
    discovery.configs[1].muted[CA4] = NOW + 3600
    await feed.tick(NOW)
    assert len(chan.sent) == 1 and CA1 in discovery.pending

    discovery.configs.clear()
    discovery.seen.clear()
    discovery.pending.clear()
    discovery.posted.clear()
    await discovery.load_feed()
    assert discovery.configs[1].muted == {CA4: NOW + 3600} and discovery.configs[1].max_per_hour == 10
    assert discovery.seen == {f"spike:{CA2}": NOW}
    assert discovery.pending[CA1].first_liq == 8_000.0 and discovery.pending[CA1].candidate().symbol == "PONS"
    assert discovery.recent_tokens() == {CA2: ("SPK", 18)}

    restarted = discovery.Feed(world.bot)
    await restarted.tick(NOW + 30)
    assert len(chan.sent) == 1, "the spike is in the seen-set; the pair is still pending"
    assert restarted.status(1, NOW + 30)["posts_last_hour"] == 1


@pytest.mark.asyncio
async def test_a_failed_geckoterminal_fetch_posts_nothing_and_status_is_stale(world, monkeypatch):
    feed, chan = world.feed, world.chan

    async def down(url, **kw):
        return None
    for name, fn in REAL_FETCHERS.items():
        monkeypatch.setattr(rhchain, name, fn)      # back to the real fetchers
    monkeypatch.setattr(rhchain, "get_json", down)
    rhchain.clear_caches()
    real_now = time.time()                          # the fetchers stamp the real clock
    await feed.tick(real_now)
    assert chan.sent == [] and rhchain.last_error
    # Age it from the stamps the tick actually wrote, not from before it ran:
    # the tick takes a fraction of a second, which would round the age down.
    st = feed.status(1, max(rhchain.last_error_ts, feed.last_fetch_ts) + 5)
    assert st["stale"] and "stale: last refresh failed 5s ago" in st["text"]
    assert "last fetch 5s ago" in st["text"]
    rhchain.clear_caches()


@pytest.mark.asyncio
async def test_feed_yields_when_the_geckoterminal_bucket_is_under_half(world):
    m, chan, feed = world.market, world.chan, world.feed
    m.trending = [spike_pool()]
    m.snaps[CA2] = snapshot(CA2, liq=50_000.0)
    lim = gecko.gecko_limiter
    lim.tokens = 0.5 * lim.capacity - 0.5
    lim.updated = time.monotonic()
    await feed.tick(NOW)
    assert chan.sent == [] and m.refreshed == [] and feed.paused
    assert "paused (GeckoTerminal busy)" in feed.status(1, NOW)["text"]
    lim.tokens = float(lim.capacity)
    await feed.tick(NOW + 60)
    assert len(chan.sent) == 1 and not feed.paused


@pytest.mark.asyncio
async def test_post_carries_the_alert_row_only_for_a_robinhood_snapshot(world, monkeypatch):
    m, chan, feed = world.market, world.chan, world.feed
    m.trending = [spike_pool()]
    m.snaps[CA2] = snapshot(CA2, liq=50_000.0)
    await feed.tick(NOW)
    view = chan.sent[0][1].get("view")
    assert view is not None and len(view.children) >= 4
    assert discovery.trade_view(CA2, snapshot(CA2, chain="solana")) is None
    assert discovery.trade_view("So11111111111111111111111111111111111111112", snapshot(CA2)) is None
    assert discovery.trade_view(CA2, None) is None

    from mccapbot import views
    monkeypatch.delattr(views, "alert_row")
    assert discovery.trade_view(CA2, snapshot(CA2)) is None, "no row builder yet: post without buttons"


@pytest.mark.asyncio
async def test_post_records_a_feed_scan_event_and_notifies_subscribers(world):
    m, feed = world.market, world.feed
    m.trending = [spike_pool()]
    m.snaps[CA2] = snapshot(CA2, liq=50_000.0, mc=123_000.0)
    got = []

    async def sub(cand, snap):
        got.append((cand, snap))
    feed.on_post.append(sub)
    await feed.tick(NOW)
    assert len(storage.scan_events) == 1
    ev = storage.scan_events[0]
    assert ev.source == "feed" and ev.kind == "spike" and ev.ca == CA2 and ev.symbol == "SPK"
    assert ev.mc_at_scan == 123_000.0 and ev.peak_mc == 123_000.0 and ev.message_id == 1 and ev.scanner_id == 0
    assert ev.guild_id == 1 and ev.channel_id == 99 and ev.ts == NOW
    assert (world.dir / "scans.json").exists()
    assert len(got) == 1 and got[0][0].guild_id == 1 and got[0][0].channel_id == 99 and got[0][1].mc == 123_000.0


@pytest.mark.asyncio
async def test_seen_and_posted_are_saved_before_the_send(world, monkeypatch):
    m, chan, feed = world.market, world.chan, world.feed
    m.trending = [spike_pool()]
    m.snaps[CA2] = snapshot(CA2, liq=50_000.0)
    order = []
    real_save = discovery.save_feed

    async def save():
        order.append(("save", dict(discovery.seen), len(discovery.posted)))
        await real_save()
    monkeypatch.setattr(discovery, "save_feed", save)
    real_send = chan.send

    async def send(content=None, **kw):
        order.append(("send",))
        return await real_send(content, **kw)
    chan.send = send
    await feed.tick(NOW)
    assert [o[0] for o in order] == ["save", "send"]
    assert order[0][1] == {f"spike:{CA2}": NOW} and order[0][2] == 1, "the save already held the post"


@pytest.mark.asyncio
async def test_an_unreachable_channel_costs_the_post_not_a_repeat(world):
    m, chan, feed = world.market, world.chan, world.feed
    m.trending = [spike_pool()]
    m.snaps[CA2] = snapshot(CA2, liq=50_000.0)
    chan.fail = RuntimeError("Missing Access")
    await feed.tick(NOW)
    assert chan.sent == [] and storage.scan_events == []
    st = feed.status(1, NOW)
    assert st["unreachable"] and "channel unreachable" in st["text"]
    assert discovery.seen[f"spike:{CA2}"] == NOW
    chan.fail = None
    await feed.tick(NOW + 60)
    assert chan.sent == [], "the token is seen; a fixed channel does not replay it"
    assert discovery.configs[1].enabled, "the config is never switched off behind the manager's back"


@pytest.mark.asyncio
async def test_pending_is_capped_at_ten_keeping_the_strongest(world, monkeypatch):
    m, feed = world.market, world.feed
    monkeypatch.setattr(discovery, "FEED_MAX_PENDING", 3)
    m.new = [pool(f"0xn{i}", f"T{i}", "0x" + f"{i:02d}" * 20, liq=8_000.0, buys=20, sells=2, buyers=8 + i, created=NOW - 60)
             for i in range(5)]
    await feed.tick(NOW)
    assert len(discovery.pending) == 3
    assert sorted(p.first_buyers for p in discovery.pending.values()) == [10, 11, 12], "the weakest made room"


@pytest.mark.asyncio
async def test_disabled_config_and_no_config_do_nothing(world):
    m, chan, feed = world.market, world.chan, world.feed
    m.trending = [spike_pool()]
    m.snaps[CA2] = snapshot(CA2, liq=50_000.0)
    discovery.configs[1].enabled = False
    await feed.tick(NOW)
    assert chan.sent == [] and m.refreshed == []
    assert "**off**" in feed.status(1, NOW)["text"]
    assert feed.status(42, NOW)["enabled"] is False and "No feed in this server" in feed.status(42, NOW)["text"]


@pytest.mark.asyncio
async def test_status_text_shape(world):
    m, feed = world.market, world.feed
    m.trending = [spike_pool()]
    m.snaps[CA2] = snapshot(CA2, liq=50_000.0, mc=100_000.0)
    await feed.tick(NOW)
    ev = storage.scan_events[0]
    ev.peak_mc, ev.last_mc = 140_000.0, 90_000.0
    storage.scan_events.append(ScanEvent(ca=CA3, guild_id=1, channel_id=99, scanner_id=0, name="Mov", symbol="MOV",
                                         mc_at_scan=100_000.0, ts=NOW - 600, peak_mc=250_000.0, last_mc=200_000.0,
                                         source="feed", kind="mover"))
    storage.scan_events.append(ScanEvent(ca=CA4, guild_id=1, channel_id=99, scanner_id=5, name="Rick", symbol="RICK",
                                         mc_at_scan=1.0, ts=NOW - 60, peak_mc=9.0, last_mc=9.0))
    lines = feed.status(1, NOW + 23)["lines"]
    assert lines[0] == "Feed → <#99> · **on**"
    assert lines[1] == "Kinds: new pairs, spikes, movers · liq ≥ $5K · buyers ≥ 8 · pace ≥ 3x · move ≥ +25%"
    assert lines[2] == "Last hour: **1** post (cap 10) · pending 0 · last fetch 23s ago"
    assert lines[3] == "Last 24h: 2 posts · median peak **1.95x** · 1 hit 2x · 1 below entry", "scanner-bot events are not the feed's"
    assert lines[4].startswith("GeckoTerminal ~") and "DexScreener ~" in lines[4]
    discovery.configs[1].muted[CA4] = NOW + 999
    assert feed.status(1, NOW + 23)["lines"][-1] == "Muted: 1 token"


# ---------------- the shared tracker ----------------


@pytest.fixture
def scans(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "SCANS_FILE", str(tmp_path / "scans.json"))
    monkeypatch.setattr(storage, "MOVES_FILE", str(tmp_path / "moves.json"))
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    storage.scan_events.clear()
    storage.move_alerts.clear()
    yield tmp_path
    storage.scan_events.clear()
    storage.move_alerts.clear()


def feed_event(ca, ts, mc=100.0):
    return ScanEvent(ca=ca, guild_id=1, channel_id=99, scanner_id=0, name=ca[:6], symbol=ca[2:6].upper(),
                     mc_at_scan=mc, ts=ts, peak_mc=mc, last_mc=mc, source="feed", kind="spike")


@pytest.mark.asyncio
async def test_tracker_updates_a_feed_events_peak_without_the_scans_cog(scans, monkeypatch):
    storage.scan_events.append(feed_event(CA2, NOW - 600))
    stale = feed_event(CA3, NOW - 49 * 3600)
    storage.scan_events.append(stale)
    asked = []

    async def summary(ca):
        asked.append(ca)
        return {"mc": 250.0} if ca == CA2 else None
    monkeypatch.setattr(tracker, "token_summary", summary)

    assert await tracker.tick(NOW) == 1
    ev = storage.scan_events[0]
    assert ev.peak_mc == 250.0 and ev.peak_ts == NOW and ev.last_mc == 250.0 and ev.last_checked_ts == NOW
    assert ev.multiple() == pytest.approx(2.5)
    assert asked == [CA2], "outside the 48h window: not polled"
    assert stale.peak_mc == 100.0
    assert (scans / "scans.json").exists()

    async def lower(ca):
        return {"mc": 200.0}
    monkeypatch.setattr(tracker, "token_summary", lower)
    await tracker.tick(NOW + 300)
    assert ev.peak_mc == 250.0 and ev.last_mc == 200.0, "the peak never falls"

    async def nothing(ca):
        return None
    monkeypatch.setattr(tracker, "token_summary", nothing)
    await tracker.tick(NOW + 600)
    assert ev.last_mc == 200.0 and ev.last_checked_ts == NOW + 300, "no data is not a price"


@pytest.mark.asyncio
async def test_tracker_reads_at_most_forty_tokens_newest_first(scans, monkeypatch):
    for i in range(45):
        ca = "0x" + f"{i:02d}" * 20
        storage.scan_events.append(feed_event(ca, NOW - i * 10))
        storage.scan_events.append(feed_event(ca, NOW - i * 10 - 5))         # a second event per token
    asked = []

    async def summary(ca):
        asked.append(ca)
        return {"mc": 300.0}
    monkeypatch.setattr(tracker, "token_summary", summary)
    assert await tracker.tick(NOW) == 40
    assert len(asked) == 40 == len(set(asked)), "one read per distinct token, capped"
    assert asked[0] == "0x" + "00" * 20 and asked[-1] == "0x" + "39" * 20, "newest first"
    assert all(s.peak_mc == 300.0 for s in storage.scan_events if s.ca in asked), "every event of a read token is updated"
    assert tracker.tokens_to_check(NOW, limit=3) == asked[:3]


@pytest.mark.asyncio
async def test_tracker_retires_expired_auto_alerts_and_idles_when_nothing_is_live(scans, monkeypatch):
    from mccapbot.models import MoveAlert
    storage.move_alerts.append(MoveAlert(ca=CA2, pct=30, window_sec=3600, direction="both", channel_id=1, creator_id=2,
                                         guild_id=1, name="B", symbol="B", auto_expires_ts=time.time() - 1))
    called = []

    async def summary(ca):
        called.append(ca)
        return {"mc": 1.0}
    monkeypatch.setattr(tracker, "token_summary", summary)
    assert await tracker.tick(NOW) == 0
    assert storage.move_alerts == [] and called == []


@pytest.mark.asyncio
async def test_tracker_loop_stops_with_the_bot_and_survives_a_bad_tick(scans, monkeypatch):
    ticks = []

    async def tick(now=None):
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("boom")
        bot.closed = True
        return 0
    monkeypatch.setattr(tracker, "tick", tick)
    bot = FakeBot(None, Chan())
    await asyncio.wait_for(tracker.run(bot, interval=0), timeout=2)
    assert len(ticks) == 2


def test_scans_cog_no_longer_owns_a_tracker():
    from mccapbot.cogs import scans as cog
    assert not hasattr(cog.ScansCog, "_track_loop") and not hasattr(cog.ScansCog, "_expire_auto_moves")
    assert "cog_unload" not in cog.ScansCog.__dict__, "nothing to cancel: the cog owns no task"


@pytest.mark.asyncio
async def test_feed_loop_is_off_under_the_master_switch(world, monkeypatch):
    monkeypatch.setattr(discovery, "FEED_ENABLE", False)
    await asyncio.wait_for(world.feed.run(), timeout=1)
    assert "off (FEED_ENABLE=0)" in world.feed.status(1, NOW)["text"]


def test_the_tracker_takes_the_bot_first_so_it_cannot_sleep_on_it():
    """The bot was once passed where the delay goes: asyncio.sleep raised, and
    because the sleep was what failed the loop ran flat out. The bot is the
    first parameter now, and a nonsense delay falls back instead of spinning."""
    import inspect
    from mccapbot import bot as bot_module
    from mccapbot.config import SCAN_TRACK_INTERVAL

    params = list(inspect.signature(tracker.run).parameters)
    assert params[0] == "bot", "a caller with a bot in hand must not be able to pass it as the delay"
    assert "tracker.run(self)" in inspect.getsource(bot_module.Bot.setup_hook)

    async def one_pass(interval):
        seen = []

        async def fake_sleep(d):
            seen.append(d)
            raise asyncio.CancelledError                    # stop after the first sleep
        real_sleep = asyncio.sleep
        asyncio.sleep = fake_sleep
        try:
            with pytest.raises(asyncio.CancelledError):
                await tracker.run(None, interval=interval)
        finally:
            asyncio.sleep = real_sleep
        return seen

    assert asyncio.run(one_pass(12)) == [12]
    assert asyncio.run(one_pass(object())) == [SCAN_TRACK_INTERVAL], "a nonsense delay falls back, never spins"



# ---------------- what a call records, and what votes do to the next one ----------------


@pytest.mark.asyncio
async def test_a_post_records_the_numbers_it_fired_on(world):
    """Without this the record can say how a call did but never why it was
    made, and no amount of grading tells a good threshold from a lucky one."""
    m, chan, feed = world.market, world.chan, world.feed
    discovery.configs[1] = cfg()
    m.trending = [spike_pool()]
    m.snaps[CA2] = snapshot(CA2, liq=50_000.0)
    await feed.tick(NOW)
    [ev] = [s for s in storage.scan_events if s.source == "feed"]
    assert ev.signals["kind"] == "spike"
    assert ev.signals["pace"] == pytest.approx(5.0)
    assert ev.signals["buyers"] == 15
    assert ev.signals["liq"] == pytest.approx(50_000.0)
    assert ev.signals["stock"] == "no"
    assert ev.signals["depth"] == pytest.approx(50_000.0 / 400_000.0 * 100)
    assert ev.votes == {}


def test_a_tokenized_share_is_recognised_by_its_suffix_not_the_word():
    """"Robinhood Wallet" and "Robinhood Hat Strategy" are ordinary memecoins
    that happen to be named after the company; NVDA is not."""
    assert discovery.is_stock_token("NVIDIA • Robinhood Token")
    assert discovery.is_stock_token("  tesla • robinhood token  ")
    assert not discovery.is_stock_token("Robinhood Wallet")
    assert not discovery.is_stock_token("Robinhood Hat Strategy")
    assert not discovery.is_stock_token("")


@pytest.mark.asyncio
async def test_the_buttons_under_a_post_can_vote_on_that_exact_call(world):
    m, chan, feed = world.market, world.chan, world.feed
    discovery.configs[1] = cfg()
    m.trending = [spike_pool()]
    m.snaps[CA2] = snapshot(CA2, liq=50_000.0)
    await feed.tick(NOW)
    [ev] = [s for s in storage.scan_events if s.source == "feed"]
    view = chan.sent[-1][1].get("view")
    ids = [c.custom_id for c in view.children]
    assert f"rh:vote:{ev.id}:up" in ids and f"rh:vote:{ev.id}:down" in ids


def test_what_people_vote_down_ranks_lower_next_time(world):
    """The whole point: a pattern the channel keeps marking down stops winning
    the hour's budget, without anyone editing a threshold."""
    from mccapbot import grading
    grading.clear_cache()
    good = candidate("spike", CA2, "GOOD", buyers=10, pace=1.0)
    bad = candidate("mover", CA3, "BAD", buyers=10, pace=1.0)
    assert good.score() == bad.score(), "identical on the rules alone"
    assert discovery.strength(good) == pytest.approx(discovery.strength(bad)), "and identical with nothing learned"

    n = grading.FEED_LEARN_MIN_SAMPLES
    for _ in range(n):
        storage.scan_events.append(discovery.ScanEvent(
            ca=CA4, guild_id=1, channel_id=99, scanner_id=0, name="X", symbol="X", mc_at_scan=1e6,
            ts=NOW - 7200, peak_mc=1e6, last_mc=1e6, source="feed", kind="mover",
            signals={"kind": "mover"}, votes={"u1": -1, "u2": -1, "u3": -1},
        ))
    grading.clear_cache()
    grading.table(storage.scan_events, force=True)
    assert discovery.strength(bad) < discovery.strength(good)
    picked, held = discovery.select([good, bad], cfg(max_per_hour=1), NOW)
    assert [c.symbol for c in picked] == ["GOOD"] and [c.symbol for c in held] == ["BAD"]
    grading.clear_cache()


# ---------------- a refused request is not a blind tick ----------------


@pytest.mark.asyncio
async def test_a_tick_still_works_when_one_of_its_sources_was_refused(world):
    """The provider refuses the tail of a burst, so the busiest-pools call was
    losing every minute — and the tick threw away the sources that had already
    answered. The feed went blind for the better part of an hour."""
    m, chan, feed = world.market, world.chan, world.feed
    discovery.configs[1] = cfg()
    m.trending = [spike_pool()]
    m.snaps[CA2] = snapshot(CA2, liq=50_000.0)
    monkey_error(rhchain, "something was refused", ok_ts=NOW - 5)
    await feed.tick(NOW)
    assert len(chan.sent) == 1, "four good sources are still a listing"


@pytest.mark.asyncio
async def test_a_tick_stands_down_once_the_listing_is_genuinely_stale(world):
    m, chan, feed = world.market, world.chan, world.feed
    discovery.configs[1] = cfg()
    m.trending = [spike_pool()]
    m.snaps[CA2] = snapshot(CA2, liq=50_000.0)
    monkey_error(rhchain, "everything is refused",
                 ok_ts=NOW - discovery.FETCH_STALE_SECONDS - 1)
    await feed.tick(NOW)
    assert chan.sent == [], "this is history, not a listing"


def monkey_error(mod, message, *, ok_ts):
    mod.last_error = message
    mod.last_error_ts = ok_ts
    mod.last_ok_ts = ok_ts


@pytest.mark.asyncio
async def test_the_feed_paces_its_own_requests_rather_than_bursting(world, monkeypatch):
    """It has a minute and spends about a second of it; the spacing is free."""
    slept = []
    monkeypatch.setattr(discovery, "FETCH_SPACING", 2.0)
    monkeypatch.setattr(discovery.asyncio, "sleep", lambda s: slept.append(s) or _noop())
    await world.feed._fetch(NOW)
    assert slept == [2.0, 2.0], "between the three sources, not before the first"


def _noop():
    async def go():
        return None
    return go()
