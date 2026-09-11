"""The persistent trade buttons in mccapbot/views.py.

Templates match exactly what the factories emit and nothing near it; owner-bound
items refuse strangers before any defer; every click gates through the cog
(allowlist, wallet, cap) before the first I/O; the cog's coroutines receive the
arguments the contract promises; a broken handler still answers the interaction
exactly once; factories return None instead of raising.

The cog is a recording fake: the real one is wired by the integrator and its
own flows are covered in test_rhc_cog_flows.py.
"""

import time
from types import SimpleNamespace

import discord
import pytest

from mccapbot import views
from mccapbot.rhc import wallets
from tests.rhc_fakes import PONS, USER, WALLET, FakeBot, FakeInteraction, make_match

SOLANA = "So11111111111111111111111111111111111111112"
OTHER = "0x" + "b2" * 20


# ---------------- fakes ----------------


class FakeCog:
    """Records what the buttons ask of the cog; sends the refusal texts the real one would."""

    def __init__(self, deny=False, boom=None):
        self.deny = deny
        self.boom = boom            # name of a method that should raise
        self.calls = []
        self.results_private = True

    def default_slippage(self):
        return 200

    def is_allowed(self, user_id):
        return not self.deny

    def _maybe_boom(self, name):
        if self.boom == name:
            raise RuntimeError(f"{name} exploded")

    async def _deny_trade(self, inter):
        self.calls.append(("deny", inter.response.is_done()))
        self._maybe_boom("_deny_trade")
        if self.deny:
            await inter.response.send_message("🔒 You are not on the trader allowlist.", ephemeral=True)
            return True
        return False

    async def _no_wallet(self, inter):
        self.calls.append(("no_wallet", inter.response.is_done()))
        await inter.response.send_message("You have no wallet yet.", ephemeral=True, view=views.wallet_nudge())

    async def _run_sell(self, inter, w, token, percent, *, bps, priv, source):
        self.calls.append(("run_sell", inter.response.is_done(), w, token, percent, bps, priv, source))
        self._maybe_boom("_run_sell")
        await inter.followup.send("quote", ephemeral=True)

    async def _run_buy(self, inter, w, token, *, eth, usd, bps, priv, source):
        self.calls.append(("run_buy", inter.response.is_done(), w, token, eth, usd, bps, priv, source))
        self._maybe_boom("_run_buy")
        await inter.followup.send("quote", ephemeral=True)

    async def _create_wallet(self, inter, priv):
        self.calls.append(("create_wallet", inter.response.is_done(), priv))
        await inter.followup.send("wallet made", ephemeral=priv)

    async def button_wallet_show(self, inter):
        self.calls.append(("wallet_show", inter.response.is_done()))
        await inter.response.send_message("your wallet", ephemeral=True)

    async def button_trending(self, inter):
        self.calls.append(("trending", inter.response.is_done()))
        await inter.response.send_message("board", ephemeral=True)

    async def button_tutorial(self, inter):
        self.calls.append(("tutorial", inter.response.is_done()))
        await inter.response.send_message("tutorial", ephemeral=True)

    async def modal_buy(self, inter, token, usd_text, eth_text):
        self.calls.append(("modal_buy", inter.response.is_done(), token, usd_text, eth_text))
        await inter.followup.send("quote", ephemeral=True)

    async def button_share(self, inter, ca):
        self.calls.append(("button_share", ca))
        await inter.response.send_message("shared", ephemeral=True)

    async def button_feed_vote(self, inter, eid, direction):
        self.calls.append(("button_feed_vote", (eid, direction)))
        await inter.response.send_message("counted", ephemeral=True)

    async def modal_tpsl(self, inter, uid, token, tp_at, tp_pct, sl_at, sl_pct):
        self.calls.append(("modal_tpsl", inter.response.is_done(), uid, token, tp_at, tp_pct, sl_at, sl_pct))
        await inter.followup.send("armed?", ephemeral=True)


@pytest.fixture
def have_wallet():
    wallets.wallets.clear()
    wallets._loaded = True
    wallets.wallets.append(wallets.Wallet(user_id=USER, address=WALLET, salt="", nonce="", ciphertext="", ops=1, mem=1))
    yield wallets.wallets[0]
    wallets.wallets.clear()


@pytest.fixture
def no_wallet():
    wallets.wallets.clear()
    wallets._loaded = True
    yield
    wallets.wallets.clear()


def click(cog_obj, user_id=USER):
    return FakeInteraction(user_id=user_id, client=FakeBot(cog_obj=cog_obj))


async def build(cls, custom_id, item=None):
    """Rebuild a dynamic item the way the view store does: from its custom_id."""
    m = make_match(cls, custom_id)
    assert m is not None, custom_id
    if item is None:
        item = discord.ui.Button(custom_id=custom_id, label="x")
    return await cls.from_custom_id(FakeInteraction(), item, m)


def names(cog):
    return [c[0] for c in cog.calls]


def response_texts(inter):
    return [c for c, kw in inter.followup.sent if kw.get("via") == "response"]


def followup_texts(inter):
    return [c for c, kw in inter.followup.sent if kw.get("via") != "response"]


# ---------------- templates ----------------


GOOD_IDS = {
    views.SellPctButton: f"rh:sell:{USER}:{PONS}:25",
    views.SellMineButton: f"rh:sellme:{PONS}:100",
    views.BuyUsdButton: f"rh:buy:{PONS}:500",
    views.BuyCustomButton: f"rh:buyx:{PONS}",
    views.TpSlButton: f"rh:tpsl:{USER}:{PONS}",
    views.TokenPickSelect: "rh:pick:trending",
    views.NavButton: "rh:nav:wallet_create",
    views.FeedVoteButton: "rh:vote:a1b2c3:up",
    views.ShareButton: f"rh:share:{PONS}",
}

BAD_IDS = [
    f"rh:sell:{USER}:{SOLANA}:25",          # Solana address
    f"rh:sell:{USER}:{PONS}:1000",          # 4-digit pct
    f"rx:sell:{USER}:{PONS}:25",            # wrong prefix
    f"rh:sell:{PONS}:25",                   # owner id missing
    f"rh:sellme:{USER}:{PONS}:25",          # owner id where none belongs
    f"rh:buy:{PONS}:12345678",              # 8-digit cents
    f"rh:buy:{PONS}:5.00",                  # not cents
    f"rh:buyx:{SOLANA}",
    f"rh:tpsl:{PONS}",
    "rh:pick:hot",
    "rh:nav:wallet_delete",
    f"rh:sell:{USER}:{PONS}:25:extra",      # trailing junk
    f" rh:sell:{USER}:{PONS}:25",           # leading junk
    "rh:vote:a1b2c3:sideways",              # not a direction
    "rh:vote:NOTHEX:up",                    # event ids are hex
    "rh:vote:a1b2c3d:up",                   # seven characters, not six
    f"rh:share:{SOLANA}",                   # Solana address
    "rh:share:",                            # no token at all
]


@pytest.mark.parametrize("cls,custom_id", list(GOOD_IDS.items()))
def test_each_template_fullmatches_its_own_id(cls, custom_id):
    assert make_match(cls, custom_id) is not None
    assert len(custom_id) <= 100


@pytest.mark.parametrize("custom_id", BAD_IDS)
def test_templates_reject_near_misses(custom_id):
    assert all(make_match(cls, custom_id) is None for cls in views.DYNAMIC_ITEMS), custom_id


def test_templates_do_not_overlap():
    for cls, custom_id in GOOD_IDS.items():
        hits = [c for c in views.DYNAMIC_ITEMS if make_match(c, custom_id)]
        assert hits == [cls], (custom_id, hits)


def test_dynamic_items_tuple_is_complete_and_registrable():
    assert set(views.DYNAMIC_ITEMS) == set(GOOD_IDS)
    for cls in views.DYNAMIC_ITEMS:
        assert issubclass(cls, discord.ui.DynamicItem)
        assert cls.__discord_ui_compiled_template__.pattern.startswith("rh:")


def test_every_generated_custom_id_fits_discord_even_for_a_20_digit_owner():
    big = 10**19 + 7
    toks = [SimpleNamespace(symbol=f"T{i}", address=f"0x{i:040x}", mc_usd=1e6, liq_usd=1e5) for i in range(25)]
    made = [
        views.receipt_row(big, PONS), views.partial_sell_row(big, PONS), views.size_card(PONS),
        views.alert_row(PONS), views.wallet_nudge(), views.after_create_row(),
        views.tutorial_row("no_wallet"), views.tutorial_row("wallet"), views.tutorial_row("public"),
        views.board_view("trending", toks), views.board_view("new", toks),
    ]
    for v in made:
        assert isinstance(v, discord.ui.View) and v.timeout is None
        for child in v.children:
            assert isinstance(child, discord.ui.DynamicItem)
            assert len(child.custom_id) <= 100
            assert make_match(type(child), child.custom_id) is not None


@pytest.mark.asyncio
async def test_from_custom_id_rebuilds_every_class():
    b = await build(views.SellPctButton, f"rh:sell:{USER}:{PONS}:25")
    assert (b.uid, b.token, b.pct) == (USER, PONS, 25) and b.item.label == "Sell 25%"
    b = await build(views.SellMineButton, f"rh:sellme:{PONS}:100")
    assert (b.token, b.pct) == (PONS, 100) and b.item.label == "Sell all"
    assert b.item.style is discord.ButtonStyle.danger
    b = await build(views.BuyUsdButton, f"rh:buy:{PONS}:2000")
    assert (b.token, b.cents) == (PONS, 2000) and b.item.label == "Buy $20"
    assert b.item.style is discord.ButtonStyle.primary
    b = await build(views.BuyCustomButton, f"rh:buyx:{PONS}")
    assert b.token == PONS and b.item.label == "Other amount"
    b = await build(views.TpSlButton, f"rh:tpsl:{USER}:{PONS}")
    assert (b.uid, b.token) == (USER, PONS) and b.item.label == "TP / SL"
    sel = discord.ui.Select(custom_id="rh:pick:new", options=[discord.SelectOption(label="PONS", value=PONS)])
    s = await build(views.TokenPickSelect, "rh:pick:new", item=sel)
    assert s.kind == "new" and [o.value for o in s.item.options] == [PONS]
    n = await build(views.NavButton, "rh:nav:wallet_create")
    assert n.kind == "wallet_create" and n.item.label == "Create wallet"
    assert n.item.style is discord.ButtonStyle.success
    for child in views.receipt_row(USER, PONS).children:
        again = await build(type(child), child.custom_id)
        assert again.custom_id == child.custom_id


# ---------------- owner binding ----------------


@pytest.mark.asyncio
async def test_owner_bound_items_refuse_a_stranger_without_deferring(have_wallet):
    cog = FakeCog()
    for cls, cid in ((views.SellPctButton, f"rh:sell:{USER}:{PONS}:25"), (views.TpSlButton, f"rh:tpsl:{USER}:{PONS}")):
        item = await build(cls, cid)
        inter = click(cog, user_id=USER + 1)
        assert await item.interaction_check(inter) is False
        assert response_texts(inter) == [views.NOT_YOURS_TEXT]
        assert inter.followup.sent[-1][1]["ephemeral"] is True
        assert inter.response.deferred_ephemeral is None
    assert cog.calls == []


@pytest.mark.asyncio
async def test_owner_passes_the_check_and_alert_sells_have_no_owner(have_wallet):
    item = await build(views.SellPctButton, f"rh:sell:{USER}:{PONS}:25")
    inter = click(FakeCog())
    assert await item.interaction_check(inter) is True
    assert inter.followup.sent == []
    mine = await build(views.SellMineButton, f"rh:sellme:{PONS}:50")
    assert await mine.interaction_check(click(FakeCog(), user_id=USER + 1)) is True


# ---------------- gates before any I/O ----------------


@pytest.mark.asyncio
async def test_non_allowlisted_buy_click_gets_the_lock_refusal_via_response_with_no_defer(have_wallet):
    cog = FakeCog(deny=True)
    item = await build(views.BuyUsdButton, f"rh:buy:{PONS}:500")
    inter = click(cog)
    await item.callback(inter)
    assert response_texts(inter) == ["🔒 You are not on the trader allowlist."]
    assert inter.response.deferred_ephemeral is None
    assert names(cog) == ["deny"] and cog.calls[0] == ("deny", False)


@pytest.mark.asyncio
async def test_clicker_without_wallet_gets_the_nudge_and_no_trade(no_wallet):
    cog = FakeCog()
    for cls, cid in ((views.BuyUsdButton, f"rh:buy:{PONS}:500"), (views.SellMineButton, f"rh:sellme:{PONS}:50"),
                     (views.BuyCustomButton, f"rh:buyx:{PONS}")):
        cog.calls.clear()
        item = await build(cls, cid)
        inter = click(cog)
        await item.callback(inter)
        assert names(cog) == ["deny", "no_wallet"], cid
        assert response_texts(inter) == ["You have no wallet yet."]
        assert inter.response.deferred_ephemeral is None
        assert inter.response.modal is None
        ids = [c.custom_id for c in inter.followup.sent[-1][1]["view"].children]
        assert ids == ["rh:nav:wallet_create", "rh:nav:tutorial"]


@pytest.mark.asyncio
async def test_owner_sell_click_defers_ephemerally_then_runs_the_sell(have_wallet):
    cog = FakeCog()
    item = await build(views.SellPctButton, f"rh:sell:{USER}:{PONS}:25")
    inter = click(cog)
    assert await item.interaction_check(inter)
    await item.callback(inter)
    assert names(cog) == ["deny", "run_sell"]
    assert inter.response.deferred_ephemeral is True
    assert cog.calls[1] == ("run_sell", True, have_wallet, PONS, 25, 200, True, "button")
    assert followup_texts(inter) == ["quote"]


@pytest.mark.asyncio
async def test_alert_sell_uses_the_clickers_wallet(have_wallet):
    cog = FakeCog()
    item = await build(views.SellMineButton, f"rh:sellme:{PONS}:100")
    inter = click(cog)
    await item.callback(inter)
    assert cog.calls[-1] == ("run_sell", True, have_wallet, PONS, 100, 200, True, "button")


@pytest.mark.asyncio
async def test_buy_click_passes_dollars_not_cents(have_wallet):
    cog = FakeCog()
    cog.results_private = False
    item = await build(views.BuyUsdButton, f"rh:buy:{PONS}:2000")
    inter = click(cog)
    await item.callback(inter)
    assert inter.response.deferred_ephemeral is True
    assert cog.calls[-1] == ("run_buy", True, have_wallet, PONS, None, 20.0, 200, False, "button")


@pytest.mark.asyncio
async def test_cents_above_the_cap_are_refused_never_clamped(have_wallet, monkeypatch):
    monkeypatch.setattr(views, "RHC_MAX_TRADE_USD", 50.0)
    cog = FakeCog()
    item = await build(views.BuyUsdButton, f"rh:buy:{PONS}:5001")
    inter = click(cog)
    await item.callback(inter)
    assert names(cog) == ["deny"]
    assert inter.response.deferred_ephemeral is None
    [text] = response_texts(inter)
    assert "$50.01" in text and "$50" in text and "/rh buy" in text
    # exactly the cap is fine
    item = await build(views.BuyUsdButton, f"rh:buy:{PONS}:5000")
    inter = click(cog)
    await item.callback(inter)
    assert cog.calls[-1][0] == "run_buy" and cog.calls[-1][5] == 50.0


# ---------------- modals ----------------


@pytest.mark.asyncio
async def test_tpsl_button_gates_then_opens_the_modal_without_deferring(have_wallet):
    cog = FakeCog()
    item = await build(views.TpSlButton, f"rh:tpsl:{USER}:{PONS}")
    inter = click(cog)
    await item.callback(inter)
    assert names(cog) == ["deny"]
    assert inter.response.deferred_ephemeral is None
    modal = inter.response.modal
    assert isinstance(modal, views.TpSlModal) and (modal.uid, modal.token) == (USER, PONS)
    labels = [(c.text, c.description, c.component.value) for c in modal.children]
    assert labels == [
        ("Take-profit at", "2x, +50%, 900k; blank to skip", "2x"),
        ("Sell this % at take-profit", None, "50"),
        ("Stop-loss at", "-30%, 300k, or trail 25%; blank to skip", "-30%"),
        ("Sell this % at stop-loss", None, "100"),
    ]
    for c in modal.children:
        assert len(c.text) <= 45 and len(c.description or "") <= 100
    assert len(modal.title) <= 45


@pytest.mark.asyncio
async def test_tpsl_modal_submit_defers_then_hands_the_four_texts_to_the_cog(have_wallet):
    cog = FakeCog()
    modal = views.TpSlModal(USER, PONS)
    modal.tp_at.component._value = " 3x "
    modal.sl_at.component._value = ""
    inter = click(cog)
    await modal.on_submit(inter)
    assert inter.response.deferred_ephemeral is True
    assert cog.calls == [("modal_tpsl", True, USER, PONS, "3x", "50", "", "100")]
    assert followup_texts(inter) == ["armed?"]


@pytest.mark.asyncio
async def test_buy_custom_button_opens_the_amount_modal_and_submit_calls_modal_buy(have_wallet):
    cog = FakeCog()
    item = await build(views.BuyCustomButton, f"rh:buyx:{PONS}")
    inter = click(cog)
    await item.callback(inter)
    assert names(cog) == ["deny"] and inter.response.deferred_ephemeral is None
    modal = inter.response.modal
    assert isinstance(modal, views.BuyAmountModal) and modal.token == PONS
    assert [c.text for c in modal.children] == ["USD", "ETH"]
    modal.usd_text.component._value = "7.5"
    inter2 = click(cog)
    await modal.on_submit(inter2)
    assert inter2.response.deferred_ephemeral is True
    assert cog.calls[-1] == ("modal_buy", True, PONS, "7.5", "")


@pytest.mark.asyncio
async def test_modal_submit_with_no_cog_says_not_loaded():
    modal = views.TpSlModal(USER, PONS)
    inter = click(None)
    await modal.on_submit(inter)
    assert response_texts(inter) == [views.NOT_LOADED_TEXT]
    assert inter.response.deferred_ephemeral is None


# ---------------- token picker ----------------


def test_board_view_caps_at_25_options_and_describes_each():
    toks = [SimpleNamespace(symbol=f"T{i}", address=f"0x{i:040x}", mc_usd=1_000_000 + i, liq_usd=50_000) for i in range(40)]
    v = views.board_view("trending", toks)
    [sel] = v.children
    assert isinstance(sel, views.TokenPickSelect) and sel.custom_id == "rh:pick:trending"
    opts = sel.item.options
    assert len(opts) == 25
    assert opts[0].label == "T0" and opts[0].value == toks[0].address
    assert opts[0].description == "$1M MC · quiet 5m · liq $50K"
    assert sel.item.placeholder == "Pick a token to buy"
    assert all(len(o.label) <= 100 and len(o.description) <= 100 for o in opts)


class _Row(SimpleNamespace):
    """A stand-in for rhchain.TokenActivity with the signals the picker reads."""

    def off_high(self):
        return self._off_high

    def depth(self):
        return self._depth

    def buyers(self, window="m5"):
        return self._buyers

    def sells(self, window="m5"):
        return self._sells


def test_the_picker_leads_with_how_far_a_token_is_off_its_high():
    row = _Row(symbol="DIP", address=PONS, mc_usd=2_000_000, liq_usd=120_000, created_ts=0.0,
               _off_high=-38.4, _depth=6.0, _buyers=12, _sells=4)
    [sel] = views.board_view("trending", [row]).children
    [opt] = sel.item.options
    assert opt.label == "DIP  -38.4% off high"
    assert opt.description == "$2M MC · 12 buyers 5m · liq $120K (6% of cap)"


def test_a_brand_new_pair_shows_buy_sell_pressure_and_age():
    row = _Row(symbol="FRESH", address=PONS, mc_usd=90_000, liq_usd=15_000,
               created_ts=time.time() - 600, _off_high=None, _depth=None, _buyers=9, _sells=3)
    [sel] = views.board_view("new", [row]).children
    [opt] = sel.item.options
    assert opt.label == "FRESH"
    assert opt.description == "$90K MC · 9 buyers 5m · 9/3 buy/sell · liq $15K · 10m old"


def test_a_row_whose_signals_raise_still_gets_a_picker_option():
    class Broken(SimpleNamespace):
        def off_high(self):
            raise RuntimeError("no market data")

        def depth(self):
            raise RuntimeError("no market data")

        def buyers(self, window="m5"):
            raise RuntimeError("no market data")

        def sells(self, window="m5"):
            raise RuntimeError("no market data")

    [sel] = views.board_view("trending", [Broken(symbol="OOPS", address=PONS, mc_usd=1_000, liq_usd=None)]).children
    [opt] = sel.item.options
    assert opt.label == "OOPS" and opt.value == PONS


def test_board_view_skips_non_evm_addresses_and_duplicates():
    toks = [SimpleNamespace(symbol="SOL", address=SOLANA, mc_usd=1, liq_usd=1),
            SimpleNamespace(symbol="PONS", address=PONS, mc_usd=1, liq_usd=1),
            SimpleNamespace(symbol="PONS2", address=PONS.upper().replace("0X", "0x"), mc_usd=1, liq_usd=None)]
    [sel] = views.board_view("new", toks).children
    assert [o.label for o in sel.item.options] == ["PONS"]
    assert views.board_view("new", [toks[0]]) is None


@pytest.mark.asyncio
async def test_token_pick_rejects_a_non_address_value():
    sel = discord.ui.Select(custom_id="rh:pick:trending", options=[discord.SelectOption(label="SOL", value=SOLANA)])
    item = await build(views.TokenPickSelect, "rh:pick:trending", item=sel)
    inter = click(FakeCog())
    inter.data = {"values": [SOLANA]}
    item._refresh_state(inter, inter.data)
    await item.callback(inter)
    assert response_texts(inter) == [views.NOT_A_TOKEN_TEXT]
    assert inter.followup.sent[-1][1].get("view") is None


@pytest.mark.asyncio
async def test_token_pick_opens_the_size_card_with_the_options_label(monkeypatch):
    monkeypatch.setattr(views, "RHC_BUTTON_USD_SIZES", [5.0, 20.0])
    monkeypatch.setattr(views, "RHC_MAX_TRADE_USD", 50.0)
    sel = discord.ui.Select(custom_id="rh:pick:trending", options=[
        discord.SelectOption(label="OTHER", value=OTHER), discord.SelectOption(label="PONS", value=PONS)])
    item = await build(views.TokenPickSelect, "rh:pick:trending", item=sel)
    inter = click(None)                       # no cog needed: picking only opens a card
    inter.data = {"values": [PONS]}
    item._refresh_state(inter, inter.data)
    await item.callback(inter)
    [(text, kw)] = inter.followup.sent
    assert kw["via"] == "response" and kw["ephemeral"] is True
    assert text == f"**PONS** · `{PONS}`\nHow much?"
    ids = [c.custom_id for c in kw["view"].children]
    assert ids == [f"rh:buy:{PONS}:500", f"rh:buy:{PONS}:2000", f"rh:buyx:{PONS}"]
    assert [c.item.label for c in kw["view"].children] == ["Buy $5", "Buy $20", "Other amount"]


# ---------------- nav ----------------


@pytest.mark.asyncio
async def test_nav_wallet_create_gates_then_defers_then_creates(no_wallet):
    cog = FakeCog()
    item = await build(views.NavButton, "rh:nav:wallet_create")
    inter = click(cog)
    await item.callback(inter)
    assert cog.calls == [("deny", False), ("create_wallet", True, True)]
    assert inter.response.deferred_ephemeral is True
    denied = FakeCog(deny=True)
    inter = click(denied)
    await item.callback(inter)
    assert names(denied) == ["deny"] and inter.response.deferred_ephemeral is None
    assert response_texts(inter) == ["🔒 You are not on the trader allowlist."]


@pytest.mark.asyncio
async def test_nav_show_trending_tutorial_delegate_without_deferring():
    cog = FakeCog()
    for kind, name in (("wallet_show", "wallet_show"), ("trending", "trending"), ("tutorial", "tutorial")):
        item = await build(views.NavButton, f"rh:nav:{kind}")
        inter = click(cog)
        await item.callback(inter)
        assert cog.calls[-1] == (name, False)
        assert inter.response.deferred_ephemeral is None
        assert len(inter.followup.sent) == 1


# ---------------- failure handling ----------------


@pytest.mark.asyncio
async def test_missing_cog_says_not_loaded_for_every_button(have_wallet):
    for cls, cid in GOOD_IDS.items():
        if cls is views.TokenPickSelect:
            continue
        item = await build(cls, cid)
        inter = click(None)
        await item.callback(inter)
        assert response_texts(inter) == [views.NOT_LOADED_TEXT], cid
        assert inter.response.deferred_ephemeral is None
        assert len(inter.followup.sent) == 1


@pytest.mark.asyncio
async def test_a_raising_cog_method_after_the_defer_reports_once_via_followup(have_wallet):
    cog = FakeCog(boom="_run_sell")
    item = await build(views.SellPctButton, f"rh:sell:{USER}:{PONS}:50")
    inter = click(cog)
    await item.callback(inter)
    assert inter.response.deferred_ephemeral is True
    assert followup_texts(inter) == [views.BROKE_TEXT]
    assert inter.followup.sent[-1][1]["ephemeral"] is True
    assert len(inter.followup.sent) == 1


@pytest.mark.asyncio
async def test_a_raising_gate_before_the_defer_reports_once_via_response(have_wallet):
    cog = FakeCog(boom="_deny_trade")
    item = await build(views.BuyUsdButton, f"rh:buy:{PONS}:500")
    inter = click(cog)
    await item.callback(inter)
    assert response_texts(inter) == [views.BROKE_TEXT]
    assert inter.response.deferred_ephemeral is None
    assert len(inter.followup.sent) == 1
    assert names(cog) == ["deny"]


@pytest.mark.asyncio
async def test_a_raising_modal_handler_reports_once(have_wallet):
    cog = FakeCog()

    async def boom(*a, **k):
        raise RuntimeError("no")
    cog.modal_tpsl = boom
    inter = click(cog)
    await views.TpSlModal(USER, PONS).on_submit(inter)
    assert followup_texts(inter) == [views.BROKE_TEXT] and len(inter.followup.sent) == 1


# ---------------- factories ----------------


def test_receipt_and_partial_rows_have_the_designed_buttons():
    v = views.receipt_row(USER, PONS)
    assert [c.custom_id for c in v.children] == [
        f"rh:sell:{USER}:{PONS}:25", f"rh:sell:{USER}:{PONS}:50", f"rh:sell:{USER}:{PONS}:100", f"rh:tpsl:{USER}:{PONS}"]
    assert [c.item.label for c in v.children] == ["Sell 25%", "Sell 50%", "Sell all", "TP / SL"]
    assert [c.item.style for c in v.children] == [
        discord.ButtonStyle.secondary, discord.ButtonStyle.secondary, discord.ButtonStyle.danger, discord.ButtonStyle.secondary]
    p = views.partial_sell_row(USER, PONS)
    assert [c.custom_id for c in p.children] == [f"rh:sell:{USER}:{PONS}:100", f"rh:tpsl:{USER}:{PONS}"]
    assert [c.item.label for c in p.children] == ["Sell rest", "TP / SL"]


def test_alert_row_and_size_card_follow_the_ladder_under_the_cap(monkeypatch):
    monkeypatch.setattr(views, "RHC_BUTTON_USD_SIZES", [5.0, 20.0, 75.0, 40.0])
    monkeypatch.setattr(views, "RHC_MAX_TRADE_USD", 50.0)
    a = views.alert_row(PONS)
    assert [c.custom_id for c in a.children] == [
        f"rh:buy:{PONS}:500", f"rh:buy:{PONS}:2000", f"rh:sellme:{PONS}:50", f"rh:sellme:{PONS}:100",
        f"rh:share:{PONS}"]
    assert [c.item.label for c in a.children] == ["Buy $5", "Buy $20", "Sell 50%", "Sell all", "📤 Share"]
    s = views.size_card(PONS)
    assert [c.custom_id for c in s.children] == [
        f"rh:buy:{PONS}:500", f"rh:buy:{PONS}:2000", f"rh:buy:{PONS}:4000", f"rh:buyx:{PONS}"]


def test_nav_rows_per_state():
    ids = lambda v: [c.custom_id for c in v.children]
    assert ids(views.wallet_nudge()) == ["rh:nav:wallet_create", "rh:nav:tutorial"]
    assert ids(views.after_create_row()) == ["rh:nav:trending", "rh:nav:tutorial"]
    assert ids(views.tutorial_row("no_wallet")) == ["rh:nav:wallet_create", "rh:nav:trending"]
    assert ids(views.tutorial_row("public")) == ["rh:nav:wallet_create", "rh:nav:trending"]
    assert ids(views.tutorial_row("wallet")) == ["rh:nav:wallet_show", "rh:nav:trending"]
    assert views.tutorial_row("none") is None
    labels = [c.item.label for c in views.wallet_nudge().children] + [c.item.label for c in views.tutorial_row("wallet").children]
    assert labels == ["Create wallet", "How it works", "My wallet", "Trending now"]


def test_every_factory_returns_none_on_bad_input():
    assert views.receipt_row(USER, SOLANA) is None
    assert views.receipt_row("seven", PONS) is None
    assert views.receipt_row(0, PONS) is None
    assert views.partial_sell_row(USER, "") is None
    assert views.partial_sell_row(None, PONS) is None
    assert views.size_card(SOLANA) is None
    assert views.size_card(None) is None
    assert views.alert_row("0x123") is None
    assert views.board_view("hot", []) is None
    assert views.board_view("trending", []) is None
    assert views.board_view("trending", None) is None
    assert views.tutorial_row("bogus") is None
    assert views.tutorial_row(None) is None


def test_views_module_imports_stay_web3_free():
    """alerts.py imports views; the module-level imports must never pull in the chain, Kyber, swap, trade or a cog."""
    import ast
    tree = ast.parse(open(views.__file__, encoding="utf-8").read())
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    modules = set()
    for n in top:
        if isinstance(n, ast.Import):
            modules.update(a.name for a in n.names)
        else:
            modules.add(n.module or "")
            modules.update(f"{n.module or ''}.{a.name}" for a in n.names)
    for banned in ("chain", "kyber", "swap", "trade", "cogs", "wallets", "web3"):
        assert not any(banned in m for m in modules), (banned, modules)
    assert modules <= {"re", "time", "typing", "discord", "config", "helpers", "logging_setup"} | {
        m for m in modules if m.startswith(("typing.", "config.", "helpers.", "logging_setup."))}


# ---------------- voting on a call ----------------


@pytest.mark.asyncio
async def test_a_vote_reaches_the_cog_with_the_call_it_belongs_to():
    cog = FakeCog()
    item = await build(views.FeedVoteButton, "rh:vote:a1b2c3:down")
    inter = click(cog)
    await item.callback(inter)
    assert cog.calls == [("button_feed_vote", ("a1b2c3", "down"))]


def test_the_vote_row_carries_its_tally_in_the_labels():
    """The point of a vote is that the next person to look can see it."""
    v = views.feed_row(PONS, "a1b2c3", 3, 1)
    votes = [c for c in v.children if isinstance(c, views.FeedVoteButton)]
    assert [c.item.label for c in votes] == ["👍 3", "👎 1"]
    assert all(c.item.row == 1 for c in votes), "votes sit under the trade row, not in it"
    assert [c.custom_id for c in votes] == ["rh:vote:a1b2c3:up", "rh:vote:a1b2c3:down"]


def test_an_unvoted_call_shows_bare_thumbs():
    v = views.feed_row(PONS, "a1b2c3")
    assert [c.item.label for c in v.children if isinstance(c, views.FeedVoteButton)] == ["👍", "👎"]


def test_a_token_mccap_cannot_trade_still_gets_its_votes():
    """Whether a call was worth posting is a separate question from whether
    this particular wallet can act on it."""
    v = views.feed_row(PONS, "a1b2c3", trade=False)
    kinds = [type(c).__name__ for c in v.children]
    assert kinds == ["FeedVoteButton", "FeedVoteButton", "ShareButton"]


def test_the_vote_row_fits_discords_five_per_row_limit():
    v = views.feed_row(PONS, "a1b2c3", 12, 34)
    rows = {}
    for c in v.children:
        rows.setdefault(c.item.row, []).append(c)
    assert all(len(items) <= 5 for items in rows.values()), rows
    assert all(len(c.item.label) <= 80 for c in v.children)


@pytest.mark.asyncio
async def test_the_share_button_hands_the_cog_the_token_it_sits_under():
    cog = FakeCog()
    item = await build(views.ShareButton, f"rh:share:{PONS}")
    inter = click(cog)
    await item.callback(inter)
    assert cog.calls == [("button_share", PONS)]
