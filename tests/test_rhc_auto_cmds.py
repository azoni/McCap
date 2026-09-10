"""/rh auto sell|buy|list|cancel, the TP / SL and Other-amount modal handlers, and the
tutorial command, driven through the cog with fakes. Nothing signs or sends."""

import json
import os

import pytest
from discord import app_commands

from mccapbot import storage
from mccapbot.cogs import rhc as cog
from mccapbot.rhc import chain, kyber, ledger, orders, swap
from tests.rhc_fakes import PONS, USER, Declined, Expired, FakeInteraction, arm_world, disarm_world


def choice(value):
    return app_commands.Choice(name=value, value=value)


@pytest.fixture
def world(monkeypatch):
    w = arm_world(monkeypatch)
    storage.auto_orders.clear()
    try:
        os.remove(storage.RHC_ORDERS_FILE)
    except FileNotFoundError:
        pass
    yield w
    storage.auto_orders.clear()
    disarm_world()


def journal_buy(usd_in=20.0, mc=50_000.0):
    ledger.journal({"ts": 1.0, "user_id": USER, "kind": "buy", "token": PONS, "symbol": "PONS", "decimals": 18,
                    "amount_in": "10000000000000000", "quoted_out": str(36 * 10**18), "actual_out_estimate": str(36 * 10**18),
                    "usd_in": usd_in, "mc_usd": mc, "tx": "0xb1", "status": "confirmed", "gas_cost_wei": "0"})


def on_disk():
    with open(storage.RHC_ORDERS_FILE, encoding="utf-8") as f:
        return json.load(f)


async def arm_sell(inter, percent=50, at="2x", anchor=None, expires=None, private=False):
    await cog.RhcCog.auto_sell.callback(cog.RhcCog(bot=None), inter, PONS, percent, at,
                                        anchor=choice(anchor) if anchor else None, expires=expires, private=private)


async def arm_buy(inter, usd=10.0, condition="mc_below", value="200k", expires=None):
    await cog.RhcCog.auto_buy.callback(cog.RhcCog(bot=None), inter, PONS, usd, choice(condition), value, expires=expires)


# ---------------- /rh auto sell ----------------


@pytest.mark.asyncio
async def test_auto_sell_anchors_on_the_entry_and_arms_one_rule(world):
    journal_buy()                      # 36 PONS for $20: $0.556 a token
    world.mc_now, world.price_now = 200_000.0, 0.7      # entry restated at today's supply = $159K
    inter = FakeInteraction()
    await arm_sell(inter)
    assert inter.response.deferred_ephemeral is True
    prompt = inter.texts[0]
    assert prompt.startswith("Arm **sell 50% of PONS** when MC ≥ **$317K**?"), prompt
    assert "2x from your $159K entry" in prompt and "now $200K" in prompt and "without asking again" in prompt
    assert PONS in prompt.lower()
    assert len(storage.auto_orders) == 1
    o = storage.auto_orders[0]
    assert o.side == "sell" and o.direction == "above" and o.target == pytest.approx(317_460, rel=0.001)
    assert o.spec == "2x" and o.anchor == "entry" and o.size == 50.0 and o.ca == chain.to_checksum(PONS)
    assert o.user_id == USER and o.channel_id == 99 and o.status == "armed" and o.expires_ts > 0
    assert [d["id"] for d in on_disk()] == [o.id], "armed rules are on disk before they are announced"
    assert inter.last.startswith(f"🤖 Armed `{o.id}`") and "expires <t:" in inter.last
    assert world.executed == [], "arming moves no money"


@pytest.mark.asyncio
async def test_auto_sell_without_an_entry_anchors_on_now_and_says_so(world):
    world.mc_now = 200_000.0
    inter = FakeInteraction()
    await arm_sell(inter, at="-30%")
    o = storage.auto_orders[0]
    assert o.anchor == "now" and o.direction == "below" and o.target == pytest.approx(140_000.0)
    assert "McCap has no entry for you" in inter.texts[0]

    forced = FakeInteraction()
    journal_buy()
    await arm_sell(forced, at="0.5x", anchor="now")
    assert storage.auto_orders[-1].anchor == "now" and "no entry" not in forced.texts[0]


@pytest.mark.asyncio
async def test_auto_sell_refusals_arm_nothing(world, monkeypatch):
    world.mc_now = 200_000.0
    below = FakeInteraction()
    await arm_sell(below, at="100k")         # an absolute level under the price is a stop: direction below, not met
    assert storage.auto_orders[-1].direction == "below"
    above = FakeInteraction()
    await arm_sell(above, at="300k")         # over the price: a take-profit, direction above, not met
    assert storage.auto_orders[-1].direction == "above" and len(storage.auto_orders) == 2
    storage.auto_orders.clear()

    journal_buy()                            # entry restated at today's supply = $159K, price already 26% above it
    world.price_now = 0.7
    past = FakeInteraction()
    await arm_sell(past, at="+10%")          # +10% from the entry is $175K; the market cap is $200K: already met
    assert storage.auto_orders == [] and past.last.startswith("🚫") and "/rh sell" in past.last

    world.token_balance = 0
    empty = FakeInteraction()
    await arm_sell(empty)
    assert "hold no PONS" in empty.last and storage.auto_orders == []
    world.token_balance = 36 * 10**18

    bad = FakeInteraction()
    await arm_sell(bad, at="sideways")
    assert bad.last.startswith("❌") and storage.auto_orders == []

    short = FakeInteraction()
    await arm_sell(short, expires="10m")
    assert short.last.startswith("❌") and storage.auto_orders == []

    monkeypatch.setattr(cog, "ConfirmOrder", Declined)
    no = FakeInteraction()
    await arm_sell(no)
    assert "Cancelled, nothing was armed" in no.last and storage.auto_orders == []
    monkeypatch.setattr(cog, "ConfirmOrder", Expired)
    late = FakeInteraction()
    await arm_sell(late)
    assert late.last.startswith("⏲️") and storage.auto_orders == []


@pytest.mark.asyncio
async def test_auto_gates_and_limits_answer_before_the_defer(world, monkeypatch):
    stranger = FakeInteraction(user_id=99)
    await arm_sell(stranger)
    assert stranger.last.startswith("🔒") and stranger.last_kw.get("via") == "response"

    monkeypatch.setattr(orders, "RHC_AUTO_ENABLE", False)
    off = FakeInteraction()
    await arm_sell(off)
    assert "RHC_AUTO_ENABLE" in off.last and off.response.deferred_ephemeral is None
    monkeypatch.setattr(orders, "RHC_AUTO_ENABLE", True)

    monkeypatch.setattr(orders, "RHC_AUTO_MAX_PER_USER", 1)   # the slot arithmetic lives in orders.room_for
    first = FakeInteraction()
    await arm_sell(first)
    assert len(storage.auto_orders) == 1
    second = FakeInteraction()
    await arm_sell(second)
    assert len(storage.auto_orders) == 1 and "auto-order" in second.last and second.response.deferred_ephemeral is None


# ---------------- /rh auto buy ----------------


@pytest.mark.asyncio
async def test_auto_buy_runs_the_sell_back_check_and_arms(world):
    inter = FakeInteraction()
    await arm_buy(inter)
    prompt = inter.texts[0]
    assert prompt.startswith("Arm **buy $10.00 of PONS** when MC ≤ **$200K**?"), prompt
    assert "sells straight back" in prompt and "without asking again" in prompt
    o = storage.auto_orders[0]
    assert o.side == "buy" and o.metric == "mc" and o.direction == "below" and o.target == 200_000.0 and o.size == 10.0
    assert world.executed == [] and ledger.spent_today(USER) == 0.0, "no reservation until it fires"

    vol = FakeInteraction()
    await arm_buy(vol, condition="vol1h_above", value="50k")
    assert storage.auto_orders[-1].metric == "vol1h" and storage.auto_orders[-1].direction == "above"
    assert "1h volume ≥ **$50K**" in vol.texts[0]


@pytest.mark.asyncio
async def test_auto_buy_refusals(world, monkeypatch):
    big = FakeInteraction()
    await arm_buy(big, usd=500.0)
    assert big.last.startswith("🚫") and storage.auto_orders == []

    world.back_error = kyber.NoRoute("no way back")
    trap = FakeInteraction()
    await arm_buy(trap)
    assert "honeypot" in trap.last.lower() and storage.auto_orders == []
    world.back_error = None

    met = FakeInteraction()
    await arm_buy(met, condition="mc_above", value="100k")      # 500M now: already above
    assert "/rh buy" in met.last and storage.auto_orders == []

    met_vol = FakeInteraction()
    await arm_buy(met_vol, condition="vol1h_above", value="10k")   # 12K now
    assert "/rh buy" in met_vol.last and storage.auto_orders == []

    capped = FakeInteraction()
    ledger.record(USER, 195.0)
    await arm_buy(capped)
    assert capped.last.startswith("🚫") and storage.auto_orders == []


# ---------------- list and cancel ----------------


@pytest.mark.asyncio
async def test_auto_list_and_cancel(world, monkeypatch):
    monkeypatch.setattr(cog, "PRIVATE", False)     # results public, so public:True can actually flip the list
    empty = FakeInteraction()
    await cog.RhcCog.auto_list.callback(cog.RhcCog(bot=None), empty)
    assert "no auto-orders" in empty.last and empty.last_kw.get("ephemeral") is True

    await arm_sell(FakeInteraction())
    o = storage.auto_orders[0]
    mine = FakeInteraction()
    await cog.RhcCog.auto_list.callback(cog.RhcCog(bot=None), mine)
    embed = mine.followup.sent[-1][1]["embed"]
    assert o.id in embed.description and "sell 50%" in embed.description and "PONS" in embed.description
    assert mine.response.deferred_ephemeral is True
    shared = FakeInteraction()
    await cog.RhcCog.auto_list.callback(cog.RhcCog(bot=None), shared, public=True)
    assert shared.response.deferred_ephemeral is False

    other = FakeInteraction(user_id=8)
    await cog.RhcCog.auto_cancel.callback(cog.RhcCog(bot=None), other, o.id)
    assert "No auto-order" in other.last and len(storage.auto_orders) == 1

    o.status = "firing"
    busy = FakeInteraction()
    await cog.RhcCog.auto_cancel.callback(cog.RhcCog(bot=None), busy, o.id)
    assert "executing right now" in busy.last and len(storage.auto_orders) == 1
    o.status = "pending"
    await cog.RhcCog.auto_cancel.callback(cog.RhcCog(bot=None), busy, o.id)
    assert "already fired" in busy.last and len(storage.auto_orders) == 1
    o.status = "armed"

    gone = FakeInteraction()
    await cog.RhcCog.auto_cancel.callback(cog.RhcCog(bot=None), gone, o.id.upper())
    assert gone.last.startswith("Cancelled") and storage.auto_orders == [] and on_disk() == []

    c = cog.RhcCog(bot=None)
    await arm_sell(FakeInteraction())
    names = [ch.name for ch in await c._cancel_autocomplete(FakeInteraction(), "")]
    assert len(names) == 1 and storage.auto_orders[0].id in names[0]
    assert await c._cancel_autocomplete(FakeInteraction(user_id=8), "") == []


# ---------------- modal handlers ----------------


@pytest.mark.asyncio
async def test_tpsl_modal_arms_two_rules_behind_one_confirm(world, monkeypatch):
    journal_buy()
    world.mc_now, world.price_now = 200_000.0, 0.7
    inter = FakeInteraction()
    await inter.response.defer(ephemeral=True)
    await cog.RhcCog(bot=None).modal_tpsl(inter, USER, PONS, "2x", "50", "-30%", "100")
    prompts = [c for c in inter.texts if c.startswith("Arm")]
    assert len(prompts) == 1 and prompts[0].count("Arm **sell") == 2, "one Confirm for both legs"
    assert [(o.direction, o.size) for o in storage.auto_orders] == [("above", 50.0), ("below", 100.0)]
    assert all(o.anchor == "entry" for o in storage.auto_orders)
    storage.auto_orders.clear()

    one = FakeInteraction()
    await one.response.defer(ephemeral=True)
    await cog.RhcCog(bot=None).modal_tpsl(one, USER, PONS, "2x", "50", "", "100")
    assert len(storage.auto_orders) == 1
    storage.auto_orders.clear()

    blank = FakeInteraction()
    await blank.response.defer(ephemeral=True)
    await cog.RhcCog(bot=None).modal_tpsl(blank, USER, PONS, "", "", "", "")
    assert "Nothing to arm" in blank.last and storage.auto_orders == []

    wrong = FakeInteraction(user_id=8)
    await wrong.response.defer(ephemeral=True)
    await cog.RhcCog(bot=None).modal_tpsl(wrong, USER, PONS, "2x", "50", "", "")
    assert "isn't your position" in wrong.last and storage.auto_orders == []


@pytest.mark.asyncio
async def test_buy_modal_needs_exactly_one_amount_and_then_buys(world):
    both = FakeInteraction()
    await both.response.defer(ephemeral=True)
    await cog.RhcCog(bot=None).modal_buy(both, PONS, "5", "0.01")
    assert "either USD or ETH" in both.last and world.executed == []

    inter = FakeInteraction()
    await inter.response.defer(ephemeral=True)
    await cog.RhcCog(bot=None).modal_buy(inter, PONS, "$7.50", "")
    assert inter.last.startswith("✅ Bought") and world.last_extra["source"] == "button"

    junk = FakeInteraction()
    await junk.response.defer(ephemeral=True)
    await cog.RhcCog(bot=None).modal_buy(junk, PONS, "seven", "")
    assert junk.last.startswith("❌")


# ---------------- receipts carry the buttons; auto fills are marked ----------------


@pytest.mark.asyncio
async def test_receipts_carry_the_button_rows(world):
    inter = FakeInteraction()
    await cog.RhcCog.buy.callback(cog.RhcCog(bot=None), inter, PONS, eth="0.01")
    view = inter.last_kw.get("view")
    assert view is not None and len(view.children) == 4
    ids = [c.custom_id for c in view.children]
    assert ids[:3] == [f"rh:sell:{USER}:{chain.to_checksum(PONS)}:{p}" for p in (25, 50, 100)]
    assert ids[3] == f"rh:tpsl:{USER}:{chain.to_checksum(PONS)}"
    assert inter.texts[-2].startswith("💡 First buy"), "the tip comes once, right before the first receipt"

    half = FakeInteraction()
    world.result = swap.SwapResult(ok=True, tx="0xsell", amount_out=5 * 10**15)
    await cog.RhcCog.sell.callback(cog.RhcCog(bot=None), half, PONS, 50, None)
    assert half.last_kw.get("view") is not None and len(half.last_kw["view"].children) == 2
    world.sell_route = kyber.Route(token_in=PONS, token_out=chain.NATIVE, amount_in=36 * 10**18, amount_out=10**16,
                                   amount_in_usd=24.7, amount_out_usd=24.5, gas=1, gas_usd=0.4, router=kyber.RHC_KYBER_ROUTER, summary={})
    all_of_it = FakeInteraction()
    await cog.RhcCog.sell.callback(cog.RhcCog(bot=None), all_of_it, PONS, 100, None)
    assert all_of_it.last.startswith("✅") and "view" not in all_of_it.last_kw, "nothing left to act on"


@pytest.mark.asyncio
async def test_history_marks_auto_fills(world):
    ledger.journal({"ts": 1.0, "user_id": USER, "kind": "buy", "token": PONS, "symbol": "PONS", "decimals": 18,
                    "amount_in": "10000000000000000", "quoted_out": str(36 * 10**18), "actual_out_estimate": str(36 * 10**18),
                    "usd_in": 20.0, "tx": "0xb1", "status": "confirmed", "gas_cost_wei": "0", "source": "auto"})
    inter = FakeInteraction()
    await cog.RhcCog.history.callback(cog.RhcCog(bot=None), inter, 10)
    assert inter.followup.sent[-1][1]["embed"].description.startswith("🤖 ✅")


# ---------------- tutorial and nav buttons ----------------


@pytest.mark.asyncio
async def test_tutorial_command_defers_then_answers_privately(world):
    inter = FakeInteraction()
    await cog.RhcCog.tutorial.callback(cog.RhcCog(bot=None), inter)
    assert inter.response.deferred_ephemeral is True
    kw = inter.last_kw
    assert kw["embed"].title == "How McCap trading works" and kw.get("ephemeral") is True
    assert kw.get("view") is not None, "a trader with a wallet gets My wallet / Trending now"

    shared = FakeInteraction()
    await cog.RhcCog.tutorial.callback(cog.RhcCog(bot=None), shared, topic=choice("auto"), public=True)
    assert shared.response.deferred_ephemeral is False and shared.last_kw.get("ephemeral") is False

    stranger = FakeInteraction(user_id=99)
    await cog.RhcCog.tutorial.callback(cog.RhcCog(bot=None), stranger)
    assert stranger.last_kw["embed"] is not None and "view" not in stranger.last_kw


@pytest.mark.asyncio
async def test_no_wallet_nudge_has_buttons_only_for_traders(world):
    from mccapbot.rhc import wallets
    wallets.wallets.clear()
    trader = FakeInteraction()
    await cog.RhcCog.buy.callback(cog.RhcCog(bot=None), trader, PONS, eth="0.01")
    assert "no wallet yet" in trader.last and trader.last_kw.get("view") is not None
    assert trader.last_kw.get("via") == "response", "answered before any defer"
    stranger = FakeInteraction(user_id=99)
    await cog.RhcCog.holdings.callback(cog.RhcCog(bot=None), stranger)
    assert "no wallet yet" in stranger.last and "view" not in stranger.last_kw
