"""Regressions pinned by the adversarial review of the buttons / auto-orders change.

Each test here reproduces a confirmed defect and asserts the fix: a pending
token approval is not a fill, a slippage failure does not delete a stop-loss,
an error after the money moved is never reported as "nothing was sold", Kyber
build errors are classified, holds are visible, a cap-sized buy is shrunk to
the cap rather than dropped, and a couple of button hygiene points.
"""

import json
from types import SimpleNamespace

import pytest

from mccapbot import storage, views
from mccapbot.cogs import rhc as cog_module
from mccapbot.rhc import guard, kyber, ledger, orders, swap, trade
from tests.rhc_fakes import PONS, USER, WALLET, FakeInteraction, arm_world, disarm_world
from tests.test_rhc_buttons import FakeCog, build, click, have_wallet  # noqa: F401  (fixtures reused)
from tests.test_rhc_orders import CA, arm, clock, engine, on_disk, snap, trigger, world  # noqa: F401
from tests.test_rhc_swap import fc  # noqa: F401


# ---------------- #1 a pending approval is not a fill ----------------


@pytest.mark.asyncio
async def test_pending_approval_holds_the_rule_instead_of_consuming_it(world, clock):
    world.result = swap.SwapResult(ok=False, tx="0xapprove", pending=True, stage="approve",
                                   error="Approval submitted but unconfirmed. Check the explorer before retrying.")
    eng, bot = engine()
    world.mc_now = 150_000.0
    o = await arm(side="sell", direction="below", target=200_000.0, size=100.0)
    await trigger(eng, clock, mc=150_000.0)
    assert o in storage.auto_orders and o.status == "armed" and o.tx == ""
    assert {d["id"]: d["status"] for d in on_disk()}[o.id] == "armed"
    assert not any("fired" in t for t in bot.channel.texts), "nothing was sold, so nothing says it was"
    reason, _when = eng.held_for(o.id)
    assert "approval" in reason and eng._retry_after[o.id] > clock.t


def test_classify_treats_an_approval_leg_as_busy():
    assert orders.classify(swap.SwapResult(ok=False, tx="0xa", pending=True, stage="approve")) == "busy"
    assert orders.classify(swap.SwapResult(ok=False, tx="0xa", pending=True)) == "pending"
    assert orders.classify(swap.SwapResult(ok=False, error="Approval failed: nope", stage="approve")) == "failed"


@pytest.mark.asyncio
async def test_a_rule_left_pending_on_an_approval_is_re_armed_not_reported_as_filled(world, clock, monkeypatch):
    eng, bot = engine()
    o = await arm(side="sell", direction="below", target=200_000.0, size=100.0, status="pending", tx="0xapp")
    ledger.journal({"ts": 1.0, "user_id": USER, "kind": "approve", "token": PONS, "amount": "1", "tx": "0xapp",
                    "status": "submitted"})

    async def poll(uid, tx):
        return "confirmed"
    monkeypatch.setattr(swap, "poll_pending", poll)
    clock.advance(orders.PENDING_POLL_SECONDS + 1)
    await eng.tick()
    assert o in storage.auto_orders and o.status == "armed" and o.tx == ""
    text = bot.channel.texts[-1]
    assert "approval confirmed" in text and "armed again" in text and "✅ confirmed on chain" not in text


@pytest.mark.asyncio
async def test_tick_refreshes_a_wallets_pending_tx_so_the_block_lifts(world, clock, monkeypatch):
    eng, bot = engine()
    world.mc_now = 150_000.0
    o = await arm(side="sell", direction="below", target=200_000.0, size=50.0)
    swap._pending[USER] = ("0xapp", clock.t)
    refreshed = []

    async def refresh(uid):
        refreshed.append(uid)
        swap._pending.pop(uid, None)      # the chain answered: the approval landed
        return "confirmed"
    monkeypatch.setattr(swap, "refresh_pending", refresh)
    clock.advance(10)
    snap(150_000.0, clock.t - 1)
    await eng.tick()                      # refresh runs; the wallet was in flight, so no spawn yet
    assert refreshed == [USER] and not eng._firing
    clock.advance(10)
    snap(150_000.0, clock.t - 1)
    await eng.tick()
    await eng.drain()
    assert o not in storage.auto_orders, "with the block lifted the stop-loss fires"
    assert len(world.executed) == 1


def test_approve_results_carry_their_stage_and_pending_helpers(fc):
    import asyncio
    import time
    fc.receipt = None                                  # no receipt: the approval goes pending
    wallet_addr = "0x" + "a1" * 20
    from mccapbot.rhc import wallets
    wallets.wallets.clear()
    wallets._loaded = True
    fc.native[wallet_addr] = 10**18

    async def sign(user_id, tx):
        return "0xapprovetx"
    import mccapbot.rhc.swap as swap_mod
    orig = swap_mod._sign_and_send
    swap_mod._sign_and_send = sign
    try:
        res = asyncio.run(swap._approve(USER, PONS, wallet_addr, 10))
    finally:
        swap_mod._sign_and_send = orig
    assert res.pending and res.stage == "approve" and swap.pending_tx(USER) == "0xapprovetx"
    fc.receipts["0xapprovetx"] = {"status": 1, "gasUsed": 21_000, "effectiveGasPrice": 300_000_000}
    assert asyncio.run(swap.refresh_pending(USER)) == "confirmed" and swap.pending_tx(USER) is None
    assert swap.SwapResult(ok=True).stage == "swap"
    assert time.time() > 0


# ---------------- #2 slippage failures retry, they do not delete a stop-loss ----------------


@pytest.mark.asyncio
async def test_slippage_revert_twice_is_a_retry_not_a_retirement(world, clock):
    world.results = [swap.SwapResult(ok=False, error=guard.SLIPPAGE_TEXT), swap.SwapResult(ok=False, error=guard.SLIPPAGE_TEXT)]
    eng, bot = engine()
    world.mc_now = 150_000.0
    o = await arm(side="sell", direction="below", target=200_000.0, size=100.0)
    await trigger(eng, clock, mc=150_000.0)
    assert o in storage.auto_orders and o.status == "armed"
    assert o.attempts == 1 and "slippage" in o.last_error and eng._retry_after[o.id] > clock.t
    assert bot.channel.texts == [], "a retry is quiet; only giving up after MAX_ATTEMPTS is reported"
    assert len(world.executed) == 2, "trade.settle_sell still re-quotes once per attempt"


# ---------------- #3 an error after the money moved is reported as what it is ----------------


@pytest.mark.asyncio
async def test_error_after_settle_reports_the_transaction_never_nothing_was_sold(world, clock, monkeypatch):
    eng, bot = engine()
    world.mc_now = 150_000.0
    world.result = swap.SwapResult(ok=True, tx="0xsold", amount_out=5 * 10**15)

    async def broken_conclude(self, o, res, success, view):
        raise RuntimeError("disk full")
    monkeypatch.setattr(orders.Engine, "_conclude", broken_conclude)
    o = await arm(side="sell", direction="below", target=200_000.0, size=50.0)
    await trigger(eng, clock, mc=150_000.0)
    assert o not in storage.auto_orders
    text = bot.channel.texts[-1]
    assert "executed, but McCap could not finish recording it" in text and "0xsold" in text
    assert "nothing was sold" not in text and eng._settled == {}


@pytest.mark.asyncio
async def test_a_receipt_formatting_slip_still_reports_the_fill(world, clock, monkeypatch):
    eng, bot = engine()
    world.mc_now = 150_000.0
    world.result = swap.SwapResult(ok=True, tx="0xsold", amount_out=5 * 10**15)
    monkeypatch.setattr(trade, "sell_success", lambda *a, **k: 1 / 0)
    o = await arm(side="sell", direction="below", target=200_000.0, size=50.0)
    await trigger(eng, clock, mc=150_000.0)
    text = bot.channel.texts[-1]
    assert f"🤖 `{o.id}` fired" in text and "Sold PONS (details in /rh history)" in text and "0xsold" in text


# ---------------- #4 Kyber build errors are classified, not "internal error" ----------------


@pytest.mark.asyncio
async def test_kyber_build_error_is_a_retry_and_a_refusing_one_retires(world, clock, monkeypatch):
    async def stale(rt, sender, bps, recipient=None):
        raise kyber.KyberError("KyberSwap could not build the transaction (HTTP 400).")
    monkeypatch.setattr(kyber, "build", stale)
    eng, bot = engine()
    world.mc_now = 150_000.0
    o = await arm(side="sell", direction="below", target=200_000.0, size=100.0)
    await trigger(eng, clock, mc=150_000.0)
    assert o in storage.auto_orders and o.status == "armed" and o.attempts == 1 and "HTTP 400" in o.last_error

    async def refusing(rt, sender, bps, recipient=None):
        raise kyber.KyberError("KyberSwap returned malformed calldata. Refusing.")
    monkeypatch.setattr(kyber, "build", refusing)
    eng._retry_after.clear()
    await trigger(eng, clock, mc=150_000.0)
    assert o not in storage.auto_orders and "Refusing" in bot.channel.texts[-1] and "nothing was sold" in bot.channel.texts[-1]


@pytest.mark.asyncio
async def test_trade_build_wrapper_and_cap_refusal_carry_what_callers_need(monkeypatch):
    w = arm_world(monkeypatch)
    try:
        async def boom(rt, sender, bps, recipient=None):
            raise kyber.KyberError("That quote is stale; get a fresh one.")
        monkeypatch.setattr(kyber, "build", boom)
        with pytest.raises(trade.Refusal) as e:
            await trade._build(w.buy_route, WALLET, 200)
        assert e.value.retry and e.value.icon == "❌"

        async def refusing(rt, sender, bps, recipient=None):
            raise kyber.KyberError("KyberSwap returned router 0xbad, expected 0xgood. Refusing.")
        monkeypatch.setattr(kyber, "build", refusing)
        with pytest.raises(trade.Refusal) as e:
            await trade._build(w.buy_route, WALLET, 200)
        assert not e.value.retry

        monkeypatch.setattr(ledger, "RHC_MAX_TRADE_USD", 10.0)
        with pytest.raises(trade.Refusal) as e:
            await trade.plan_buy(USER, WALLET, PONS, "PONS", 18, 10**16, 200, w.eth_usd)
        assert e.value.usd == pytest.approx(25.0) and "per-trade cap" in e.value.text
    finally:
        disarm_world()


# ---------------- #7 a buy sized at the cap is shrunk to it at fire time ----------------


@pytest.mark.asyncio
async def test_cap_sized_buy_is_scaled_to_the_cap_instead_of_retired(world, clock, monkeypatch):
    async def kyber_values_eth_higher(token_in, token_out, amount_in):
        # DexScreener says $2500/ETH (world.eth_usd); Kyber prices the same ETH 0.4% higher.
        usd_in = amount_in / 1e18 * 2510.0
        # Tokens out follow the dollars in at DexScreener's $0.70, so the price-agreement check is not what stops it.
        return kyber.Route(token_in=token_in, token_out=token_out, amount_in=amount_in, amount_out=int(usd_in / 0.7 * 1e18),
                           amount_in_usd=usd_in, amount_out_usd=usd_in * 0.99, gas=1, gas_usd=0.49,
                           router=kyber.RHC_KYBER_ROUTER, summary={}, hops=["uniswap-v4"])
    monkeypatch.setattr(kyber, "route", kyber_values_eth_higher)
    monkeypatch.setattr(ledger, "RHC_MAX_TRADE_USD", 50.0)
    eng, bot = engine()
    world.mc_now = 150_000.0
    o = await arm(side="buy", direction="below", target=200_000.0, size=50.0)
    await trigger(eng, clock, mc=150_000.0)
    assert o not in storage.auto_orders and len(world.executed) == 1
    built = world.executed[0]
    assert built.amount_in < int(50.0 / 2500.0 * 1e18), "the ETH leg was shrunk"
    assert built.amount_in_usd <= 50.0
    text = bot.channel.texts[-1]
    assert "✅ Bought" in text and "sized to the $50.00 cap" in text


@pytest.mark.asyncio
async def test_creation_refuses_a_buy_kyber_already_prices_over_the_cap(monkeypatch):
    w = arm_world(monkeypatch)
    storage.auto_orders.clear()
    try:
        w.buy_route.amount_in_usd = 60.0
        inter = FakeInteraction()
        from discord import app_commands
        await cog_module.RhcCog.auto_buy.callback(cog_module.RhcCog(bot=None), inter, PONS, 50.0,
                                                  app_commands.Choice(name="mc_below", value="mc_below"), "100k")
        assert inter.last.startswith("🚫 KyberSwap values that at $60.00") and storage.auto_orders == []
    finally:
        storage.auto_orders.clear()
        disarm_world()


# ---------------- #8 holds are visible ----------------


@pytest.mark.asyncio
async def test_holds_are_remembered_and_shown_in_the_list(world, clock):
    eng, bot = engine()
    o = await arm(side="sell", direction="below", target=200_000.0, size=50.0)
    world.mc_now = 300_000.0                      # the cache says fire, the fresh read says no
    await trigger(eng, clock, mc=150_000.0)
    reason, when_ts = eng.held_for(o.id)
    assert "no longer met" in reason and when_ts == clock.t and o.status == "armed"

    c = cog_module.RhcCog(bot=SimpleNamespace(auto_orders=eng))
    inter = FakeInteraction()
    await cog_module.RhcCog.auto_list.callback(c, inter)
    desc = inter.followup.sent[-1][1]["embed"].description
    assert f"⏸ held <t:{int(clock.t)}:R>: {reason}" in desc

    world.mc_now = 150_000.0
    eng._retry_after.clear()
    await trigger(eng, clock, mc=150_000.0)
    assert eng.held_for(o.id) is None and o not in storage.auto_orders, "a fill clears the hold note"


# ---------------- #5, #6, #10 button hygiene ----------------


def test_button_ladder_never_emits_duplicate_custom_ids(monkeypatch):
    monkeypatch.setattr(views, "RHC_BUTTON_USD_SIZES", [5, 5.004, 5, 20, 20.0, 999])
    monkeypatch.setattr(views, "RHC_MAX_TRADE_USD", 50.0)
    assert views._ladder() == [5.0, 20.0]
    for factory, args in ((views.alert_row, (PONS,)), (views.size_card, (PONS,))):
        ids = [c.custom_id for c in factory(*args).children]
        assert len(ids) == len(set(ids)), ids


def test_modals_carry_a_timeout_so_dismissed_ones_are_evicted():
    assert views.BuyAmountModal(PONS).timeout == views.MODAL_TIMEOUT == 15 * 60
    assert views.TpSlModal(USER, PONS).timeout == views.MODAL_TIMEOUT


@pytest.mark.asyncio
async def test_tpsl_button_checks_the_auto_order_limits_before_opening_the_form(have_wallet):
    cog = FakeCog()
    asked = []

    async def limits(inter):
        asked.append(True)
        await inter.response.send_message("🔒 Auto-orders are switched off (`RHC_AUTO_ENABLE=0`).", ephemeral=True)
        return True
    cog._auto_limits = limits
    item = await build(views.TpSlButton, f"rh:tpsl:{USER}:{PONS}")
    inter = click(cog)
    await item.callback(inter)
    assert asked == [True] and inter.response.modal is None
    assert any("switched off" in (c or "") for c, _k in inter.followup.sent)

    async def fine(inter):
        return False
    cog._auto_limits = fine
    inter = click(cog)
    await item.callback(inter)
    assert isinstance(inter.response.modal, views.TpSlModal)


def test_orders_state_file_is_ignored_by_git():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
    assert "rhc_orders.json" in text.split()
