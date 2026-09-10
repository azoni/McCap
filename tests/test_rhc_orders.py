"""The auto-order engine and its helpers.

Rules are parsed once, debounced on the watcher's cache, re-checked fresh at
fire time, marked `firing` on disk before any money moves, and every outcome
(fill, pending, busy, failure, retry, retire, expiry, restart) is written out
and reported. The chain, Kyber and the executor are all stand-ins; the engine
never refunds; nothing is signed.
"""

import asyncio
import json
from types import SimpleNamespace

import discord
import pytest

from mccapbot import storage
from mccapbot.cache import token_cache
from mccapbot.models import TokenSnapshot
from mccapbot.rhc import chain, kyber, ledger, orders, swap, trade, wallets
from tests.rhc_fakes import PONS, USER, Chan, FakeBot, arm_world, disarm_world, make_order

CA = chain.to_checksum(PONS)
T0 = 1_800_000_000.0


# ---------------- fixtures and helpers ----------------

class Clock:
    def __init__(self):
        self.t = T0

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(orders, "_now", lambda: c.t)
    return c


@pytest.fixture
def world(monkeypatch, clock):
    w = arm_world(monkeypatch)
    storage.auto_orders.clear()
    token_cache.clear()
    yield w
    disarm_world()
    storage.auto_orders.clear()
    token_cache.clear()


def fake_cog(deny=None, gate=None, calls=None):
    def deny_fn(uid, gid):
        if calls is not None:
            calls.append("deny")
        return deny

    return SimpleNamespace(deny=deny_fn, gate_reason=lambda: gate)


def engine(cog=None, chan=None):
    bot = FakeBot(cog if cog is not None else fake_cog(), chan or Chan())
    return orders.Engine(bot), bot


def snap(mc, ts, vol1h=None):
    token_cache[CA] = TokenSnapshot(mc=mc, url="", updated_ts=ts, vol1h=vol1h)


def on_disk():
    with open(storage.RHC_ORDERS_FILE, encoding="utf-8") as f:
        return json.load(f)


def disk_status(order_id):
    return {d["id"]: d["status"] for d in on_disk()}.get(order_id)


async def arm(**overrides):
    overrides.setdefault("expires_ts", T0 + 3600)
    o = make_order(**overrides)
    storage.auto_orders.append(o)
    await storage.save_orders()
    return o


async def trigger(eng, clock, mc=600_000.0, vol1h=None):
    """Two fresh cache samples meeting the rule, then let the fire finish."""
    clock.advance(10)
    snap(mc, clock.t - 1, vol1h)
    await eng.tick()
    clock.advance(10)
    snap(mc, clock.t - 1, vol1h)
    await eng.tick()
    await eng.drain()


def not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")


# ---------------- parse_expiry ----------------

def test_parse_expiry_accepts_hours_and_days_and_falls_back_to_the_default(clock):
    assert orders.parse_expiry("1h", "7d", "30d") == T0 + 3600
    assert orders.parse_expiry("12h", "7d", "30d") == T0 + 12 * 3600
    assert orders.parse_expiry("3d", "7d", "30d") == T0 + 3 * 86400
    assert orders.parse_expiry("30d", "7d", "30d") == T0 + 30 * 86400
    assert orders.parse_expiry(None, "7d", "30d") == T0 + 7 * 86400
    assert orders.parse_expiry("", "24h", "30d") == T0 + 86400
    assert orders.parse_expiry(None, orders.RHC_AUTO_BUY_TTL, orders.RHC_AUTO_MAX_TTL) == T0 + 86400
    assert orders.parse_expiry(None, orders.RHC_AUTO_SELL_TTL, orders.RHC_AUTO_MAX_TTL) == T0 + 7 * 86400


def test_parse_expiry_refuses_too_short_too_long_and_junk(clock):
    with pytest.raises(ValueError, match="at least 1h"):
        orders.parse_expiry("30m", "7d", "30d")
    with pytest.raises(ValueError, match="at most 30d"):
        orders.parse_expiry("31d", "7d", "30d")
    with pytest.raises(ValueError, match="duration"):
        orders.parse_expiry("soon", "7d", "30d")


# ---------------- sell_rule / buy_rule / already_met ----------------

def test_sell_rule_direction_comes_from_the_spec_not_the_price():
    r = orders.sell_rule("2x", 250_000.0, 260_000.0, "entry")
    assert (r.direction, r.target, r.spec, r.anchor_mc, r.anchor) == ("above", 500_000.0, "2x", 250_000.0, "entry")
    r = orders.sell_rule("-30%", 250_000.0, 260_000.0, "now")
    assert r.direction == "below" and r.target == pytest.approx(175_000.0) and r.spec == "-30%"
    r = orders.sell_rule("0.5x", 250_000.0, 100_000.0, "entry")      # below even though the price already sits under it
    assert r.direction == "below" and r.target == 125_000.0
    r = orders.sell_rule("+50%", 200_000.0, None, "now")
    assert r.direction == "above" and r.target == 300_000.0
    assert orders.sell_rule("1x", 250_000.0, 260_000.0, "entry").direction == "above"


def test_sell_rule_absolute_targets_look_at_the_price_now():
    r = orders.sell_rule("500k", 250_000.0, 260_000.0, "entry")
    assert (r.direction, r.target, r.spec, r.anchor) == ("above", 500_000.0, "", "")
    assert orders.sell_rule("100k", 250_000.0, 260_000.0, "entry").direction == "below"
    assert orders.sell_rule("100k", None, None, "now").direction == "above"     # unknown price: assume a take-profit


def test_sell_rule_refusals_are_user_facing():
    with pytest.raises(ValueError, match="no market cap to anchor"):
        orders.sell_rule("2x", None, None, "now")
    with pytest.raises(ValueError, match="Could not read"):
        orders.sell_rule("moon", 250_000.0, 260_000.0, "entry")
    with pytest.raises(ValueError):
        orders.sell_rule("", 250_000.0, 260_000.0, "entry")


def test_buy_rule_for_the_three_choices_and_bad_input():
    assert orders.buy_rule("mc_below", "200k") == orders.Rule("mc", "below", 200_000.0)
    assert orders.buy_rule("mc_above", "1.5m") == orders.Rule("mc", "above", 1_500_000.0)
    assert orders.buy_rule("vol1h_above", "50k") == orders.Rule("vol1h", "above", 50_000.0)
    with pytest.raises(ValueError, match="Pick a condition"):
        orders.buy_rule("price_below", "1")
    with pytest.raises(ValueError, match="Could not read"):
        orders.buy_rule("mc_below", "lots")
    with pytest.raises(ValueError, match="above zero"):
        orders.buy_rule("mc_below", "0")


def test_already_met_never_fires_on_an_unknown_value():
    assert orders.already_met("above", 600_000.0, 500_000.0)
    assert orders.already_met("below", 100_000.0, 200_000.0)
    assert not orders.already_met("above", 400_000.0, 500_000.0)
    assert not orders.already_met("below", None, 200_000.0)


# ---------------- describe_rule / classify ----------------

def test_describe_rule_covers_every_shape():
    o = make_order(spec="2x", anchor_mc=250_000.0, anchor="entry")
    assert orders.describe_rule(o) == "sell 50% PONS when MC ≥ $500K (2x from your $250K entry)"
    o = make_order(direction="below", target=175_000.0, spec="-30%", anchor_mc=250_000.0, anchor="now")
    assert orders.describe_rule(o) == "sell 50% PONS when MC ≤ $175K (-30% from now)"
    o = make_order(direction="below", target=300_000.0)
    assert orders.describe_rule(o) == "sell 50% PONS when MC ≤ $300K"
    o = make_order(side="buy", direction="below", target=200_000.0, size=10.0)
    assert orders.describe_rule(o) == "buy $10.00 PONS when MC ≤ $200K"      # usd() keeps cents everywhere
    o = make_order(side="buy", metric="vol1h", direction="above", target=50_000.0, size=10.0)
    assert orders.describe_rule(o) == "buy $10.00 PONS when 1h volume ≥ $50K"
    assert orders.describe_rule(make_order(size=100.0)).startswith("sell 100% PONS")
    # Trailing: the high it trails, not the anchor it started from.
    o = make_order(size=100.0, direction="below", target=400_000.0, spec="trail 20%", anchor="now",
                   anchor_mc=450_000.0, trail_pct=20.0, high_mc=500_000.0)
    assert orders.describe_rule(o) == "sell 100% PONS when MC ≤ $400K (trail 20% below its $500K high)"
    o = make_order(size=100.0, direction="below", target=400_000.0, spec="trail 20%", anchor="now",
                   anchor_mc=500_000.0, trail_pct=20.0)                     # never ratcheted: the anchor is the high
    assert orders.describe_rule(o) == "sell 100% PONS when MC ≤ $400K (trail 20% below its $500K high)"
    # A buy that arms protection on fill says so; a sell with a stray `then` does not.
    o = make_order(side="buy", direction="below", target=200_000.0, size=10.0, then="tp=2x:50,sl=-30%:100")
    assert orders.describe_rule(o) == "buy $10.00 PONS when MC ≤ $200K · then TP 2x / SL -30%"
    o = make_order(side="buy", direction="below", target=200_000.0, size=10.0, then="sl=-30%:100")
    assert orders.describe_rule(o) == "buy $10.00 PONS when MC ≤ $200K · then SL -30%"
    o = make_order(side="buy", direction="below", target=200_000.0, size=10.0, then="junk")
    assert orders.describe_rule(o) == "buy $10.00 PONS when MC ≤ $200K · then junk"
    assert orders.describe_rule(make_order(then="tp=2x:50")) == "sell 50% PONS when MC ≥ $500K"
    # The metrics later items add never render as a dollar level.
    o = make_order(side="buy", metric="move", direction="above", target=25.0, size=10.0, window_sec=900)
    assert orders.describe_rule(o) == "buy $10.00 PONS when it moves ≥ +25% in 15m"
    o = make_order(side="buy", metric="now", direction="above", target=0.0, size=5.0, then="tp=2x:50,sl=-30%:100")
    assert orders.describe_rule(o) == "buy $5.00 PONS now · then TP 2x / SL -30%"
    o = make_order(side="buy", metric="fill", direction="above", target=0.0, size=5.0)
    assert orders.describe_rule(o) == "buy $5.00 PONS (waiting for the fill)"


def test_classify_tells_a_busy_wallet_from_a_failure():
    assert orders.classify(swap.SwapResult(ok=True, tx="0x1")) == "filled"
    assert orders.classify(swap.SwapResult(ok=False, tx="0x1", pending=True, error="unconfirmed")) == "pending"
    assert orders.classify(swap.SwapResult(ok=False, error=swap.IN_FLIGHT_TEXT)) == "busy"
    assert orders.classify(swap.SwapResult(ok=False, error="Your earlier transaction 0x9 is still unconfirmed.")) == "busy"
    assert orders.classify(swap.SwapResult(ok=False, error="Swap reverted on-chain.")) == "failed"


# ---------------- debounce ----------------

@pytest.mark.asyncio
async def test_same_sample_counts_once_and_two_fresh_hits_fire(world, clock):
    eng, bot = engine()
    o = await arm()
    snap(600_000.0, clock.t - 1)
    await eng.tick()
    await eng.tick()
    await eng.tick()
    assert eng._hits[o.id][0] == 1 and world.executed == []
    clock.advance(10)
    snap(600_000.0, clock.t - 1)
    await eng.tick()
    await eng.drain()
    assert len(world.executed) == 1


@pytest.mark.asyncio
async def test_stale_samples_never_count(world, clock):
    eng, _ = engine()
    o = await arm()
    snap(600_000.0, clock.t - orders.STALE_SECONDS - 1)
    await eng.tick()
    clock.advance(10)
    snap(600_000.0, clock.t - orders.STALE_SECONDS - 1)
    await eng.tick()
    await eng.drain()
    assert o.id not in eng._hits and world.executed == []


@pytest.mark.asyncio
async def test_a_missing_value_is_never_zero(world, clock):
    eng, _ = engine()
    o_vol = await arm(side="buy", metric="vol1h", direction="above", target=50_000.0, size=10.0)
    o_mc = await arm(direction="below", target=300_000.0, user_id=8)
    snap(None, clock.t - 1, vol1h=None)                 # mc None would "meet" a below rule if coerced to 0
    await eng.tick()
    clock.advance(10)
    snap(None, clock.t - 1, vol1h=None)
    await eng.tick()
    await eng.drain()
    assert o_vol.id not in eng._hits and o_mc.id not in eng._hits and world.executed == []


@pytest.mark.asyncio
async def test_a_miss_resets_the_count(world, clock):
    eng, _ = engine()
    o = await arm()
    snap(600_000.0, clock.t - 1)
    await eng.tick()
    clock.advance(10)
    snap(400_000.0, clock.t - 1)
    await eng.tick()
    assert eng._hits[o.id][0] == 0
    clock.advance(10)
    snap(600_000.0, clock.t - 1)
    await eng.tick()
    await eng.drain()
    assert eng._hits[o.id][0] == 1 and world.executed == []


@pytest.mark.asyncio
async def test_no_snapshot_means_no_progress(world, clock):
    eng, _ = engine()
    o = await arm()
    await eng.tick()
    assert o.id not in eng._hits


# ---------------- the buy fire path ----------------

@pytest.mark.asyncio
async def test_auto_buy_runs_the_guards_in_order_and_marks_firing_before_the_money_step(world, clock, monkeypatch):
    calls = []

    def spy(mod, name, label):
        orig = getattr(mod, name)
        if asyncio.iscoroutinefunction(orig):
            async def w(*a, **k):
                calls.append(label)
                return await orig(*a, **k)
        else:
            def w(*a, **k):
                calls.append(label)
                return orig(*a, **k)
        monkeypatch.setattr(mod, name, w)

    spy(wallets, "get", "wallet")
    spy(trade, "token_summary", "summary")
    spy(kyber, "route", "route")
    spy(trade, "usd_basis", "usd_basis")
    spy(ledger, "check", "check")
    spy(chain, "native_balance", "native_balance")
    spy(chain, "gas_price", "gas_price")
    spy(trade, "round_trip", "round_trip")
    spy(storage, "save_orders", "save")
    spy(ledger, "record", "record")
    spy(kyber, "build", "build")
    seen = {}
    real_execute = swap.execute

    async def execute(uid, built, token, sym, extra=None):
        calls.append("execute")
        seen["disk"] = disk_status(o.id)
        seen["mem"] = o.status
        seen["in_list"] = o in storage.auto_orders
        return await real_execute(uid, built, token, sym, extra)
    monkeypatch.setattr(swap, "execute", execute)

    eng, bot = engine(fake_cog(calls=calls))
    world.mc_now = 150_000.0
    o = await arm(side="buy", direction="below", target=200_000.0, size=10.0)
    calls.clear()
    await trigger(eng, clock, mc=150_000.0)

    assert calls == ["deny", "wallet", "summary", "route", "usd_basis", "check", "native_balance", "gas_price",
                     "round_trip", "route", "save", "route", "usd_basis", "check", "record", "build", "execute", "save"]
    assert seen == {"disk": "firing", "mem": "firing", "in_list": True}
    assert on_disk() == [] and storage.auto_orders == []


@pytest.mark.asyncio
async def test_buy_fill_is_reported_with_a_panel_and_journaled_as_auto(world, clock, monkeypatch):
    from mccapbot import views
    monkeypatch.setattr(views, "receipt_row", lambda uid, addr: ("ROW", uid, addr), raising=False)
    eng, bot = engine()
    world.mc_now = 150_000.0
    o = await arm(side="buy", direction="below", target=200_000.0, size=10.0)
    await trigger(eng, clock, mc=150_000.0)

    assert storage.auto_orders == [] and on_disk() == []
    content, kw = bot.channel.sent[-1]
    assert content.startswith(f"<@{USER}>{orders.SEP}🤖 `{o.id}` fired{orders.SEP}✅ Bought 36 PONS")
    assert "buy $10.00 PONS when MC ≤ $200K" in content and "[Transaction](" in content
    assert kw["view"] == ("ROW", USER, CA) and kw["suppress_embeds"] is True
    assert kw["allowed_mentions"].users is True and kw["allowed_mentions"].everyone is False
    assert ledger.spent_today(USER) == pytest.approx(24.8)
    assert world.last_extra["source"] == "auto" and world.last_extra["order_id"] == o.id
    assert world.last_extra["rule"] == orders.describe_rule(o)
    assert world.last_extra["decimals"] == 18 and world.last_extra["eth_usd"] == 2500.0


@pytest.mark.asyncio
async def test_pending_fill_is_tracked_and_keeps_its_reservation(world, clock):
    eng, bot = engine()
    world.mc_now = 150_000.0
    world.result = swap.SwapResult(ok=False, tx="0xdef", pending=True, error="Submitted but unconfirmed.")
    o = await arm(side="buy", direction="below", target=200_000.0, size=10.0)
    await trigger(eng, clock, mc=150_000.0)

    assert o.status == "pending" and o.tx == "0xdef" and disk_status(o.id) == "pending"
    assert on_disk()[0]["tx"] == "0xdef"
    text = bot.channel.texts[-1]
    assert "⏳ Sent but unconfirmed after 2m" in text and "0xdef" in text and f"<@{USER}>" in text
    assert ledger.spent_today(USER) == pytest.approx(24.8)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,mark", [("confirmed", "✅ confirmed on chain"),
                                         ("reverted", "❌ reverted on chain; nothing was swapped"),
                                         ("dropped", "🚫 dropped after 15m; nothing moved")])
async def test_pending_resolution_is_reported_once_it_is_known(world, clock, monkeypatch, status, mark):
    polls = []

    async def poll_pending(uid, tx):
        polls.append((uid, tx))
        return polls_result[0]
    polls_result = ["pending"]
    monkeypatch.setattr(swap, "poll_pending", poll_pending)
    eng, bot = engine()
    o = await arm(status="pending", tx="0xdef")
    await eng.tick()
    await eng.tick()                                     # inside PENDING_POLL_SECONDS: no second poll
    assert polls == [(USER, "0xdef")] and o in storage.auto_orders
    clock.advance(orders.PENDING_POLL_SECONDS)
    polls_result[0] = status
    await eng.tick()
    assert len(polls) == 2
    assert storage.auto_orders == [] and on_disk() == []
    text = bot.channel.texts[-1]
    assert text.startswith(f"<@{USER}>{orders.SEP}🤖 `{o.id}`{orders.SEP}{mark}") and "0xdef" in text


@pytest.mark.asyncio
async def test_busy_wallet_is_a_hold_not_an_attempt(world, clock):
    eng, bot = engine()
    world.mc_now = 150_000.0
    world.result = swap.SwapResult(ok=False, error=swap.IN_FLIGHT_TEXT)
    o = await arm(side="buy", direction="below", target=200_000.0, size=10.0)
    await trigger(eng, clock, mc=150_000.0)

    assert o.status == "armed" and disk_status(o.id) == "armed"
    assert o.id not in eng._hits
    assert eng._retry_after[o.id] == clock.t + orders.RETRY_SECONDS
    assert ledger.spent_today(USER) == 0.0          # settle refunded once; the engine did not refund again
    assert bot.channel.sent == []
    assert o.attempts == 1                          # the firing attempt was counted, nothing more


@pytest.mark.asyncio
async def test_inflight_wallet_is_never_spawned(world, clock, monkeypatch):
    monkeypatch.setattr(swap, "has_inflight", lambda uid: True)
    eng, _ = engine()
    o = await arm()
    await trigger(eng, clock)
    assert world.executed == [] and o.attempts == 0 and eng._hits[o.id][0] == 2


@pytest.mark.asyncio
async def test_one_fire_per_user_per_tick(world, clock):
    eng, _ = engine()
    a = await arm()
    b = await arm(target=400_000.0)
    snap(600_000.0, clock.t - 1)
    await eng.tick()
    clock.advance(10)
    snap(600_000.0, clock.t - 1)
    await eng.tick()
    assert len(eng._firing) == 1 and eng._firing_users == {USER}
    await eng.drain()
    assert eng._firing == set() and eng._firing_users == set()
    assert len(world.executed) == 1 and len(storage.auto_orders) == 1
    clock.advance(10)
    snap(600_000.0, clock.t - 1)
    await eng.tick()
    await eng.drain()
    assert len(world.executed) == 2 and storage.auto_orders == []
    assert {a.id, b.id} == {e["order_id"] for e in world.extras}


@pytest.mark.asyncio
async def test_fresh_read_that_no_longer_meets_is_a_hold(world, clock):
    eng, bot = engine()
    world.mc_now = 400_000.0                            # the cache said 600k; DexScreener now says 400k
    o = await arm()
    await trigger(eng, clock)
    assert world.route_calls == 0 and world.executed == []
    assert o.status == "armed" and o.attempts == 0 and o.id not in eng._hits
    assert bot.channel.sent == []


@pytest.mark.asyncio
async def test_price_disagreement_holds_an_auto_buy(world, clock):
    eng, bot = engine()
    world.mc_now = 150_000.0
    world.price_now = 0.4                               # Kyber implies $0.689 per PONS: 72% apart
    o = await arm(side="buy", direction="below", target=200_000.0, size=10.0)
    await trigger(eng, clock, mc=150_000.0)
    assert world.executed == [] and o.attempts == 0 and o.status == "armed"
    assert eng._retry_after[o.id] == clock.t + orders.RETRY_SECONDS
    assert ledger.spent_today(USER) == 0.0 and bot.channel.sent == []


@pytest.mark.asyncio
async def test_kyber_outage_backs_off_and_gives_up_after_five(world, clock, monkeypatch):
    async def down(*a, **k):
        raise kyber.KyberUnavailable("KyberSwap did not answer")
    monkeypatch.setattr(kyber, "route", down)
    eng, bot = engine()
    o = await arm()
    await trigger(eng, clock)
    assert o.attempts == 1 and o.status == "armed" and o.last_error
    assert eng._retry_after[o.id] == clock.t + orders.RETRY_SECONDS
    assert on_disk()[0]["attempts"] == 1
    # Inside the backoff nothing happens even with the condition met.
    clock.advance(10)
    snap(600_000.0, clock.t - 1)
    await eng.tick()
    await eng.drain()
    assert o.attempts == 1
    for n in range(2, orders.MAX_ATTEMPTS + 1):
        clock.advance(orders.RETRY_SECONDS)
        await trigger(eng, clock)
        if n < orders.MAX_ATTEMPTS:
            assert o.attempts == n and o in storage.auto_orders
    assert storage.auto_orders == [] and on_disk() == []
    text = bot.channel.texts[-1]
    assert f"🤖 `{o.id}` gave up after 5 tries" in text and "❌" in text and f"<@{USER}>" in text


@pytest.mark.asyncio
async def test_daily_cap_retires_a_buy_with_the_ledger_text(world, clock):
    ledger.record(USER, 190.0)
    eng, bot = engine()
    world.mc_now = 150_000.0
    o = await arm(side="buy", direction="below", target=200_000.0, size=10.0)
    await trigger(eng, clock, mc=150_000.0)
    assert storage.auto_orders == [] and world.executed == []
    text = bot.channel.texts[-1]
    assert f"🤖 `{o.id}` stopped" in text and "over the daily cap" in text and "nothing was bought" in text
    assert ledger.spent_today(USER) == pytest.approx(190.0)


@pytest.mark.asyncio
async def test_honeypot_retires_a_buy_without_touching_the_ledger(world, clock):
    world.back_error = kyber.NoRoute("No route for that pair on Robinhood Chain")
    eng, bot = engine()
    world.mc_now = 150_000.0
    o = await arm(side="buy", direction="below", target=200_000.0, size=10.0)
    await trigger(eng, clock, mc=150_000.0)
    assert storage.auto_orders == [] and world.executed == []
    assert "honeypot" in bot.channel.texts[-1] and ledger.spent_today(USER) == 0.0


@pytest.mark.asyncio
async def test_gas_shortfall_retires_with_both_figures(world, clock):
    world.eth_balance = 10**15
    eng, bot = engine()
    world.mc_now = 150_000.0
    await arm(side="buy", direction="below", target=200_000.0, size=10.0)
    await trigger(eng, clock, mc=150_000.0)
    text = bot.channel.texts[-1]
    assert "Not enough ETH: you have 0.001 ETH and this needs about" in text and "including gas" in text
    assert storage.auto_orders == [] and world.executed == []


@pytest.mark.asyncio
async def test_reverted_swap_retires_and_settle_refunds(world, clock):
    world.result = swap.SwapResult(ok=False, tx="0xbad", error="Swap reverted on-chain. Nothing was swapped (gas was spent).")
    eng, bot = engine()
    world.mc_now = 150_000.0
    o = await arm(side="buy", direction="below", target=200_000.0, size=10.0)
    await trigger(eng, clock, mc=150_000.0)
    assert storage.auto_orders == [] and on_disk() == []
    text = bot.channel.texts[-1]
    assert f"🤖 `{o.id}` stopped{orders.SEP}❌ Swap reverted on-chain" in text and "nothing was bought" in text
    assert ledger.spent_today(USER) == 0.0


# ---------------- the sell fire path ----------------

@pytest.mark.asyncio
async def test_partial_sell_fill_carries_the_sell_rest_panel(world, clock, monkeypatch):
    from mccapbot import views
    monkeypatch.setattr(views, "partial_sell_row", lambda uid, addr: ("REST", uid, addr), raising=False)
    eng, bot = engine()
    world.result = swap.SwapResult(ok=True, tx="0xabc", amount_out=5 * 10**15, gas_cost_wei=10**14)
    o = await arm(spec="2x", anchor_mc=250_000.0, anchor="entry")
    await trigger(eng, clock)
    content, kw = bot.channel.sent[-1]
    assert content.startswith(f"<@{USER}>{orders.SEP}🤖 `{o.id}` fired{orders.SEP}✅ Sold 18 PONS for 0.005 ETH ($24.70)")
    assert "(2x from your $250K entry)" in content and "[Transaction](" in content
    assert kw["view"] == ("REST", USER, CA)
    assert world.executed[0].amount_in == 18 * 10**18 and world.executed[0].min_out == kyber.min_out(5 * 10**15, 200)
    assert world.last_extra["source"] == "auto" and world.last_extra["order_id"] == o.id
    assert ledger.spent_today(USER) == 0.0            # exits never touch the cap


@pytest.mark.asyncio
async def test_full_sell_fill_has_no_panel(world, clock, monkeypatch):
    from mccapbot import views
    monkeypatch.setattr(views, "partial_sell_row", lambda uid, addr: "REST", raising=False)
    eng, bot = engine()
    world.sell_route.amount_in = 36 * 10**18
    await arm(size=100.0)
    await trigger(eng, clock)
    assert "view" not in bot.channel.sent[-1][1]
    assert len(world.executed) == 1


@pytest.mark.asyncio
async def test_missing_panel_factory_never_stops_a_report(world, clock, monkeypatch):
    from mccapbot import views

    def boom(uid, addr):
        raise RuntimeError("no buttons today")
    monkeypatch.setattr(views, "partial_sell_row", boom, raising=False)
    eng, bot = engine()
    await arm()
    await trigger(eng, clock)
    assert "view" not in bot.channel.sent[-1][1] and "✅ Sold" in bot.channel.texts[-1]


@pytest.mark.asyncio
async def test_zero_balance_retires_a_sell_quietly(world, clock):
    world.token_balance = 0
    eng, bot = engine()
    o = await arm()
    await trigger(eng, clock)
    assert storage.auto_orders == [] and world.executed == []
    text = bot.channel.texts[-1]
    assert text == f"🤖 `{o.id}` retired{orders.SEP}nothing left to sell{orders.SEP}{orders.describe_rule(o)}"
    assert "<@" not in text


@pytest.mark.asyncio
async def test_take_profit_selling_everything_retires_the_stop_loss(world, clock):
    eng, bot = engine()
    world.sell_route.amount_in = 36 * 10**18
    tp = await arm(size=100.0)
    sl = await arm(size=100.0, direction="below", target=300_000.0)
    await trigger(eng, clock)
    assert tp not in storage.auto_orders and sl in storage.auto_orders and len(world.executed) == 1
    world.token_balance = 0                              # the TP emptied the position
    world.mc_now = 250_000.0
    await trigger(eng, clock, mc=250_000.0)
    assert storage.auto_orders == [] and len(world.executed) == 1
    assert f"🤖 `{sl.id}` retired{orders.SEP}nothing left to sell" in bot.channel.texts[-1]


@pytest.mark.asyncio
async def test_no_route_on_a_sell_is_definite(world, clock, monkeypatch):
    async def gone(token_in, token_out, amount_in):
        raise kyber.NoRoute("No route for that pair on Robinhood Chain (4008): no liquidity, or an unknown token.")
    monkeypatch.setattr(kyber, "route", gone)
    eng, bot = engine()
    o = await arm()
    await trigger(eng, clock)
    assert storage.auto_orders == []
    text = bot.channel.texts[-1]
    assert f"🤖 `{o.id}` stopped{orders.SEP}🚫 no route to sell PONS for ETH" in text and "nothing was sold" in text


@pytest.mark.asyncio
async def test_rpc_trouble_on_a_sell_is_a_retry(world, clock, monkeypatch):
    async def flaky(token, owner):
        raise chain.RpcUnavailable("every endpoint failed")
    monkeypatch.setattr(chain, "erc20_balance", flaky)
    eng, bot = engine()
    o = await arm()
    await trigger(eng, clock)
    assert o in storage.auto_orders and o.attempts == 1 and o.status == "armed" and bot.channel.sent == []


# ---------------- deny, wallet, cancel race, unexpected errors ----------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kind,phrase", [("allowlist", "you are no longer on the trader allowlist"),
                                         ("guild", "trading is not enabled in that server any more")])
async def test_deny_kinds_that_retire(world, clock, kind, phrase):
    eng, bot = engine(fake_cog(deny=(kind, "refused")))
    o = await arm()
    await trigger(eng, clock)
    assert storage.auto_orders == [] and world.summary_calls == 0 and world.executed == []
    text = bot.channel.texts[-1]
    assert f"<@{USER}>{orders.SEP}🤖 `{o.id}` stopped{orders.SEP}🚫 {phrase}" in text and "nothing was sold" in text


@pytest.mark.asyncio
async def test_gate_deny_at_fire_time_holds(world, clock):
    cog = fake_cog()
    eng, bot = engine(cog)
    o = await arm()
    snap(600_000.0, clock.t - 1)
    await eng.tick()
    cog.deny = lambda uid, gid: ("gate", "paused")     # flips between the tick and the fire
    clock.advance(10)
    snap(600_000.0, clock.t - 1)
    await eng.tick()
    await eng.drain()
    assert o in storage.auto_orders and o.status == "armed" and o.attempts == 0 and bot.channel.sent == []


@pytest.mark.asyncio
async def test_wallet_gone_retires(world, clock):
    eng, bot = engine()
    o = await arm()
    wallets.wallets.clear()
    await trigger(eng, clock)
    assert storage.auto_orders == [] and on_disk() == []
    assert "your wallet is gone from the vault" in bot.channel.texts[-1]


@pytest.mark.asyncio
async def test_cancel_between_plan_and_firing_means_no_trade(world, clock, monkeypatch):
    real_plan = trade.plan_sell

    async def plan_then_cancel(*a, **k):
        plan = await real_plan(*a, **k)
        storage.auto_orders.remove(o)                   # /rh auto cancel landed while we were quoting
        await storage.save_orders()
        return plan
    monkeypatch.setattr(trade, "plan_sell", plan_then_cancel)
    eng, bot = engine()
    o = await arm()
    await trigger(eng, clock)
    assert world.executed == [] and on_disk() == [] and bot.channel.sent == []
    assert o.status == "armed"


@pytest.mark.asyncio
async def test_unexpected_exception_retires_and_never_leaves_firing(world, clock, monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(kyber, "build", broken)
    eng, bot = engine()
    o = await arm()
    await trigger(eng, clock)
    assert storage.auto_orders == [] and on_disk() == []
    assert "❌ internal error; nothing further will run for this rule" in bot.channel.texts[-1]
    assert not any(d["status"] == "firing" for d in on_disk())


# ---------------- held engine, expiry, restart ----------------

@pytest.mark.asyncio
async def test_kill_switch_holds_rules_notifies_once_per_channel_and_still_expires(world, clock):
    eng, bot = engine(fake_cog(gate="Robinhood Chain trading is disabled (`RHC_TRADING_ENABLE=0`)."))
    a = await arm()
    b = await arm(channel_id=42, target=400_000.0)
    c = await arm(target=450_000.0)
    other = Chan(42)
    real_fetch = bot.fetch_channel

    async def fetch(cid):
        return other if cid == 42 else await real_fetch(cid)
    bot.fetch_channel = fetch
    doomed = await arm(expires_ts=clock.t - 1, target=300_000.0)

    await trigger(eng, clock)
    assert world.executed == [] and a in storage.auto_orders and b in storage.auto_orders
    assert c in storage.auto_orders and doomed not in storage.auto_orders and len(on_disk()) == 3
    assert eng.held_reason().startswith("Robinhood Chain trading is disabled")
    texts = bot.channel.texts
    assert texts[0].startswith(f"🤖 `{doomed.id}` expired") and "<@" not in texts[0]
    holds = [t for t in texts if t.startswith("⏸")]
    assert len(holds) == 1
    assert holds[0].startswith("⏸ Trading is paused (Robinhood Chain trading is disabled (`RHC_TRADING_ENABLE=0`))")
    assert "2 auto-orders here are on hold" in holds[0] and "they still expire on schedule" in holds[0]
    assert len(other.texts) == 1 and "1 auto-order here" in other.texts[0]
    await eng.tick()
    await eng.tick()
    assert len([t for t in bot.channel.texts if t.startswith("⏸")]) == 1 and len(other.texts) == 1


@pytest.mark.asyncio
async def test_auto_enable_off_idles_the_engine_with_its_own_notice(world, clock, monkeypatch):
    monkeypatch.setattr(orders, "RHC_AUTO_ENABLE", False)
    eng, bot = engine()
    o = await arm()
    await trigger(eng, clock)
    assert world.executed == [] and o in storage.auto_orders
    assert eng.held_reason() == "auto-orders are switched off (`RHC_AUTO_ENABLE=0`)"
    holds = [t for t in bot.channel.texts if t.startswith("⏸")]
    assert len(holds) == 1 and holds[0].startswith("⏸ Auto-orders are switched off (`RHC_AUTO_ENABLE=0`)")
    assert "Trading is paused" not in holds[0]


@pytest.mark.asyncio
async def test_missing_cog_holds(world, clock):
    eng, bot = engine()
    bot._cog = None
    await arm()
    await trigger(eng, clock)
    assert world.executed == [] and eng.held_reason() == "trading is not loaded"
    assert any(t.startswith("⏸ Trading is paused (trading is not loaded)") for t in bot.channel.texts)


@pytest.mark.asyncio
async def test_hold_notice_returns_after_trading_resumes_and_pauses_again(world, clock):
    cog = fake_cog(gate="paused.")
    eng, bot = engine(cog)
    await arm()
    await eng.tick()
    cog.gate_reason = lambda: None
    await eng.tick()
    cog.gate_reason = lambda: "paused."
    await eng.tick()
    assert len([t for t in bot.channel.texts if t.startswith("⏸")]) == 2
    assert eng.held_reason() == "paused."


@pytest.mark.asyncio
async def test_live_engine_reports_no_hold():
    eng, _ = engine()
    assert eng.held_reason() is None


@pytest.mark.asyncio
async def test_expiry_removes_saves_and_posts_quietly(world, clock):
    eng, bot = engine()
    snap(420_000.0, clock.t - 1)
    o = await arm(expires_ts=clock.t + 5)
    await eng.tick()
    assert o in storage.auto_orders
    clock.advance(5)
    await eng.tick()
    assert storage.auto_orders == [] and on_disk() == []
    assert bot.channel.texts == [f"🤖 `{o.id}` expired{orders.SEP}{orders.describe_rule(o)}{orders.SEP}now $420K"]


@pytest.mark.asyncio
async def test_firing_leftover_from_a_restart_is_retired_never_run(world, clock):
    o = make_order(status="firing", attempts=1, expires_ts=T0 + 3600)
    storage.auto_orders.append(o)
    await storage.save_orders()
    storage.auto_orders.clear()
    await storage.load_orders()
    assert storage.auto_orders[0].status == "firing"
    eng, bot = engine()
    snap(600_000.0, clock.t - 1)
    await eng.tick()
    await eng.drain()
    assert storage.auto_orders == [] and on_disk() == [] and world.executed == []
    text = bot.channel.texts[-1]
    assert text.startswith(f"<@{USER}>{orders.SEP}🤖 `{o.id}`{orders.SEP}⚠️ McCap restarted while this rule was executing")
    assert "`/rh history`" in text and orders.describe_rule(o) in text


# ---------------- reporting ----------------

@pytest.mark.asyncio
async def test_channel_gone_falls_back_to_a_dm(world, clock):
    eng, bot = engine(chan=Chan(fail=not_found()))
    o = await arm()
    await trigger(eng, clock)
    assert storage.auto_orders == []
    dm = bot.users[USER]
    assert len(dm.texts) == 1 and f"🤖 `{o.id}` fired" in dm.texts[0] and "[Transaction](" in dm.texts[0]
    assert "view" not in dm.sent[0][1]


@pytest.mark.asyncio
async def test_both_destinations_failing_is_logged_not_raised(world, clock, monkeypatch):
    eng, bot = engine(chan=Chan(fail=not_found()))

    async def no_user(uid):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no")
    monkeypatch.setattr(bot, "fetch_user", no_user)
    o = await arm()
    await trigger(eng, clock)
    assert storage.auto_orders == [] and len(world.executed) == 1     # the fill happened and state is consistent


@pytest.mark.asyncio
async def test_private_orders_report_by_dm_only(world, clock):
    eng, bot = engine()
    o = await arm(private=True)
    await trigger(eng, clock)
    assert bot.fetched == [] and bot.channel.sent == []
    dm = bot.users[USER]
    assert len(dm.texts) == 1 and f"<@{USER}>" in dm.texts[0] and f"🤖 `{o.id}` fired" in dm.texts[0]


@pytest.mark.asyncio
async def test_post_saves_before_it_speaks(world, clock):
    eng, bot = engine(chan=Chan(fail=not_found()))
    seen = {}

    async def fetch_user(uid):
        seen["disk"] = on_disk()
        return Chan(uid)
    bot.fetch_user = fetch_user
    await arm()
    await trigger(eng, clock)
    assert seen["disk"] == []


# ---------------- lifecycle ----------------

@pytest.mark.asyncio
async def test_run_survives_a_tick_error_and_stops_when_the_bot_closes(world, clock, monkeypatch):
    monkeypatch.setattr(orders, "POLL_TICK_SECONDS", 0)
    eng, bot = engine()
    ticks = []

    async def tick():
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("first tick breaks")
        if len(ticks) >= 3:
            bot.closed = True
    monkeypatch.setattr(eng, "tick", tick)
    await asyncio.wait_for(eng.run(), 2)
    assert len(ticks) == 3


@pytest.mark.asyncio
async def test_stop_cancels_the_loop_and_waits_for_fires(world, clock, monkeypatch):
    monkeypatch.setattr(orders, "POLL_TICK_SECONDS", 0.01)
    started, release = asyncio.Event(), asyncio.Event()
    real_execute = swap.execute

    async def slow_execute(*a, **k):
        started.set()
        await release.wait()
        return await real_execute(*a, **k)
    monkeypatch.setattr(swap, "execute", slow_execute)

    eng, bot = engine()
    o = await arm()
    snap(600_000.0, clock.t - 1)
    run_task = asyncio.create_task(eng.run())
    await asyncio.sleep(0.03)
    clock.advance(10)
    snap(600_000.0, clock.t - 1)
    await asyncio.wait_for(started.wait(), 2)
    assert disk_status(o.id) == "firing"

    stop_task = asyncio.create_task(eng.stop(grace=5))
    await asyncio.sleep(0.05)
    assert run_task.cancelled() or run_task.done()
    assert not stop_task.done()                       # still waiting for the fire
    release.set()
    await asyncio.wait_for(stop_task, 2)
    assert storage.auto_orders == [] and len(world.executed) == 1 and eng._tasks == set()
    assert "✅ Sold" in bot.channel.texts[-1]


@pytest.mark.asyncio
async def test_stop_cancels_fires_that_outlive_the_grace(world, clock, monkeypatch):
    async def forever(*a, **k):
        await asyncio.Event().wait()
    monkeypatch.setattr(swap, "execute", forever)
    eng, bot = engine()
    o = await arm()
    snap(600_000.0, clock.t - 1)
    await eng.tick()
    clock.advance(10)
    snap(600_000.0, clock.t - 1)
    await eng.tick()
    await asyncio.sleep(0)
    assert eng._tasks
    await asyncio.wait_for(eng.stop(grace=0.05), 2)
    assert eng._tasks == set()
    assert disk_status(o.id) == "firing"              # the restart path retires it next boot


# ---------------- item 7: min_expiry_for, trailing stops, rule_value, floors, target snapshot ----------------

def snap_full(mc, ts, **fields):
    token_cache[CA] = TokenSnapshot(mc=mc, url="", updated_ts=ts, **fields)


def trailing(**overrides):
    base = dict(size=100.0, direction="below", target=400_000.0, spec="trail 20%", anchor="now",
                anchor_mc=500_000.0, trail_pct=20.0, high_mc=500_000.0)
    base.update(overrides)
    return base


async def sample(eng, clock, mc, **fields):
    """One distinct fresh cache sample, then a tick."""
    clock.advance(10)
    snap_full(mc, clock.t - 1, **fields)
    await eng.tick()


def test_min_expiry_for_and_parse_expiry_minimum(clock):
    assert orders.min_expiry_for("now") == 300
    assert orders.min_expiry_for("mc") == orders.min_expiry_for("vol1h") == orders.min_expiry_for("move") == 3600
    assert orders.parse_expiry("10m", "7d", "30d", minimum=orders.min_expiry_for("now")) == T0 + 600
    assert orders.parse_expiry("5m", "7d", "30d", minimum=300) == T0 + 300
    with pytest.raises(ValueError, match="at least 5m"):
        orders.parse_expiry("4m", "7d", "30d", minimum=300)
    with pytest.raises(ValueError, match="at least 1h"):
        orders.parse_expiry("10m", "7d", "30d", minimum=None)
    with pytest.raises(ValueError, match="at least 1h"):
        orders.parse_expiry("10m", "7d", "30d", minimum=orders.min_expiry_for("mc"))


def test_sell_rule_parses_a_trailing_stop_from_the_market_cap_now():
    for at in ("trail 20%", "trail 20", "20% trailing", "TRAIL 20 %", "trailing 20%", "20%trail"):
        r = orders.sell_rule(at, 250_000.0, 500_000.0, "entry")      # the anchor choice is ignored: trailing starts now
        assert r == orders.Rule(metric="mc", direction="below", target=pytest.approx(400_000.0), spec="trail 20%",
                                anchor_mc=500_000.0, anchor="now", trail_pct=20.0), at
    r = orders.sell_rule("trail 12.5%", None, 200_000.0, "now")
    assert r.spec == "trail 12.5%" and r.trail_pct == 12.5 and r.target == pytest.approx(175_000.0)
    assert not orders.already_met(r.direction, 200_000.0, r.target)   # false by construction
    assert orders.sell_rule("trail 5%", None, 100.0, "now").trail_pct == 5.0
    assert orders.sell_rule("trail 60%", None, 100.0, "now").trail_pct == 60.0


def test_sell_rule_refuses_a_trailing_stop_out_of_range_or_without_a_price():
    for at in ("trail 0%", "trail 4.9%", "trail 90%", "trail 61%", "100% trailing"):
        with pytest.raises(ValueError, match="between 5% and 60%"):
            orders.sell_rule(at, 250_000.0, 500_000.0, "entry")
    with pytest.raises(ValueError, match="no market cap to start"):
        orders.sell_rule("trail 20%", 250_000.0, None, "entry")
    with pytest.raises(ValueError, match="no market cap to start"):
        orders.sell_rule("trail 20%", 250_000.0, 0.0, "now")
    with pytest.raises(ValueError, match="Could not read"):
        orders.sell_rule("trailer 20%", 250_000.0, 500_000.0, "entry")
    assert orders.Rule("mc", "above", 1.0).trail_pct == 0.0             # plain rules never trail


def test_rule_value_reads_each_metric_and_never_invents_a_number(clock):
    o = make_order()
    s = TokenSnapshot(mc=123.0, url="", updated_ts=T0, vol1h=456.0, liq_usd=20_000.0, buys_m5=7)
    info = {"mc": 321.0, "vol1h": 654.0, "liq": 15_000.0, "buys_m5": 2}
    assert orders.rule_value(o, snap=s) == 123.0
    assert orders.rule_value(o, info=info) == 321.0
    assert orders.rule_value(o, snap=s, info=info) == 123.0             # the snapshot wins when both are given
    assert orders.rule_value(o) is None
    o.metric = "vol1h"
    assert orders.rule_value(o, snap=s) == 456.0 and orders.rule_value(o, info=info) == 654.0
    assert orders.rule_value(o, snap=TokenSnapshot(mc=1.0, url="", updated_ts=T0)) is None
    o.metric = "fill"
    assert orders.rule_value(o, snap=s, info=info) is None
    o.metric = "sideways"
    assert orders.rule_value(o, snap=s, info=info) is None
    # "now" is 1.0 only with a market cap and passing floors.
    o = make_order(side="buy", metric="now", direction="above", target=0.0, size=5.0, min_liq=10_000.0, min_buyers=3)
    assert orders.rule_value(o, snap=s) == 1.0
    assert orders.rule_value(o, info=info) is None                      # buyers 2 < 3
    assert orders.rule_value(o, snap=TokenSnapshot(mc=None, url="", updated_ts=T0, liq_usd=99_999.0, buys_m5=9)) is None
    assert orders.rule_value(o) is None
    assert orders.rule_value(make_order(side="buy", metric="now", direction="above", target=0.0, size=5.0),
                             info={"mc": 5.0}) == 1.0


def test_rule_value_move_comes_from_the_history_series(clock, monkeypatch):
    from mccapbot import history
    monkeypatch.setattr(history, "_series", {})
    o = make_order(side="buy", metric="move", direction="above", target=25.0, size=10.0, window_sec=900)
    assert orders.rule_value(o, now=T0) is None                          # no history yet
    history.record(CA, 100_000.0, T0 - 900)
    history.record(CA, 130_000.0, T0)
    assert orders.rule_value(o, now=T0) == pytest.approx(30.0)
    assert orders.rule_value(o) == pytest.approx(30.0)                  # the engine clock when now is omitted
    assert orders.rule_value(make_order(side="buy", metric="move", direction="above", target=25.0, size=10.0), now=T0) is None


def test_floors_ok_treats_zero_as_no_floor_and_unknown_as_failing():
    plain = make_order(side="buy", direction="below", target=200_000.0, size=10.0)
    assert orders.floors_ok(plain) and orders.floors_ok(plain, snap=TokenSnapshot(mc=None, url="", updated_ts=T0))
    o = make_order(side="buy", direction="below", target=200_000.0, size=10.0, min_liq=10_000.0, min_buyers=3)
    assert orders.floors_ok(o, snap=TokenSnapshot(mc=1.0, url="", updated_ts=T0, liq_usd=10_000.0, buys_m5=3))
    assert not orders.floors_ok(o, snap=TokenSnapshot(mc=1.0, url="", updated_ts=T0, liq_usd=9_999.0, buys_m5=3))
    assert not orders.floors_ok(o, snap=TokenSnapshot(mc=1.0, url="", updated_ts=T0, liq_usd=10_000.0, buys_m5=2))
    assert not orders.floors_ok(o, snap=TokenSnapshot(mc=1.0, url="", updated_ts=T0, liq_usd=None, buys_m5=9))
    assert not orders.floors_ok(o)
    assert orders.floors_ok(o, info={"liq": 12_000.0, "buys_m5": 4})
    assert not orders.floors_ok(o, info={"liq": 12_000.0})
    assert not orders.floors_ok(o, info={"buys_m5": 4})
    assert orders.floors_ok(make_order(side="buy", direction="below", target=1.0, size=1.0, min_liq=10_000.0),
                            info={"liq": 10_000.0})


@pytest.mark.asyncio
async def test_floors_gate_a_buy_in_the_tick_and_again_on_the_fresh_read(world, clock):
    eng, bot = engine()
    world.mc_now = 150_000.0
    o = await arm(side="buy", direction="below", target=200_000.0, size=10.0, min_liq=10_000.0, min_buyers=3)
    await sample(eng, clock, 150_000.0, liq_usd=5_000.0, buys_m5=9)
    await sample(eng, clock, 150_000.0, liq_usd=50_000.0, buys_m5=1)
    await sample(eng, clock, 150_000.0, liq_usd=None, buys_m5=9)
    await eng.drain()
    assert eng._hits[o.id][0] == 0 and world.executed == []
    # The cache passes but DexScreener's fresh payload is under the floor: a hold, not a fill.
    world.liquidity = 5_000.0
    await sample(eng, clock, 150_000.0, liq_usd=50_000.0, buys_m5=9)
    await sample(eng, clock, 150_000.0, liq_usd=50_000.0, buys_m5=9)
    await eng.drain()
    assert world.executed == [] and o.status == "armed" and o.attempts == 0 and world.route_calls == 0
    assert "fell under the rule's floor" in eng.held_for(o.id)[0]
    # Floors pass everywhere: it fills. (World summaries carry no buys_m5; a floor of 0 on buyers is no floor.)
    o.min_buyers = 0
    world.liquidity = 50_000.0
    await sample(eng, clock, 150_000.0, liq_usd=50_000.0, buys_m5=9)
    await sample(eng, clock, 150_000.0, liq_usd=50_000.0, buys_m5=9)
    await eng.drain()
    assert len(world.executed) == 1 and storage.auto_orders == []
    # Sells never carry floors, and a floor on a sell record is ignored.
    s = await arm(min_liq=999_999_999.0)
    world.mc_now = 600_000.0
    await trigger(eng, clock)
    assert len(world.executed) == 2 and s not in storage.auto_orders


@pytest.mark.asyncio
async def test_two_rising_samples_raise_the_high_and_the_target_one_spike_never_does(world, clock):
    eng, bot = engine()
    o = await arm(**trailing())
    await sample(eng, clock, 900_000.0)                 # one print above the high: a candidate only
    assert (o.high_mc, o.target) == (500_000.0, 400_000.0) and eng._trail_cand[o.id] == 900_000.0
    await sample(eng, clock, 450_000.0)                 # back under: the spike is forgotten
    assert (o.high_mc, o.target) == (500_000.0, 400_000.0) and o.id not in eng._trail_cand
    await sample(eng, clock, 600_000.0)
    await sample(eng, clock, 650_000.0)                 # two agreeing samples: the high is the lower of the two
    assert o.high_mc == 600_000.0 and o.target == pytest.approx(480_000.0)
    await sample(eng, clock, 700_000.0)                 # and it keeps following, one sample behind
    assert o.high_mc == 650_000.0 and o.target == pytest.approx(520_000.0)
    assert world.executed == [] and o.status == "armed"
    assert orders.describe_rule(o) == "sell 100% PONS when MC ≤ $520K (trail 20% below its $650K high)"
    # The same sample seen again, or a stale one, never moves anything.
    await eng.tick()
    snap_full(900_000.0, clock.t - orders.STALE_SECONDS - 1)
    await eng.tick()
    assert o.high_mc == 650_000.0 and eng._trail_cand[o.id] == 700_000.0


@pytest.mark.asyncio
async def test_a_lower_sample_never_lowers_the_high_or_the_target(world, clock):
    eng, bot = engine()
    o = await arm(**trailing())
    await sample(eng, clock, 650_000.0)
    await sample(eng, clock, 640_000.0)
    assert o.high_mc == 640_000.0 and o.target == pytest.approx(512_000.0)
    for mc in (620_000.0, 630_000.0, 635_000.0, 520_000.0, 515_000.0):
        await sample(eng, clock, mc)
        assert o.high_mc == 640_000.0 and o.target == pytest.approx(512_000.0), mc
    assert world.executed == [] and o.status == "armed"
    await sample(eng, clock, 641_000.0)
    await sample(eng, clock, 641_000.0)                 # equal samples above the high agree too
    assert o.high_mc == 641_000.0 and o.target == pytest.approx(512_800.0)


@pytest.mark.asyncio
async def test_a_drop_through_the_ratcheted_target_fires_against_the_target_the_tick_saw(world, clock, monkeypatch):
    seen = []
    real_fresh = orders.Engine._fresh

    async def spy(self, o, target_seen=None):
        seen.append((target_seen, o.target))
        return await real_fresh(self, o, target_seen)
    monkeypatch.setattr(orders.Engine, "_fresh", spy)
    eng, bot = engine()
    world.sell_route.amount_in = 36 * 10**18
    o = await arm(**trailing())
    await sample(eng, clock, 600_000.0)
    await sample(eng, clock, 650_000.0)
    assert o.target == pytest.approx(480_000.0)
    world.mc_now = 470_000.0                            # DexScreener agrees with the cache at fire time
    await sample(eng, clock, 470_000.0)                 # under the ratcheted stop, above the original 400K
    await sample(eng, clock, 470_000.0)
    await eng.drain()
    assert len(world.executed) == 1 and storage.auto_orders == [] and on_disk() == []
    assert seen == [(pytest.approx(480_000.0), pytest.approx(480_000.0))]
    text = bot.channel.texts[-1]
    assert f"🤖 `{o.id}` fired" in text and "✅ Sold" in text
    assert "when MC ≤ $480K (trail 20% below its $600K high)" in text
    assert world.last_extra["rule"] == "sell 100% PONS when MC ≤ $480K (trail 20% below its $600K high)"
    assert o.id not in eng._trail_cand and o.id not in eng._trail_saved


@pytest.mark.asyncio
async def test_a_target_that_moved_under_a_fire_in_flight_is_a_hold(world, clock, monkeypatch):
    eng, bot = engine()
    o = await arm(**trailing())
    with pytest.raises(orders._Hold, match="the level moved to \\$480K while this fire was in flight"):
        o.target = 480_000.0
        await eng._fresh(o, target_seen=400_000.0)
    assert world.summary_calls == 0                     # decided before any request
    o.target = 400_000.0
    # End to end: the ratchet lands between the spawn and the fresh read.
    real_preflight = orders.Engine._preflight

    def preflight_then_ratchet(self, order):
        w = real_preflight(self, order)
        order.target = 480_000.0                        # what a concurrent tick's ratchet would do
        return w
    monkeypatch.setattr(orders.Engine, "_preflight", preflight_then_ratchet)
    world.mc_now = 390_000.0
    await sample(eng, clock, 390_000.0)
    await sample(eng, clock, 390_000.0)
    await eng.drain()
    assert world.executed == [] and world.route_calls == 0 and o.status == "armed" and o.attempts == 0
    assert o in storage.auto_orders and o.id not in eng._hits and bot.channel.sent == []
    assert eng.held_for(o.id)[0] == "the level moved to $480K while this fire was in flight"
    # A plain rule passes its own target through unchanged and fires as before.
    monkeypatch.setattr(orders.Engine, "_preflight", real_preflight)
    storage.auto_orders.clear()
    world.mc_now = 600_000.0
    p = await arm()
    await trigger(eng, clock)
    assert len(world.executed) == 1 and p not in storage.auto_orders


@pytest.mark.asyncio
async def test_fresh_read_hold_texts_follow_the_metric(world, clock):
    eng, bot = engine()
    o = make_order()
    world.mc_now = 100_000.0
    with pytest.raises(orders._Hold, match=r"the fresh read \(\$100K\) no longer met the level"):
        await eng._fresh(o)
    o = make_order(side="buy", metric="vol1h", direction="above", target=50_000.0, size=10.0)
    world.vol1h = 100.0
    with pytest.raises(orders._Hold, match=r"the fresh read \(\$100.00\) no longer met the level"):
        await eng._fresh(o)
    o = make_order(side="buy", metric="move", direction="above", target=25.0, size=10.0, window_sec=900)
    with pytest.raises(orders._Hold, match=r"the fresh read \(—\) no longer met the move"):
        await eng._fresh(o)


@pytest.mark.asyncio
async def test_ratchet_saves_once_per_tick_on_a_one_percent_rise_throttled_to_thirty_seconds(world, clock, monkeypatch):
    saves = []
    real_save = storage.save_orders

    async def save():
        saves.append(clock.t)
        await real_save()
    monkeypatch.setattr(storage, "save_orders", save)
    eng, bot = engine()
    o = await arm(**trailing())
    saves.clear()

    def disk_target():
        return {d["id"]: d["target"] for d in on_disk()}[o.id]

    await sample(eng, clock, 600_000.0)
    assert saves == [] and disk_target() == 400_000.0
    await sample(eng, clock, 600_000.0)                 # first ratchet: 400K -> 480K, written at once
    assert saves == [clock.t] and disk_target() == pytest.approx(480_000.0)
    await sample(eng, clock, 700_000.0)
    await sample(eng, clock, 700_000.0)                 # 480K -> 560K inside 30s: memory only
    assert o.target == pytest.approx(560_000.0) and len(saves) == 1 and disk_target() == pytest.approx(480_000.0)
    await sample(eng, clock, 700_000.0)                 # 30s later, no new rise: the throttled save lands
    assert len(saves) == 2 and disk_target() == pytest.approx(560_000.0)
    await sample(eng, clock, 703_000.0)
    await sample(eng, clock, 703_000.0)                 # 560K -> 562.4K is under 1%: not worth a write
    clock.advance(orders.TRAIL_SAVE_SECONDS)
    await sample(eng, clock, 703_000.0)
    assert o.target == pytest.approx(562_400.0) and len(saves) == 2
    await sample(eng, clock, 710_000.0)
    await sample(eng, clock, 710_000.0)                 # 568K is over 1% above the 560K on disk: written
    assert len(saves) == 3 and disk_target() == pytest.approx(568_000.0)
    assert {d["id"]: d["high_mc"] for d in on_disk()}[o.id] == 710_000.0


@pytest.mark.asyncio
async def test_cancel_while_trailing_stops_everything(world, clock):
    eng, bot = engine()
    o = await arm(**trailing())
    await sample(eng, clock, 600_000.0)
    await sample(eng, clock, 650_000.0)
    assert o.target == pytest.approx(480_000.0)
    storage.auto_orders.remove(o)                       # /rh auto cancel
    await storage.save_orders()
    world.mc_now = 470_000.0
    await sample(eng, clock, 470_000.0)
    await sample(eng, clock, 470_000.0)
    await sample(eng, clock, 900_000.0)
    await sample(eng, clock, 900_000.0)
    await eng.drain()
    assert world.executed == [] and on_disk() == [] and bot.channel.sent == []
    assert (o.high_mc, o.target) == (600_000.0, pytest.approx(480_000.0))   # nothing touched the dead record


@pytest.mark.asyncio
async def test_trailing_and_protection_fields_round_trip_and_old_files_load(world):
    o = make_order(**trailing(), expires_ts=T0 + 3600, then="tp=2x:50,sl=-30%:100", origin="manual")
    storage.auto_orders.append(o)
    await storage.save_orders()
    storage.auto_orders.clear()
    await storage.load_orders()
    back = storage.auto_orders[0]
    assert (back.trail_pct, back.high_mc, back.spec, back.then) == (20.0, 500_000.0, "trail 20%", "tp=2x:50,sl=-30%:100")
    assert orders.describe_rule(back) == orders.describe_rule(o)
    # A file written before trailing stops and protection existed.
    old = [{"ca": CA, "symbol": "PONS", "decimals": 18, "side": "sell", "metric": "mc", "direction": "above",
            "target": 500000.0, "size": 50.0, "slippage_bps": 200, "user_id": USER, "guild_id": 1, "channel_id": 99,
            "expires_ts": T0 + 3600, "spec": "2x", "anchor_mc": 250000.0, "anchor": "entry", "private": False,
            "id": "old001", "created_ts": T0, "status": "armed", "tx": "", "fired_ts": 0.0, "attempts": 0,
            "last_attempt_ts": 0.0, "last_error": ""}]
    with open(storage.RHC_ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(old, f)
    storage.auto_orders.clear()
    await storage.load_orders()
    back = storage.auto_orders[0]
    assert back.id == "old001" and back.trail_pct == 0.0 and back.high_mc == 0.0 and back.then == ""
    assert back.min_liq == 0.0 and back.min_buyers == 0 and back.window_sec == 0
    assert orders.describe_rule(back) == "sell 50% PONS when MC ≥ $500K (2x from your $250K entry)"
    assert orders.rule_value(back, info={"mc": 1.0}) == 1.0 and orders.floors_ok(back)


# ---------------- item 6 helpers: parse_then, room_for ----------------

def test_parse_then_reads_the_default_and_rejects_junk():
    assert orders.parse_then("tp=2x:50,sl=-30%:100") == [("2x", 50), ("-30%", 100)]
    assert orders.parse_then(" TP = 2x : 50 , sl=-30% ") == [("2x", 50), ("-30%", 100)]   # a missing percent sells all
    assert orders.parse_then("sl=-30%:100") == [("-30%", 100)]
    assert orders.parse_then("tp=+50%:25") == [("+50%", 25)]
    assert orders.parse_then("sl=trail 25%:100") == [("trail 25%", 100)]              # a trailing SL is a sell rule too
    assert orders.parse_then("") == orders.parse_then(None) == orders.parse_then("none") == orders.parse_then("NONE") == []
    for junk in ("junk", "tp=moon:50", "tp=2x:0", "tp=2x:101", "tp=2x:lots", "tp=-30%:50", "sl=2x:100",
                 "tp=2x:50,tp=3x:50", "x=2x:50", "tp=500k:50", "tp=2x:50,,sl=:100"):
        with pytest.raises(ValueError, match=r"Could not read|must sit|must be between|given twice|wrong way|relative"):
            orders.parse_then(junk)
    from mccapbot.config import RHC_AUTO_PROTECT_DEFAULT
    assert orders.parse_then(RHC_AUTO_PROTECT_DEFAULT) == [("2x", 50), ("-30%", 100)]


def test_room_for_uses_the_cog_limits_and_texts(world, monkeypatch):
    monkeypatch.setattr(orders, "RHC_AUTO_MAX_PER_USER", 3)
    monkeypatch.setattr(orders, "RHC_AUTO_MAX_TOTAL", 5)
    assert orders.room_for(USER, 1) == (True, "")
    assert orders.room_for(USER, 3) == (True, "")
    assert orders.room_for(USER, 0) == (True, "")
    storage.auto_orders.extend(make_order() for _ in range(3))
    ok, text = orders.room_for(USER, 1)
    assert not ok and text == ("You already have 3 auto-orders, the most one wallet can hold."
                               f"{orders.SEP}`/rh auto cancel` frees a slot.")
    assert orders.room_for(USER, 0) == (True, "")
    assert orders.room_for(8, 1) == (True, "")
    storage.auto_orders.pop()
    ok, text = orders.room_for(USER, 2)
    assert not ok and text.startswith("Only 1 slot left of the 3 auto-orders one wallet can hold; this needs 2.")
    assert orders.room_for(USER, 1) == (True, "")
    # The total wall counts everyone's rules.
    storage.auto_orders.extend(make_order(user_id=8) for _ in range(3))
    ok, text = orders.room_for(9, 1)
    assert not ok and text == ("McCap is watching 5 auto-orders already, the most it polls for at once. "
                               "Try again when one fills or expires.")
    storage.auto_orders.pop()
    ok, text = orders.room_for(9, 2)
    assert not ok and text.startswith("McCap has 1 slot left of the 5 auto-orders it polls for at once; this needs 2.")
    assert orders.room_for(9, 1) == (True, "")
    monkeypatch.setattr(orders, "RHC_AUTO_ENABLE", False)
    assert orders.room_for(9, 1) == (False, "🔒 Auto-orders are switched off (`RHC_AUTO_ENABLE=0`).")
    assert orders.room_for(9, 0)[0] is False


def test_the_cog_asks_room_for_rather_than_counting_slots_itself():
    """One place decides whether a rule fits, and it is this module: the cog's
    _auto_limits delegates, so a limit change cannot mean two different things."""
    import inspect
    from mccapbot.cogs import rhc as cog
    src = inspect.getsource(cog.RhcCog._auto_limits)
    assert "orders.room_for" in src
    assert "RHC_AUTO_MAX_PER_USER" not in src and "RHC_AUTO_MAX_TOTAL" not in src, "no second copy of the arithmetic"
    # ...and the refusals it sends are the ones room_for returns (texts asserted
    # in test_room_for_uses_the_cog_limits_and_texts).
    assert "self._send_private(inter, why)" in src
