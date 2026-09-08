"""The shared money path: every door (slash, button, auto-order) gets the same guards.

plan_* moves nothing and refuses in a fixed order; settle_* reserves before it
sends and refunds in exactly one place. Nothing here signs or broadcasts.
"""

import os
import pathlib
import re

import pytest

from mccapbot.rhc import chain, guard, kyber, ledger, swap, trade
from tests.rhc_fakes import PONS, USER, WALLET, World, arm_world, disarm_world


@pytest.fixture
def world(monkeypatch):
    w = arm_world(monkeypatch)
    yield w
    disarm_world()


async def plan(world, amount=10**16, bps=200):
    return await trade.plan_buy(USER, WALLET, chain.to_checksum(PONS), "PONS", 18, amount, bps, world.eth_usd)


# ---------------- plan_buy: refusal order and retry flags ----------------


@pytest.mark.asyncio
async def test_plan_buy_returns_a_plan_and_moves_nothing(world):
    p = await plan(world)
    assert p.usd == pytest.approx(25.0), "cap basis is the larger of Kyber's $24.80 and 0.01 ETH × $2500"
    assert p.back is not None and p.liq == 500_000.0 and not p.thin_pool
    assert world.executed == [] and ledger.spent_today(USER) == 0.0
    assert p.tokens_out == 36.0


@pytest.mark.asyncio
async def test_plan_buy_no_route_is_definite_and_kyber_down_is_retryable(world, monkeypatch):
    async def no_route(*a):
        raise kyber.NoRoute("route not found")
    monkeypatch.setattr(kyber, "route", no_route)
    with pytest.raises(trade.Refusal) as e:
        await plan(world)
    assert not e.value.retry and e.value.icon == "❌"

    async def down(*a):
        raise kyber.KyberUnavailable("429")
    monkeypatch.setattr(kyber, "route", down)
    with pytest.raises(trade.Refusal) as e:
        await plan(world)
    assert e.value.retry


@pytest.mark.asyncio
async def test_plan_buy_unpriceable_is_retryable_but_the_caps_are_definite(world, monkeypatch):
    world.buy_route.amount_in_usd = 0.0
    world.eth_usd = None
    with pytest.raises(trade.Refusal) as e:
        await trade.plan_buy(USER, WALLET, PONS, "PONS", 18, 10**16, 200, None)
    assert e.value.retry and "Could not price" in e.value.text

    world.buy_route.amount_in_usd = 24.8
    world.eth_usd = 2500.0
    monkeypatch.setattr(ledger, "RHC_MAX_TRADE_USD", 10.0)
    with pytest.raises(trade.Refusal) as e:
        await plan(world)
    assert not e.value.retry and "per-trade cap" in e.value.text


@pytest.mark.asyncio
async def test_plan_buy_gas_headroom_names_both_figures(world):
    world.eth_balance = 10**16
    with pytest.raises(trade.Refusal) as e:
        await plan(world)
    assert not e.value.retry
    assert "you have 0.01 ETH" in e.value.text and "including gas" in e.value.text


@pytest.mark.asyncio
async def test_plan_buy_honeypot_is_definite_and_outage_is_not(world):
    world.back_error = kyber.NoRoute("no way back")
    with pytest.raises(trade.Refusal) as e:
        await plan(world)
    assert not e.value.retry and "honeypot" in e.value.text.lower()

    world.back_error = kyber.KyberUnavailable("429")
    with pytest.raises(trade.Refusal) as e:
        await plan(world)
    assert e.value.retry and "sell-back check" in e.value.text


@pytest.mark.asyncio
async def test_plan_buy_uses_a_given_summary_instead_of_fetching(world):
    before = world.summary_calls
    p = await trade.plan_buy(USER, WALLET, PONS, "PONS", 18, 10**16, 200, world.eth_usd, info={"liq": 1_000.0})
    assert world.summary_calls == before and p.liq == 1_000.0 and p.thin_pool


# ---------------- settle_buy: reserve, refund, floor, retry ----------------


@pytest.mark.asyncio
async def test_settle_buy_reserves_before_executing_and_keeps_it_on_success(world):
    p = await plan(world)
    seen = []

    async def execute(user_id, built, token, symbol, extra=None):
        seen.append(ledger.spent_today(USER))
        return world.result
    world_execute = swap.execute
    swap.execute = execute
    try:
        res, built, basis = await trade.settle_buy(USER, WALLET, p, confirmed_floor=None, extra={"source": "test"})
    finally:
        swap.execute = world_execute
    assert res.ok and basis == pytest.approx(25.0)
    assert seen == [pytest.approx(25.0)], "the reservation is on the books before the swap runs"
    assert ledger.spent_today(USER) == pytest.approx(25.0)


@pytest.mark.asyncio
async def test_settle_buy_refunds_on_definite_failure_and_on_exception_but_not_on_pending(world, monkeypatch):
    p = await plan(world)
    world.result = swap.SwapResult(ok=False, error="REFUSING TO SIGN")
    res, _b, _u = await trade.settle_buy(USER, WALLET, p, confirmed_floor=None, extra={})
    assert not res.ok and ledger.spent_today(USER) == 0.0

    world.result = swap.SwapResult(ok=False, tx="0xdef", pending=True, error="Submitted but unconfirmed.")
    res, _b, _u = await trade.settle_buy(USER, WALLET, p, confirmed_floor=None, extra={})
    assert res.pending and ledger.spent_today(USER) == pytest.approx(25.0), "a pending broadcast keeps its reservation"
    ledger.refund(USER, 25.0)

    async def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(kyber, "build", boom)
    with pytest.raises(RuntimeError):
        await trade.settle_buy(USER, WALLET, p, confirmed_floor=None, extra={})
    assert ledger.spent_today(USER) == 0.0, "an exception after the reservation refunds it"


@pytest.mark.asyncio
async def test_settle_buy_confirmed_floor_stops_a_worse_requote_and_none_skips_it(world):
    p = await plan(world)
    worse = kyber.Route(token_in=chain.NATIVE, token_out=PONS, amount_in=10**16, amount_out=30 * 10**18,
                        amount_in_usd=24.8, amount_out_usd=24.7, gas=1, gas_usd=0.49, router=kyber.RHC_KYBER_ROUTER, summary={})
    world.requotes = [worse]
    with pytest.raises(trade.Refusal) as e:
        await trade.settle_buy(USER, WALLET, p, confirmed_floor=kyber.min_out(36 * 10**18, 200), extra={})
    assert e.value.retry and "price moved" in e.value.text
    assert world.executed == [] and ledger.spent_today(USER) == 0.0

    world.requotes = [worse]
    res, built, _u = await trade.settle_buy(USER, WALLET, p, confirmed_floor=None, extra={})
    assert res.ok and built.min_out == kyber.min_out(30 * 10**18, 200), "an armed rule takes the fresh quote's own floor"


@pytest.mark.asyncio
async def test_settle_buy_retries_a_slippage_revert_once_and_tags_the_journal_extra(world):
    p = await plan(world)
    world.results = [swap.SwapResult(ok=False, error=guard.SLIPPAGE_TEXT),
                     swap.SwapResult(ok=True, tx="0xretry", amount_out=36 * 10**18)]
    res, _b, _u = await trade.settle_buy(USER, WALLET, p, confirmed_floor=None, extra={"source": "auto", "order_id": "abc"})
    assert res.ok and res.tx == "0xretry" and len(world.executed) == 2
    assert world.extras[-1]["source"] == "auto" and world.extras[-1]["order_id"] == "abc"

    world.results = [swap.SwapResult(ok=False, error=guard.SLIPPAGE_TEXT), swap.SwapResult(ok=False, error=guard.SLIPPAGE_TEXT)]
    res, _b, _u = await trade.settle_buy(USER, WALLET, p, confirmed_floor=None, extra={})
    assert not res.ok and len(world.executed) == 4, "exactly one retry"
    assert ledger.spent_today(USER) == pytest.approx(25.0), "only the successful buy is on the books"


# ---------------- sells ----------------


@pytest.mark.asyncio
async def test_plan_sell_clamps_the_percent_and_refuses_an_empty_holding(world):
    p = await trade.plan_sell(USER, WALLET, PONS, "PONS", 18, 250, 200)
    assert p.pct == 100 and p.amount == 36 * 10**18
    p = await trade.plan_sell(USER, WALLET, PONS, "PONS", 18, 0, 200)
    assert p.pct == 1
    world.token_balance = 0
    with pytest.raises(trade.Refusal) as e:
        await trade.plan_sell(USER, WALLET, PONS, "PONS", 18, 50, 200)
    assert "hold no PONS" in e.value.text and not e.value.retry


@pytest.mark.asyncio
async def test_settle_sell_never_touches_the_ledger_and_journals_the_entry_figures(world):
    ledger.journal({"ts": 1.0, "user_id": USER, "kind": "buy", "token": PONS, "symbol": "PONS", "decimals": 18,
                    "amount_in": "10000000000000000", "quoted_out": str(36 * 10**18), "actual_out_estimate": str(36 * 10**18),
                    "usd_in": 20.0, "mc_usd": 50_000.0, "tx": "0xb1", "status": "confirmed", "gas_cost_wei": "0"})
    world.mc_now = 200_000.0
    p = await trade.plan_sell(USER, WALLET, PONS, "PONS", 18, 50, 200)
    extra = trade.sell_extra(USER, PONS, 18, await trade.summary(PONS))
    assert extra["multiple"] == pytest.approx(1.26) and extra["entry_mc"] == pytest.approx(158_730, rel=0.001)
    world.result = swap.SwapResult(ok=True, tx="0xsell", amount_out=5 * 10**15)
    res, built = await trade.settle_sell(USER, WALLET, p, confirmed_floor=None, extra=extra)
    assert res.ok and not built.is_buy and world.last_extra["multiple"] == pytest.approx(1.26)
    assert ledger.spent_today(USER) == 0.0
    text = trade.sell_success(res, built, p, extra)
    assert text.startswith("Sold 18 PONS for 0.005 ETH ($24.70)") and "**1.26x** from your entry ($159K → $200K MC)" in text


@pytest.mark.asyncio
async def test_settle_sell_floor_and_no_route(world, monkeypatch):
    p = await trade.plan_sell(USER, WALLET, PONS, "PONS", 18, 50, 200)
    world.sell_route = kyber.Route(token_in=PONS, token_out=chain.NATIVE, amount_in=18 * 10**18, amount_out=4 * 10**15,
                                   amount_in_usd=24.7, amount_out_usd=20.0, gas=1, gas_usd=0.4, router=kyber.RHC_KYBER_ROUTER, summary={})
    with pytest.raises(trade.Refusal) as e:
        await trade.settle_sell(USER, WALLET, p, confirmed_floor=kyber.min_out(5 * 10**15, 200), extra={})
    assert e.value.retry and "Nothing was sold" in e.value.text and world.executed == []

    async def gone(*a):
        raise kyber.NoRoute("route not found")
    monkeypatch.setattr(kyber, "route", gone)
    with pytest.raises(trade.Refusal) as e:
        await trade.plan_sell(USER, WALLET, PONS, "PONS", 18, 50, 200)
    assert not e.value.retry


# ---------------- texts ----------------


@pytest.mark.asyncio
async def test_receipt_texts_read_the_same_whichever_door_opened_them(world):
    p = await plan(world)
    built = await kyber.build(p.rt, WALLET, 200)
    res = swap.SwapResult(ok=True, tx="0xabc", amount_out=36 * 10**18)
    assert trade.buy_success(res, built, p) == "Bought 36 PONS for 0.01 ETH ($24.80) · in at $492M MC"
    assert trade.describe(res, "x").startswith("✅ x\n[Transaction](")
    assert trade.describe(swap.SwapResult(ok=False, tx="0xd", pending=True, error="Submitted"), "x").startswith("⏳ Submitted")
    assert trade.describe(swap.SwapResult(ok=False, error="nope"), "x") == "❌ nope"
    quoted = swap.SwapResult(ok=True, tx="0xabc", amount_out=None)
    assert "(quoted)" in trade.buy_success(quoted, built, p)
    assert str(trade.Refusal("no", icon="❌")) == "❌ no" and str(trade.Refusal("no")) == "🚫 no"


def test_money_moves_only_through_the_trade_module():
    """swap.execute is called from rhc/trade.py and defined in rhc/swap.py; nowhere else."""
    root = pathlib.Path(__file__).resolve().parents[1] / "mccapbot"
    callers = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"\bswap\.execute\(", text) or (path.name == "swap.py" and "async def execute(" in text):
            callers.append(path.relative_to(root).as_posix())
    assert sorted(callers) == ["rhc/swap.py", "rhc/trade.py"], callers
