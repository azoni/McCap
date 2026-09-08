"""/rhc buy, sell and withdraw flows with a fake interaction.

Money is reserved before a swap, refunded only on a definite failure, kept on a
pending broadcast, and a stale re-quote below the confirmed floor stops the
trade. The chain, Kyber and the executor are all faked; nothing is signed.
"""

import os
import time
from types import SimpleNamespace

import pytest

from mccapbot.cogs import rhc as cog
from mccapbot.rhc import chain, kyber, ledger, swap, wallets

USER = 7
PONS = "0x39dbed3a2bd333467115de45665cc57f813c4571"
WALLET = "0x" + "a1" * 20


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))
        return SimpleNamespace()


class FakeResponse:
    """Tracks whether the interaction was answered, like discord.py's InteractionResponse."""

    def __init__(self, sink):
        self.done = False
        self.deferred_ephemeral = None
        self._sink = sink

    def is_done(self):
        return self.done

    async def defer(self, **kw):
        self.done = True
        self.deferred_ephemeral = kw.get("ephemeral")

    async def send_message(self, content=None, **kw):
        self.done = True
        self._sink.append((content, {**kw, "via": "response"}))


class FakeInteraction:
    def __init__(self, user_id=USER, guild_id=1):
        self.user = SimpleNamespace(id=user_id, display_name="tester", send=self._dm)
        self.guild_id = guild_id
        self.followup = FakeFollowup()
        self.response = FakeResponse(self.followup.sent)
        self.dms = []

    async def _dm(self, content=None, **kw):
        self.dms.append(content)

    @property
    def texts(self):
        return [c for c, _ in self.followup.sent if c]

    @property
    def last(self):
        return self.texts[-1] if self.texts else ""

    @property
    def last_kw(self):
        return self.followup.sent[-1][1] if self.followup.sent else {}


class Confirmed:
    """Stands in for ConfirmOrder: the click already happened."""
    value = True

    def __init__(self, owner_id, timeout):
        pass

    async def wait(self):
        return


def route(token_in, token_out, amount_in, amount_out, usd_in=24.8, usd_out=24.7):
    return kyber.Route(token_in=token_in, token_out=token_out, amount_in=amount_in, amount_out=amount_out,
                       amount_in_usd=usd_in, amount_out_usd=usd_out, gas=1, gas_usd=0.49,
                       router=kyber.RHC_KYBER_ROUTER, summary={}, hops=["uniswap-v4"])


class World:
    """Everything the command touches, in one mutable place."""

    def __init__(self):
        self.buy_route = route(chain.NATIVE, PONS, 10**16, 36 * 10**18)
        self.requotes = []                  # routes returned on re-quote, in order
        self.back = route(PONS, chain.NATIVE, 36 * 10**18, 10**16 * 99 // 100)
        self.back_error = None
        self.sell_route = route(PONS, chain.NATIVE, 18 * 10**18, 5 * 10**15)
        self.eth_usd = 2500.0
        self.result = swap.SwapResult(ok=True, tx="0xabc", amount_out=36 * 10**18, gas_cost_wei=10**14)
        self.executed = []                  # BuiltSwap objects handed to swap.execute
        self.token_balance = 36 * 10**18
        self.eth_balance = 10**18
        self.route_calls = 0

    def install(self, monkeypatch):
        w = self

        async def kroute(token_in, token_out, amount_in):
            w.route_calls += 1
            if token_in.lower() == chain.NATIVE.lower():
                if w.requotes and w.route_calls > 1:
                    return w.requotes.pop(0)
                return w.buy_route
            if w.back_error:
                raise w.back_error
            return w.sell_route if amount_in == 18 * 10**18 else w.back

        async def kbuild(rt, sender, bps, recipient=None):
            assert sender.lower() == WALLET.lower()
            return kyber.BuiltSwap(router=rt.router, data="0xe21fd0e9", value=rt.amount_in if rt.token_in == chain.NATIVE else 0,
                                   amount_in=rt.amount_in, amount_out=rt.amount_out, amount_in_usd=rt.amount_in_usd,
                                   amount_out_usd=rt.amount_out_usd, gas=1, gas_usd=0.49, slippage_bps=bps,
                                   min_out=kyber.min_out(rt.amount_out, bps), route=rt)

        async def execute(user_id, built, token, symbol):
            w.executed.append(built)
            return w.result

        async def eth_usd(self_):
            return w.eth_usd

        async def meta(addr):
            return ("PONS", 18)

        async def erc20_balance(token, owner):
            return w.token_balance

        async def native_balance(addr):
            return w.eth_balance

        async def gas_price():
            return 300_000_000

        async def estimate_gas(tx):
            return 21_000

        monkeypatch.setattr(chain, "native_balance", native_balance)
        monkeypatch.setattr(chain, "gas_price", gas_price)
        monkeypatch.setattr(chain, "estimate_gas", estimate_gas)
        monkeypatch.setattr(kyber, "route", kroute)
        monkeypatch.setattr(kyber, "build", kbuild)
        monkeypatch.setattr(swap, "execute", execute)
        monkeypatch.setattr(cog.RhcCog, "_eth_usd", eth_usd)
        monkeypatch.setattr(chain, "erc20_meta", meta)
        monkeypatch.setattr(chain, "erc20_balance", erc20_balance)


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(cog, "RHC_TRADING_ENABLE", True)
    monkeypatch.setattr(cog, "RHC_TRADER_IDS", {USER})
    monkeypatch.setattr(cog, "RHC_GUILD_IDS", set())
    monkeypatch.setattr(cog, "ConfirmOrder", Confirmed)
    monkeypatch.setattr(cog, "PRIVATE", True)          # the visibility test flips this
    monkeypatch.setattr(wallets, "unlockable", lambda: True)
    wallets.wallets.clear()
    wallets._loaded = True
    wallets.wallets.append(wallets.Wallet(user_id=USER, address=WALLET, salt="", nonce="", ciphertext="", ops=1, mem=1))
    for p in (ledger.RHC_LEDGER_FILE, ledger.RHC_JOURNAL_FILE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    w = World()
    w.install(monkeypatch)
    yield w
    wallets.wallets.clear()
    for p in (ledger.RHC_LEDGER_FILE, ledger.RHC_JOURNAL_FILE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


async def run_buy(inter, eth="0.01", slippage=None):
    await cog.RhcCog.buy.callback(cog.RhcCog(bot=None), inter, PONS, eth, slippage)


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
    assert "counts as $25.00" in inter.texts[0]


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
    assert "≈ $125.00" in inter.texts[0]


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
    prompt_kw = inter.followup.sent[0][1]
    assert prompt_kw.get("ephemeral") is True, "the confirm prompt stays private"
    assert inter.response.deferred_ephemeral is True
    assert inter.last.startswith("**tester** · ✅") and inter.last_kw.get("ephemeral") is False

    # A stranger's refusal is private and sent before any defer.
    stranger = FakeInteraction(user_id=99)
    await run_buy(stranger)
    assert stranger.last_kw.get("ephemeral") is True and stranger.last.startswith("🔒")

    # Quotes go public; the allowlist refusal for quotes is private and pre-defer.
    ok = FakeInteraction()
    await cog.RhcCog.quote.callback(cog.RhcCog(bot=None), ok, PONS, "0.01")
    assert ok.response.deferred_ephemeral is False and ok.last_kw.get("ephemeral") is False
    nope = FakeInteraction(user_id=99)
    await cog.RhcCog.quote.callback(cog.RhcCog(bot=None), nope, PONS, "0.01")
    assert nope.last_kw.get("via") == "response" and nope.last_kw.get("ephemeral") is True
    assert nope.response.deferred_ephemeral is None, "refused before deferring"

    # Export is private no matter what.
    exp = FakeInteraction()
    await cog.RhcCog.wallet_export.callback(cog.RhcCog(bot=None), exp)
    assert all(kw.get("ephemeral") is True for _c, kw in exp.followup.sent)


@pytest.mark.asyncio
async def test_result_falls_back_to_a_dm_when_the_followup_fails(world):
    inter = FakeInteraction()

    async def broken(content=None, **kw):
        raise RuntimeError("interaction token expired")
    inter.followup.send = broken
    await cog.RhcCog._reply(inter, "✅ Bought")
    assert inter.dms == ["✅ Bought"]
