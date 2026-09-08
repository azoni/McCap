"""/rh buy, sell and withdraw flows with a fake interaction.

Money is reserved before a swap, refunded only on a definite failure, kept on a
pending broadcast, and a stale re-quote below the confirmed floor stops the
trade. The chain, Kyber and the executor are all faked; nothing is signed.
"""

import pytest

from mccapbot import rhchain
from mccapbot.cogs import rhc as cog
from mccapbot.rhc import chain, guard, kyber, ledger, swap, wallets
from tests.rhc_fakes import PONS, USER, FakeInteraction, arm_world, disarm_world, route

@pytest.fixture
def world(monkeypatch):
    w = arm_world(monkeypatch)
    yield w
    disarm_world()


async def run_buy(inter, eth="0.01", slippage=None, usd=None):
    await cog.RhcCog.buy.callback(cog.RhcCog(bot=None), inter, PONS, eth=eth, usd=usd, slippage_bps=slippage)


# ---------------- buy ----------------


@pytest.mark.asyncio
async def test_buy_reserves_the_cap_and_reports_success(world):
    inter = FakeInteraction()
    await run_buy(inter)
    assert inter.last.startswith("✅"), inter.texts
    # Cap basis is the larger of Kyber's $24.80 and 0.01 ETH × $2500 = $25.00.
    assert ledger.spent_today(USER) == pytest.approx(25.0)
    assert len(world.executed) == 1
    built = world.executed[0]
    assert built.min_out >= kyber.min_out(world.buy_route.amount_out, 200)
    prompt = inter.texts[0]
    assert prompt.startswith("Buy **36 PONS** for **0.01 ETH ($24.80)**?"), prompt
    assert "round trip" in prompt and "Slippage 2%" in prompt and "expires <t:" in prompt
    assert inter.last.startswith("✅ Bought 36 PONS for 0.01 ETH ($24.80)"), inter.last


@pytest.mark.asyncio
async def test_definite_failure_refunds_the_reservation(world):
    world.result = swap.SwapResult(ok=False, error="REFUSING TO SIGN: drainer")
    inter = FakeInteraction()
    await run_buy(inter)
    assert inter.last.startswith("❌")
    assert ledger.spent_today(USER) == 0.0


@pytest.mark.asyncio
async def test_pending_broadcast_keeps_the_reservation(world):
    world.result = swap.SwapResult(ok=False, tx="0xdef", pending=True, error="Submitted but unconfirmed.")
    inter = FakeInteraction()
    await run_buy(inter)
    assert inter.last.startswith("⏳") and "0xdef" in inter.last
    assert ledger.spent_today(USER) == pytest.approx(25.0)


@pytest.mark.asyncio
async def test_stale_requote_below_the_confirmed_floor_stops_the_trade(world):
    world.buy_route.fetched_ts -= 60          # went stale while the button sat there
    world.requotes = [route(chain.NATIVE, PONS, 10**16, 30 * 10**18)]   # price moved 17% against the user
    inter = FakeInteraction()
    await run_buy(inter)
    assert "price moved" in inter.last and "Nothing was bought" in inter.last
    assert world.executed == []
    assert ledger.spent_today(USER) == 0.0


@pytest.mark.asyncio
async def test_stale_requote_within_the_floor_proceeds_and_anchors_min_out(world):
    world.buy_route.fetched_ts -= 60
    world.requotes = [route(chain.NATIVE, PONS, 10**16, 35_900_000_000_000_000_000)]   # -0.3%, inside 2%
    inter = FakeInteraction()
    await run_buy(inter)
    assert inter.last.startswith("✅")
    assert len(world.executed) == 1
    assert world.executed[0].min_out == kyber.min_out(36 * 10**18, 200), "floor stays anchored to the confirmed quote"


@pytest.mark.asyncio
async def test_honeypot_refuses_but_kyber_outage_is_not_a_honeypot(world):
    world.back_error = kyber.NoRoute("route not found")
    inter = FakeInteraction()
    await run_buy(inter)
    assert "honeypot" in inter.last.lower() and world.executed == [] and ledger.spent_today(USER) == 0.0

    world.back_error = kyber.KyberUnavailable("429")
    inter = FakeInteraction()
    await run_buy(inter)
    assert "not answering" in inter.last and "honeypot" not in inter.last.lower()
    assert world.executed == []


@pytest.mark.asyncio
async def test_usd_cap_uses_the_larger_price_source(world, monkeypatch):
    monkeypatch.setattr(ledger, "RHC_MAX_TRADE_USD", 25.0)
    world.buy_route.amount_in_usd = 1.0          # a bogus Kyber figure must not shrink the trade under the cap
    world.eth_usd = 5000.0                       # 0.01 ETH = $50 by DexScreener
    inter = FakeInteraction()
    await run_buy(inter)
    assert inter.last.startswith("🚫") and "per-trade cap" in inter.last
    assert world.executed == []


@pytest.mark.asyncio
async def test_unpriceable_trade_is_refused(world):
    world.buy_route.amount_in_usd = 0.0
    world.eth_usd = None
    inter = FakeInteraction()
    await run_buy(inter)
    assert "Could not price" in inter.last and world.executed == []


@pytest.mark.asyncio
async def test_gates_block_strangers_and_disallowed_servers(world, monkeypatch):
    inter = FakeInteraction(user_id=99)
    await run_buy(inter)
    assert "allowlist" in inter.last and world.executed == []

    monkeypatch.setattr(cog, "RHC_GUILD_IDS", {123})
    inter = FakeInteraction(guild_id=None)   # a DM
    await run_buy(inter)
    assert "not enabled here" in inter.last and world.executed == []


@pytest.mark.asyncio
async def test_buy_needs_eth_for_the_trade_and_its_gas_before_the_prompt(world):
    world.eth_balance = 10**16            # exactly the trade size, nothing for gas
    inter = FakeInteraction()
    await run_buy(inter)
    assert inter.last.startswith("🚫") and "including gas" in inter.last
    assert world.executed == [] and ledger.spent_today(USER) == 0.0


@pytest.mark.asyncio
async def test_withdraw_says_the_most_that_can_be_sent(world, monkeypatch):
    async def code_size(addr):
        return 0
    monkeypatch.setattr(chain, "code_size", code_size)
    world.eth_balance = 10**16
    inter = FakeInteraction()
    await cog.RhcCog.wallet_withdraw.callback(cog.RhcCog(bot=None), inter, "0x000000000000000000000000000000000000dEaD", "0.01")
    assert inter.last.startswith("🚫") and "the most you can send is" in inter.last


@pytest.mark.asyncio
async def test_bad_amount_is_rejected_before_anything_else(world):
    inter = FakeInteraction()
    await run_buy(inter, eth="0,05")
    assert inter.last.startswith("❌") and "dot" in inter.last and world.executed == []


# ---------------- sell ----------------


@pytest.mark.asyncio
async def test_sell_half_of_a_holding(world):
    world.result = swap.SwapResult(ok=True, tx="0xsell", amount_out=5 * 10**15, gas_cost_wei=10**14)
    inter = FakeInteraction()
    await cog.RhcCog.sell.callback(cog.RhcCog(bot=None), inter, PONS, 50, None)
    assert inter.last.startswith("✅") and "Sold 18 PONS" in inter.last
    assert len(world.executed) == 1 and world.executed[0].amount_in == 18 * 10**18
    assert not world.executed[0].is_buy
    assert PONS in inter.texts[0].lower(), "the confirm text shows the contract address"
    assert ledger.spent_today(USER) == 0.0, "exits are never capped"


@pytest.mark.asyncio
async def test_sell_with_no_holding_says_so(world):
    world.token_balance = 0
    inter = FakeInteraction()
    await cog.RhcCog.sell.callback(cog.RhcCog(bot=None), inter, PONS, 50, None)
    assert "hold no PONS" in inter.last and world.executed == []


# ---------------- withdraw and export gating ----------------


@pytest.mark.asyncio
async def test_withdraw_works_even_with_trading_switched_off(world, monkeypatch):
    monkeypatch.setattr(cog, "RHC_TRADING_ENABLE", False)
    sent = []

    async def send_native(user_id, to, amount):
        sent.append((to, amount))
        return swap.SwapResult(ok=True, tx="0xw", amount_out=amount)

    async def code_size(addr):
        return 0
    monkeypatch.setattr(swap, "send_native", send_native)
    monkeypatch.setattr(chain, "code_size", code_size)
    inter = FakeInteraction()
    await cog.RhcCog.wallet_withdraw.callback(cog.RhcCog(bot=None), inter, "0x000000000000000000000000000000000000dEaD", "0.05")
    assert inter.last.startswith("✅") and sent == [("0x000000000000000000000000000000000000dEaD", 5 * 10**16)]
    assert inter.texts[0].startswith("Send **0.05 ETH** ($125.00) to `0x0000"), inter.texts[0]


@pytest.mark.asyncio
async def test_withdraw_refuses_the_zero_address_and_warns_on_contracts(world, monkeypatch):
    inter = FakeInteraction()
    await cog.RhcCog.wallet_withdraw.callback(cog.RhcCog(bot=None), inter, chain.ZERO, "0.05")
    assert inter.last.startswith("❌")

    async def code_size(addr):
        return 5000

    async def send_native(user_id, to, amount):
        return swap.SwapResult(ok=True, tx="0xw", amount_out=amount)
    monkeypatch.setattr(chain, "code_size", code_size)
    monkeypatch.setattr(swap, "send_native", send_native)
    inter = FakeInteraction()
    await cog.RhcCog.wallet_withdraw.callback(cog.RhcCog(bot=None), inter, kyber.RHC_KYBER_ROUTER, "0.05")
    assert "destination is a contract" in inter.texts[0]


@pytest.mark.asyncio
async def test_public_mode_posts_results_but_keeps_prompts_and_refusals_private(world, monkeypatch):
    monkeypatch.setattr(cog, "PRIVATE", False)
    inter = FakeInteraction()
    await run_buy(inter)
    assert inter.response.deferred_ephemeral is True, "the confirm prompt is always private"
    prompt = inter.followup.sent[0]
    assert prompt[1].get("ephemeral") is True and prompt[0].startswith("Buy **36 PONS**")
    assert "is buying" not in prompt[0] and "can confirm" not in prompt[0]
    assert inter.last.startswith("**tester** · ✅") and inter.last_kw.get("ephemeral") is False

    # A stranger's refusal is private and sent before any defer.
    stranger = FakeInteraction(user_id=99)
    await run_buy(stranger)
    assert stranger.last_kw.get("via") == "response" and stranger.last_kw.get("ephemeral") is True
    assert stranger.last.startswith("🔒") and stranger.response.deferred_ephemeral is None

    # Export is private no matter what.
    exp = FakeInteraction()
    await cog.RhcCog.wallet_export.callback(cog.RhcCog(bot=None), exp)
    assert all(kw.get("ephemeral") is True for _c, kw in exp.followup.sent)


@pytest.mark.asyncio
async def test_thin_pool_warning_and_one_retry_on_a_slippage_revert(world):
    world.liquidity = 4_000.0
    world.results = [
        swap.SwapResult(ok=False, error=guard.SLIPPAGE_TEXT),   # simulation said the floor would not be met
        swap.SwapResult(ok=True, tx="0xretry", amount_out=36 * 10**18, gas_cost_wei=10**14),
    ]
    inter = FakeInteraction()
    await run_buy(inter)
    assert "Thin pool" in inter.texts[0] and "slippage_bps:500" in inter.texts[0]
    assert inter.last.startswith("✅") and "0xretry" in inter.last
    assert len(world.executed) == 2, "one fresh quote and retry, then success"
    assert ledger.spent_today(USER) == pytest.approx(25.0), "the reservation stands after a successful retry"


@pytest.mark.asyncio
async def test_slippage_revert_twice_is_reported_plainly_and_refunded(world):
    world.results = [
        swap.SwapResult(ok=False, error=guard.SLIPPAGE_TEXT),
        swap.SwapResult(ok=False, error=guard.SLIPPAGE_TEXT),
    ]
    inter = FakeInteraction()
    await run_buy(inter)
    assert inter.last.startswith("❌") and "Nothing was spent" in inter.last and "0x" not in inter.last
    assert len(world.executed) == 2 and ledger.spent_today(USER) == 0.0


@pytest.mark.asyncio
async def test_buy_in_dollars_and_results_carry_dollar_amounts(world):
    inter = FakeInteraction()
    await run_buy(inter, eth=None, usd=5.0)               # $5 at $2500/ETH = 0.002 ETH
    assert world.executed[0].route.amount_in == world.buy_route.amount_in   # the fake route ignores size
    # Entry MC is the price paid in market-cap terms: $24.80 / 36 tokens × (500M / $0.70 supply) ≈ $492M.
    assert inter.last.startswith("✅") and "($24.80)" in inter.last and "in at $492M MC" in inter.last

    both = FakeInteraction()
    await run_buy(both, eth="0.01", usd=5.0)
    assert "either" in both.last and both.response.deferred_ephemeral is None
    neither = FakeInteraction()
    await run_buy(neither, eth=None, usd=None)
    assert "either" in neither.last


@pytest.mark.asyncio
async def test_sell_result_shows_the_multiple_from_entry(world):
    # 36 PONS for $20 is $0.556 a token; the price is $0.70 now, so 1.26x. The
    # journal's $50K market cap at buy time is NOT the anchor: restated at
    # today's supply ($200K / $0.70) the entry was $159K, which agrees with the
    # dollars. (The old market-cap ratio would have claimed 4x on a 26% gain.)
    ledger.journal({"ts": 1.0, "user_id": USER, "kind": "buy", "token": PONS, "symbol": "PONS", "decimals": 18,
                    "amount_in": "10000000000000000", "quoted_out": str(36 * 10**18), "actual_out_estimate": str(36 * 10**18),
                    "usd_in": 20.0, "mc_usd": 50_000.0, "tx": "0xb1", "status": "confirmed", "gas_cost_wei": "0"})
    world.mc_now = 200_000.0
    world.result = swap.SwapResult(ok=True, tx="0xsell", amount_out=5 * 10**15, gas_cost_wei=10**14)
    inter = FakeInteraction()
    await cog.RhcCog.sell.callback(cog.RhcCog(bot=None), inter, PONS, 50, None)
    assert inter.texts[0].startswith("Sell **18 PONS** (50% of 36) for **≈ 0.005 ETH ($24.70)**?"), inter.texts[0]
    assert inter.last.startswith("✅ Sold 18 PONS for 0.005 ETH ($24.70)"), inter.last
    assert "**1.26x** from your entry ($159K → $200K MC)" in inter.last, inter.last
    assert world.last_extra["multiple"] == pytest.approx(1.26) and world.last_extra["entry_mc"] == pytest.approx(158_730, rel=0.001)
    journaled = [e for e in ledger.entries_for(USER) if e.get("kind") == "sell"]
    assert journaled == [], "the fake executor journals nothing; the real one is covered in test_rhc_swap"


@pytest.mark.asyncio
async def test_holdings_history_pnl_and_stats_render(world, monkeypatch):
    ledger.journal({"ts": 1.0, "user_id": USER, "kind": "buy", "token": PONS, "symbol": "PONS", "decimals": 18,
                    "amount_in": "10000000000000000", "quoted_out": str(36 * 10**18), "actual_out_estimate": str(36 * 10**18),
                    "usd_in": 20.0, "mc_usd": 250_000_000.0, "tx": "0xb1", "status": "confirmed", "gas_cost_wei": str(10**14)})
    world.mc_now = 500_000_000.0            # price 0.7 × 36 = $25.20 worth against $20 paid: 1.26x
    inter = FakeInteraction()
    await cog.RhcCog.holdings.callback(cog.RhcCog(bot=None), inter)
    embed = inter.followup.sent[-1][1]["embed"]
    text = embed.description + "\n" + "\n".join(f.value for f in embed.fields)
    assert embed.description.startswith("**$2.53K** total · 1 ETH ($2.5K) · tokens $25.20"), embed.description
    assert "**PONS** · 36 · **$25.20** now · cost $20.00 · **1.26x**" in text, text
    assert "Net **+$4.95** · unrealized +$5.20 · realized +$0.00 · gas $0.25" in text, text   # 25.20 - 20 - 0.25
    assert "$250" not in text, "the buy-time market cap reading is not shown; the multiple carries the entry"

    hist = FakeInteraction()
    await cog.RhcCog.history.callback(cog.RhcCog(bot=None), hist, 10)
    h = hist.followup.sent[-1][1]["embed"].description
    assert h.startswith("✅ <t:1:R> Bought 36 PONS for 0.01 ETH ($20.00) [tx](") , h

    p = FakeInteraction()
    await cog.RhcCog.pnl_cmd.callback(cog.RhcCog(bot=None), p)
    kw = p.followup.sent[-1][1]
    assert kw["embed"].description.startswith("**Net +$4.95** · bought $20.00 of tokens"), kw["embed"].description
    assert "**PONS** · net **+$5.20** · 1.26x · cost $20.00" in kw["embed"].fields[0].value
    assert kw["files"] and kw["files"][0].filename == "pnl.png"

    s = FakeInteraction()
    await cog.RhcCog.stats.callback(cog.RhcCog(bot=None), s)
    d = s.followup.sent[-1][1]["embed"].description
    assert "**1 wallet**, 1 trading" in d and "1 buy, 0 sells · $20.00 traded · gas $0.25" in d, d
    assert "Realized **+$0.00** · best sell —" in d, d


@pytest.mark.asyncio
async def test_visibility_defaults_in_public_mode(world, monkeypatch):
    """Your book is yours by default (public:True shares it); a trade result is
    the channel's by default (private:True keeps it); the confirm prompt is
    always yours."""
    monkeypatch.setattr(cog, "PRIVATE", False)
    mine = FakeInteraction()
    await cog.RhcCog.holdings.callback(cog.RhcCog(bot=None), mine)
    assert mine.response.deferred_ephemeral is True and mine.last_kw.get("ephemeral") is True

    shared = FakeInteraction()
    await cog.RhcCog.holdings.callback(cog.RhcCog(bot=None), shared, public=True)
    assert shared.response.deferred_ephemeral is False and shared.last_kw.get("ephemeral") is False

    for cmd in (cog.RhcCog.history, cog.RhcCog.pnl_cmd):
        i = FakeInteraction()
        await cmd.callback(cog.RhcCog(bot=None), i)
        assert i.response.deferred_ephemeral is True, cmd.name

    loud_buy = FakeInteraction()
    await run_buy(loud_buy)
    assert loud_buy.response.deferred_ephemeral is True and loud_buy.last_kw.get("ephemeral") is False
    quiet_buy = FakeInteraction()
    await cog.RhcCog.buy.callback(cog.RhcCog(bot=None), quiet_buy, PONS, eth="0.01", private=True)
    assert quiet_buy.last_kw.get("ephemeral") is True
    assert not quiet_buy.last.startswith("**tester**"), "no name prefix when nobody else can see it"


@pytest.mark.asyncio
async def test_sell_autocomplete_lists_holdings_with_amounts(world, monkeypatch):
    ledger.journal({"ts": 1.0, "user_id": USER, "kind": "buy", "token": PONS, "symbol": "PONS", "decimals": 18,
                    "amount_in": "10000000000000000", "quoted_out": str(36 * 10**18), "actual_out_estimate": str(36 * 10**18),
                    "usd_in": 20.0, "mc_usd": 50_000.0, "tx": "0xb1", "status": "confirmed", "gas_cost_wei": "0"})
    other = "0x" + "77" * 20
    ledger.journal({"ts": 2.0, "user_id": USER, "kind": "buy", "token": other, "symbol": "GONE", "decimals": 18,
                    "amount_in": "1", "quoted_out": "1", "actual_out_estimate": "1",
                    "usd_in": 1.0, "tx": "0xb2", "status": "confirmed", "gas_cost_wei": "0"})

    async def erc20_balance(token, owner):
        return 36 * 10**18 if token.lower() == PONS else 0     # GONE was fully sold
    monkeypatch.setattr(chain, "erc20_balance", erc20_balance)
    cog.RhcCog._bal_cache.clear()

    c = cog.RhcCog(bot=None)
    choices = await c._holding_choices(USER, "")
    assert [ch.value.lower() for ch in choices] == [PONS], "only tokens with a balance are offered"
    assert choices[0].name == "PONS · 36 · cost $20.00"
    assert await c._holding_choices(USER, "po") and not await c._holding_choices(USER, "zzz")
    assert await c._holding_choices(999, "") == [], "no wallet, no choices"

    inter = FakeInteraction()
    assert [ch.value.lower() for ch in await c.sell_token_autocomplete(inter, "")] == [PONS]


@pytest.mark.asyncio
async def test_buy_autocomplete_suggests_the_busiest_tokens(world, monkeypatch):
    def pool(addr, sym, base, vol, mc):
        return rhchain.Pool(address=addr, name=f"{sym} / WETH", dex="Uniswap V3", base_symbol=sym, base_name=sym,
                            base_address=base, quote_symbol="WETH", price_usd=1, liq_usd=1e6, mc_usd=mc,
                            volume={"h24": vol}, change={"h24": 0.0}, buys_h24=0, sells_h24=0, created_ts=1.0)

    async def pools():
        return [pool("0xa", "PONS", PONS, 100e6, 500e6), pool("0xb", "MEME", "0x" + "2" * 40, 30e6, 90e6),
                pool("0xc", "WETH", chain.WETH.lower(), 900e6, 6e9)]
    monkeypatch.setattr(rhchain, "top_pools", pools)
    c = cog.RhcCog(bot=None)
    choices = await c.buy_token_autocomplete(FakeInteraction(), "")
    assert [ch.name.split(" · ")[0] for ch in choices] == ["PONS", "MEME"], "majors hidden, busiest first"
    assert choices[0].value.lower() == PONS and choices[0].name == "PONS · $100M 24h vol · $500M MC"
    assert [ch.name.split(" · ")[0] for ch in await c.buy_token_autocomplete(FakeInteraction(), "me")] == ["MEME"]


@pytest.mark.asyncio
async def test_result_falls_back_to_a_dm_when_the_followup_fails(world):
    inter = FakeInteraction()

    async def broken(content=None, **kw):
        raise RuntimeError("interaction token expired")
    inter.followup.send = broken
    await cog.RhcCog._reply(inter, "✅ Bought", True)
    assert inter.dms == ["✅ Bought"]
