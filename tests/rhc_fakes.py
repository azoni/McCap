"""Shared fakes for the /rh command, button, auto-order and tutorial tests.

Everything a trade touches, in one mutable place, so the suites do not fork
their own FakeInteraction or World. Nothing here signs or sends: the chain,
Kyber and the executor are all stand-ins.
"""

import time
from types import SimpleNamespace
from typing import Optional

from mccapbot.cogs import rhc as cog
from mccapbot.models import AutoOrder
from mccapbot.rhc import chain, kyber, pnl, portfolio, swap, trade, wallets

USER = 7
PONS = "0x39dbed3a2bd333467115de45665cc57f813c4571"
WALLET = "0x" + "a1" * 20


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))
        return SimpleNamespace(id=len(self.sent))


class FakeResponse:
    """Tracks whether the interaction was answered, like discord.py's InteractionResponse."""

    def __init__(self, sink):
        self.done = False
        self.deferred_ephemeral = None
        self.modal = None
        self.edited = []
        self._sink = sink

    def is_done(self):
        return self.done

    async def defer(self, **kw):
        self.done = True
        self.deferred_ephemeral = kw.get("ephemeral")

    async def send_message(self, content=None, **kw):
        self.done = True
        self._sink.append((content, {**kw, "via": "response"}))

    async def send_modal(self, modal):
        self.done = True
        self.modal = modal

    async def edit_message(self, **kw):
        self.done = True
        self.edited.append(kw)


class FakeInteraction:
    def __init__(self, user_id=USER, guild_id=1, client=None, channel_id=99):
        self.user = SimpleNamespace(id=user_id, display_name="tester", send=self._dm)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.channel = SimpleNamespace(id=channel_id)
        self.message = SimpleNamespace(id=1)
        self.data = {}
        self.client = client
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
        self.owner_id = owner_id

    async def wait(self):
        return


class Declined(Confirmed):
    value = False


class Expired(Confirmed):
    value = None


def route(token_in, token_out, amount_in, amount_out, usd_in=24.8, usd_out=24.7):
    return kyber.Route(token_in=token_in, token_out=token_out, amount_in=amount_in, amount_out=amount_out,
                       amount_in_usd=usd_in, amount_out_usd=usd_out, gas=1, gas_usd=0.49,
                       router=kyber.RHC_KYBER_ROUTER, summary={}, hops=["uniswap-v4"])


class World:
    """Everything a trade touches, in one mutable place."""

    def __init__(self):
        self.buy_route = route(chain.NATIVE, PONS, 10**16, 36 * 10**18)
        self.requotes = []                  # routes returned on re-quote, in order
        self.back = route(PONS, chain.NATIVE, 36 * 10**18, 10**16 * 99 // 100)
        self.back_error = None
        self.sell_route = route(PONS, chain.NATIVE, 18 * 10**18, 5 * 10**15)
        self.eth_usd = 2500.0
        self.result = swap.SwapResult(ok=True, tx="0xabc", amount_out=36 * 10**18, gas_cost_wei=10**14)
        self.executed = []                  # BuiltSwap objects handed to swap.execute
        self.extras = []                    # the extra dict handed to each execute
        self.token_balance = 36 * 10**18
        self.eth_balance = 10**18
        self.liquidity = 500_000.0
        self.mc_now = 500_000_000.0
        self.price_now = 0.7
        self.vol1h = 12_000.0
        self.results = []                   # optional sequence of results for successive executes
        self.route_calls = 0
        self.summary_calls = 0
        self.last_extra = None

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
            return w.sell_route if amount_in == w.sell_route.amount_in else w.back

        async def kbuild(rt, sender, bps, recipient=None):
            assert sender.lower() == WALLET.lower()
            return kyber.BuiltSwap(router=rt.router, data="0xe21fd0e9", value=rt.amount_in if rt.token_in == chain.NATIVE else 0,
                                   amount_in=rt.amount_in, amount_out=rt.amount_out, amount_in_usd=rt.amount_in_usd,
                                   amount_out_usd=rt.amount_out_usd, gas=1, gas_usd=0.49, slippage_bps=bps,
                                   min_out=kyber.min_out(rt.amount_out, bps), route=rt)

        async def execute(user_id, built, token, symbol, extra=None):
            w.executed.append(built)
            w.extras.append(extra)
            w.last_extra = extra
            if w.results:
                return w.results.pop(0)
            return w.result

        async def summary(addr):
            w.summary_calls += 1
            if addr.lower() == chain.WETH.lower():
                return {"price": w.eth_usd}
            return {"liq": w.liquidity, "price": w.price_now, "mc": w.mc_now, "vol1h": w.vol1h,
                    "symbol": "PONS", "name": "Pons", "chain": "robinhood"}
        monkeypatch.setattr(cog, "token_summary", summary)
        monkeypatch.setattr(trade, "token_summary", summary)
        monkeypatch.setattr(pnl, "token_summary", summary)
        monkeypatch.setattr(portfolio, "token_summary", summary)
        portfolio._cache = None

        async def eth_usd(self_=None):
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
        monkeypatch.setattr(trade, "eth_usd", eth_usd)
        monkeypatch.setattr(chain, "erc20_meta", meta)
        monkeypatch.setattr(chain, "erc20_balance", erc20_balance)


def arm_world(monkeypatch, user_id=USER):
    """Trading on for one user with one wallet and a clean ledger; returns the World."""
    import os
    from mccapbot.rhc import ledger
    monkeypatch.setattr(cog, "RHC_TRADING_ENABLE", True)
    monkeypatch.setattr(cog, "RHC_TRADER_IDS", {user_id})
    monkeypatch.setattr(cog, "RHC_GUILD_IDS", set())
    monkeypatch.setattr(cog, "ConfirmOrder", Confirmed)
    monkeypatch.setattr(cog, "PRIVATE", True)          # the visibility test flips this
    monkeypatch.setattr(wallets, "unlockable", lambda: True)
    wallets.wallets.clear()
    wallets._loaded = True
    wallets.wallets.append(wallets.Wallet(user_id=user_id, address=WALLET, salt="", nonce="", ciphertext="", ops=1, mem=1))
    for p in (ledger.RHC_LEDGER_FILE, ledger.RHC_JOURNAL_FILE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    w = World()
    w.install(monkeypatch)
    return w


def disarm_world():
    import os
    from mccapbot.rhc import ledger
    wallets.wallets.clear()
    for p in (ledger.RHC_LEDGER_FILE, ledger.RHC_JOURNAL_FILE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


class Chan:
    """A channel that records what was sent to it."""

    def __init__(self, channel_id=99, fail=None):
        self.id = channel_id
        self.sent = []
        self.fail = fail

    async def send(self, content=None, **kw):
        if self.fail:
            raise self.fail
        self.sent.append((content, kw))
        return SimpleNamespace(id=len(self.sent))

    @property
    def texts(self):
        return [c for c, _ in self.sent if c]


class FakeBot:
    """Just enough of commands.Bot for the auto-order engine and the buttons."""

    def __init__(self, cog_obj=None, channel: Optional[Chan] = None):
        self._cog = cog_obj
        self.channel = channel or Chan()
        self.users = {}
        self.fetched = []
        self.closed = False

    def get_cog(self, name):
        return self._cog if name == "RhcCog" else None

    async def fetch_channel(self, channel_id):
        self.fetched.append(channel_id)
        if isinstance(self.channel, Exception):
            raise self.channel
        return self.channel

    async def fetch_user(self, user_id):
        return self.users.setdefault(user_id, Chan(channel_id=user_id))

    def is_closed(self):
        return self.closed

    async def wait_until_ready(self):
        return


def make_order(**overrides) -> AutoOrder:
    base = dict(ca=chain.to_checksum(PONS), symbol="PONS", decimals=18, side="sell", metric="mc", direction="above",
                target=500_000.0, size=50.0, slippage_bps=200, user_id=USER, guild_id=1, channel_id=99,
                expires_ts=time.time() + 3600)
    base.update(overrides)
    return AutoOrder(**base)


def make_match(cls, custom_id: str):
    """The regex match a DynamicItem subclass would receive for this custom_id, or None."""
    return cls.__discord_ui_compiled_template__.fullmatch(custom_id)
