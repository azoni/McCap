"""/rh gates and helpers. Money must not move for the wrong person, server, or flag."""

import pytest

from mccapbot.cogs import rhc as cog
from mccapbot.rhc import chain, kyber, swap, wallets
from mccapbot import rhchain

PONS = "0x39dbed3a2bd333467115de45665cc57f813c4571"


@pytest.fixture(autouse=True)
def loaded_vault():
    wallets.wallets.clear()
    wallets._loaded = True
    yield
    wallets.wallets.clear()


def test_trading_is_off_by_default():
    reason = cog._gate()
    assert reason and "RHC_TRADING_ENABLE" in reason


def test_gate_checks_flag_then_allowlist_then_vault(monkeypatch):
    monkeypatch.setattr(cog, "RHC_TRADING_ENABLE", True)
    monkeypatch.setattr(cog, "RHC_TRADER_IDS", set())
    assert "allowlist" in cog._gate()
    monkeypatch.setattr(cog, "RHC_TRADER_IDS", {5})
    monkeypatch.setattr(wallets, "RHC_WALLET_SECRET", "")
    assert "RHC_WALLET_SECRET" in cog._gate()
    monkeypatch.setattr(wallets, "RHC_WALLET_SECRET", "long-enough-secret-0123456789-abcdef")
    assert cog._gate() is None
    wallets._loaded = False
    assert "did not load" in cog._gate()


def test_allowlist_never_means_everyone(monkeypatch):
    monkeypatch.setattr(cog, "RHC_TRADER_IDS", {5, 6})
    assert cog.allowed(5) and not cog.allowed(7)
    monkeypatch.setattr(cog, "RHC_TRADER_IDS", set())
    assert not cog.allowed(0) and not cog.allowed(7)


def test_guild_allowlist_covers_dms_too(monkeypatch):
    monkeypatch.setattr(cog, "RHC_GUILD_IDS", set())
    assert cog.guild_ok(123) and cog.guild_ok(None)
    monkeypatch.setattr(cog, "RHC_GUILD_IDS", {123})
    assert cog.guild_ok(123) and not cog.guild_ok(456)
    assert not cog.guild_ok(None), "a server allowlist must not be bypassed by DMing the bot"


def test_slippage_is_clamped(monkeypatch):
    monkeypatch.setattr(cog, "RHC_DEFAULT_SLIPPAGE_BPS", 200)
    monkeypatch.setattr(cog, "RHC_MAX_SLIPPAGE_BPS", 1000)
    assert cog.clamp_slippage(None) == 200
    assert cog.clamp_slippage(5) == 10
    assert cog.clamp_slippage(5000) == 1000
    assert cog.clamp_slippage(300) == 300


def route(amount_in=10**16, amount_out=36 * 10**18, usd_in=24.8, usd_out=24.7, hops=("uniswap-v4-fee",)):
    return kyber.Route(token_in=chain.NATIVE, token_out=PONS, amount_in=amount_in, amount_out=amount_out,
                       amount_in_usd=usd_in, amount_out_usd=usd_out, gas=1, gas_usd=0.49,
                       router=cog.kyber.RHC_KYBER_ROUTER, summary={}, hops=list(hops))


def back_route(amount_out):
    return kyber.Route(token_in=PONS, token_out=chain.NATIVE, amount_in=36 * 10**18, amount_out=amount_out,
                       amount_in_usd=24.7, amount_out_usd=24.5, gas=1, gas_usd=0.4, router="", summary={})


def test_quote_text_shows_the_address_and_flags_honeypots_and_bad_round_trips():
    rt = route()
    no_way_back = cog.RhcCog._quote_text(rt, PONS, "PONS", 18, None, False)
    assert "No sell route" in no_way_back and "Honeypot" in no_way_back and PONS in no_way_back
    unknown = cog.RhcCog._quote_text(rt, PONS, "PONS", 18, None, True)
    assert "did not answer" in unknown and "Honeypot" not in unknown
    fine = cog.RhcCog._quote_text(rt, PONS, "PONS", 18, back_route(10**16 * 99 // 100), False, liq=500_000.0)
    assert fine.startswith("Buy **36 PONS** for **0.01 ETH ($24.80)**?"), fine
    assert "round trip" in fine and "liquidity $500K" in fine and "gas ≈ $0.49" in fine and "⚠️" not in fine
    bad = cog.RhcCog._quote_text(rt, PONS, "PONS", 18, back_route(10**16 // 2), False)
    assert "⚠️" in bad and "-50%" in bad


def test_describe_distinguishes_ok_pending_and_failed():
    ok = swap.SwapResult(ok=True, tx="0xabc", gas_cost_wei=10**14)
    assert cog.RhcCog._describe(ok, "Bought X").startswith("✅") and "0xabc" in cog.RhcCog._describe(ok, "x")
    assert "gas" not in cog.RhcCog._describe(ok, "x"), "gas lives in /rh holdings and /rh stats, not on every receipt"
    pending = swap.SwapResult(ok=False, tx="0xdef", pending=True, error="Submitted but unconfirmed.")
    assert cog.RhcCog._describe(pending, "x").startswith("⏳")
    failed = swap.SwapResult(ok=False, error="REFUSING TO SIGN")
    text = cog.RhcCog._describe(failed, "x")
    assert text.startswith("❌") and "Transaction" not in text


@pytest.mark.asyncio
async def test_round_trip_separates_no_route_from_kyber_down(monkeypatch):
    c = cog.RhcCog(bot=None)

    async def no_route(*a):
        raise kyber.NoRoute("no route")
    monkeypatch.setattr(kyber, "route", no_route)
    assert await c._round_trip(PONS, 1) == (None, False)

    async def down(*a):
        raise kyber.KyberUnavailable("429")
    monkeypatch.setattr(kyber, "route", down)
    assert await c._round_trip(PONS, 1) == (None, True)

    async def fine(*a):
        return back_route(10**16)
    monkeypatch.setattr(kyber, "route", fine)
    rt, unavailable = await c._round_trip(PONS, 1)
    assert rt.amount_out == 10**16 and not unavailable


@pytest.mark.asyncio
async def test_resolve_token_by_address_symbol_and_ambiguity(monkeypatch):
    async def meta(addr):
        return ("PONS", 18)
    monkeypatch.setattr(chain, "erc20_meta", meta)

    def pool(addr, sym, base):
        return rhchain.Pool(address=addr, name=f"{sym} / WETH", dex="Uniswap V3", base_symbol=sym, base_name=sym,
                            base_address=base, quote_symbol="WETH", price_usd=1, liq_usd=1, mc_usd=1,
                            volume={"h24": 1.0}, change={"h24": 0.0}, buys_h24=0, sells_h24=0, created_ts=1.0)

    async def pools():
        return [pool("0xp", "PONS", PONS), pool("0xq", "DUP", "0x" + "1" * 40), pool("0xr", "DUP", "0x" + "2" * 40)]
    monkeypatch.setattr(rhchain, "top_pools", pools)

    c = cog.RhcCog(bot=None)
    addr, sym, dec = await c._resolve_token(PONS)
    assert addr.lower() == PONS and sym == "PONS" and dec == 18
    addr, sym, dec = await c._resolve_token("pons")
    assert addr.lower() == PONS
    with pytest.raises(ValueError, match="Several"):
        await c._resolve_token("DUP")
    with pytest.raises(ValueError, match="Unknown"):
        await c._resolve_token("NOPE")
