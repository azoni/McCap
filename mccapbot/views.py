"""Shared Discord UI pieces: the Confirm prompt, the persistent trade buttons and their modals.

Lives outside ``mccapbot.cogs`` on purpose: discord.py purges an extension's
module from ``sys.modules`` when it is unloaded, so anything imported lazily
from a cog module can silently resolve to a different object than the one a
test patched. A plain module is never purged.

Permission model, in one sentence: buttons that act on a position answer only
to its owner; buttons that open a position act on whoever clicks, with the
clicker's own wallet, allowlist and caps.

What this module must never do:

* Move money or quote a trade itself. Every trade button lands on the cog's
  ``_run_buy`` / ``_run_sell`` (the same private quote and Confirm prompt the
  slash command shows) or on a modal whose submit hands off to the cog. Nothing
  here imports ``rhc.chain``, ``rhc.kyber``, ``rhc.swap`` or ``rhc.trade``.
* Import a cog module or hold a cog reference. The cog is looked up at click
  time with ``inter.client.get_cog("RhcCog")`` so extension reloads are safe.
* Keep state outside the ``custom_id``. Views made only of ``DynamicItem``
  subclasses are fully persistent: no ``add_view``, no message ids, a restart
  loses nothing. ``from_custom_id`` does no I/O.
* Leave an interaction unanswered. Every callback body runs inside ``_safe``,
  which answers exactly once even when the cog raises (the view store swallows
  DynamicItem exceptions and there is no ``on_error``).
* Drag web3 into ``alerts.py``: ``rhc.wallets`` is imported lazily inside the
  one helper that needs it, so importing this module stays cheap and pure.
"""

import re
import time
from typing import Any, Callable, Coroutine, List, Optional, Sequence

import discord

from .config import RHC_BUTTON_USD_SIZES, RHC_MAX_TRADE_USD
from .helpers import SEP, age, footer, is_evm_address, pct, usd
from .logging_setup import log


class ConfirmOrder(discord.ui.View):
    """A single-use confirm/cancel prompt, bound to one user."""

    def __init__(self, owner_id: int, timeout: int):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.value: Optional[bool] = None

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        # Buttons are visible to whoever can see the message; bind them to the
        # owner so nobody else can press Confirm.
        if inter.user.id != self.owner_id:
            await inter.response.send_message("This isn't your order.", ephemeral=True)
            return False
        return True

    # stop() runs in a finally: if editing the message fails, the waiter must
    # still wake up now. Otherwise it wakes at the timeout with value already
    # set and the order executes a minute after the click.
    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, _b: discord.ui.Button):
        self.value = True
        try:
            for child in self.children:
                child.disabled = True
            await inter.response.edit_message(view=self)
        finally:
            self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, _b: discord.ui.Button):
        self.value = False
        try:
            for child in self.children:
                child.disabled = True
            await inter.response.edit_message(view=self)
        finally:
            self.stop()


# ---------------- texts ----------------

NOT_LOADED_TEXT = "Trading is not loaded right now; try the slash command in a minute."
NOT_YOURS_TEXT = "This isn't your position."
BROKE_TEXT = "❌ Something broke handling that button; nothing was sent. Try the slash command."
NOT_A_TOKEN_TEXT = "That option is not a Robinhood Chain token."

_EVM = r"0x[0-9a-fA-F]{40}"
_ADDRESS_RE = re.compile(_EVM)
COG_NAME = "RhcCog"


# ---------------- plumbing ----------------

async def _private(inter: discord.Interaction, text: str, view: Optional[discord.ui.View] = None) -> None:
    """One private message, whether or not the interaction has been answered yet."""
    kw: dict = {"ephemeral": True}
    if view is not None:
        kw["view"] = view
    if inter.response.is_done():
        await inter.followup.send(text, **kw)
    else:
        await inter.response.send_message(text, **kw)


async def _safe(inter: discord.Interaction, body: Callable[[], Coroutine[Any, Any, None]], what: str) -> None:
    """Run a callback body; on any exception log it and answer the interaction once.

    discord.py's ViewStore logs and drops exceptions from DynamicItem callbacks and
    there is no ``on_error`` hook, so without this a broken button would leave the
    user staring at "This interaction failed".
    """
    try:
        await body()
    except Exception:
        log.exception("Button handler failed: %s (user %s)", what, getattr(getattr(inter, "user", None), "id", "?"))
        try:
            await _private(inter, BROKE_TEXT)
        except Exception:
            log.exception("Could not even report the button failure for %s", what)


def _cog(inter: discord.Interaction):
    """The trading cog, or None when the extension is not loaded."""
    client = getattr(inter, "client", None)
    getter = getattr(client, "get_cog", None)
    return getter(COG_NAME) if getter else None


def _wallet(user_id: int):
    """The clicker's Wallet or None. Imported lazily so alerts.py never loads eth_account through here."""
    from .rhc import wallets
    return wallets.get(user_id)


async def _prelude(inter: discord.Interaction):
    """Steps 2-4 of the callback contract: cog present, allowed to trade here, has a wallet.

    Returns ``(cog, wallet)`` or None after having answered the interaction.
    Nothing here defers, so refusals stay private even when results are public.
    """
    cog = _cog(inter)
    if cog is None:
        await _private(inter, NOT_LOADED_TEXT)
        return None
    if await cog._deny_trade(inter):
        return None
    w = _wallet(inter.user.id)
    if w is None:
        await cog._no_wallet(inter)
        return None
    return cog, w


async def _owner_check(inter: discord.Interaction, uid: int) -> bool:
    """Owner-bound items: the clicker must be the position's owner. Never defers."""
    if inter.user.id != uid:
        await inter.response.send_message(NOT_YOURS_TEXT, ephemeral=True)
        return False
    return True


async def _sell_flow(inter: discord.Interaction, token: str, pct: int) -> None:
    got = await _prelude(inter)
    if got is None:
        return
    cog, w = got
    await inter.response.defer(thinking=True, ephemeral=True)
    await cog._run_sell(inter, w, token, pct, bps=cog.default_slippage(), priv=cog.results_private, source="button")


async def _buy_flow(inter: discord.Interaction, token: str, cents: int) -> None:
    got = await _prelude(inter)
    if got is None:
        return
    cog, w = got
    size = cents / 100
    if size > RHC_MAX_TRADE_USD:
        # Never clamp: the user must see the real amount the button carries.
        await _private(
            inter,
            f"That button's size ({usd(size)}) is above the per-trade cap ({usd(RHC_MAX_TRADE_USD)}); "
            f"use `/rh buy` with a smaller amount.",
        )
        return
    await inter.response.defer(thinking=True, ephemeral=True)
    await cog._run_buy(inter, w, token, eth=None, usd=size, bps=cog.default_slippage(),
                       priv=cog.results_private, source="button")


def _sell_label(pct: int) -> str:
    return "Sell all" if pct >= 100 else f"Sell {pct}%"


def _sell_style(pct: int) -> discord.ButtonStyle:
    return discord.ButtonStyle.danger if pct >= 100 else discord.ButtonStyle.secondary


def _cents(size: float) -> int:
    return int(round(float(size) * 100))


def _buy_label(size: float) -> str:
    """"Buy $5" / "Buy $20" / "Buy $7.50": helpers.usd, minus the ".00" a button has no room for."""
    text = usd(size)
    return f"Buy {text[:-3] if text.endswith('.00') else text}"


# ---------------- dynamic items ----------------

class SellPctButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=rf"rh:sell:(?P<uid>\d{{1,20}}):(?P<token>{_EVM}):(?P<pct>\d{{1,3}})"):
    """Sell a percentage of the owner's position. Owner-bound: sits under a receipt."""

    def __init__(self, uid: int, token: str, pct: int, label: Optional[str] = None):
        self.uid, self.token, self.pct = int(uid), token, int(pct)
        super().__init__(discord.ui.Button(
            label=label or _sell_label(self.pct), style=_sell_style(self.pct),
            custom_id=f"rh:sell:{self.uid}:{token}:{self.pct}",
        ))

    @classmethod
    async def from_custom_id(cls, inter: discord.Interaction, item: discord.ui.Button, match: re.Match, /):
        return cls(int(match["uid"]), match["token"], int(match["pct"]))

    async def interaction_check(self, inter: discord.Interaction, /) -> bool:
        return await _owner_check(inter, self.uid)

    async def callback(self, inter: discord.Interaction) -> None:
        await _safe(inter, lambda: _sell_flow(inter, self.token, self.pct), self.custom_id)


class SellMineButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=rf"rh:sellme:(?P<token>{_EVM}):(?P<pct>\d{{1,3}})"):
    """Sell a percentage of whatever the clicker holds. Sits under an alert; anyone may press it."""

    def __init__(self, token: str, pct: int):
        self.token, self.pct = token, int(pct)
        super().__init__(discord.ui.Button(
            label=_sell_label(self.pct), style=_sell_style(self.pct), custom_id=f"rh:sellme:{token}:{self.pct}",
        ))

    @classmethod
    async def from_custom_id(cls, inter: discord.Interaction, item: discord.ui.Button, match: re.Match, /):
        return cls(match["token"], int(match["pct"]))

    async def callback(self, inter: discord.Interaction) -> None:
        await _safe(inter, lambda: _sell_flow(inter, self.token, self.pct), self.custom_id)


class BuyUsdButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=rf"rh:buy:(?P<token>{_EVM}):(?P<cents>\d{{1,7}})"):
    """Buy a fixed dollar amount with the clicker's wallet. Any size is accepted in the id;
    anything above the per-trade cap is refused at click time, never clamped."""

    def __init__(self, token: str, cents: int):
        self.token, self.cents = token, int(cents)
        super().__init__(discord.ui.Button(
            label=_buy_label(self.cents / 100), style=discord.ButtonStyle.primary,
            custom_id=f"rh:buy:{token}:{self.cents}",
        ))

    @classmethod
    async def from_custom_id(cls, inter: discord.Interaction, item: discord.ui.Button, match: re.Match, /):
        return cls(match["token"], int(match["cents"]))

    async def callback(self, inter: discord.Interaction) -> None:
        await _safe(inter, lambda: _buy_flow(inter, self.token, self.cents), self.custom_id)


class BuyCustomButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"rh:buyx:(?P<token>{_EVM})"):
    """Opens the amount modal. Gates first; a modal cannot follow a defer, so this branch never defers."""

    def __init__(self, token: str):
        self.token = token
        super().__init__(discord.ui.Button(
            label="Other amount", style=discord.ButtonStyle.secondary, custom_id=f"rh:buyx:{token}",
        ))

    @classmethod
    async def from_custom_id(cls, inter: discord.Interaction, item: discord.ui.Button, match: re.Match, /):
        return cls(match["token"])

    async def callback(self, inter: discord.Interaction) -> None:
        async def body():
            if await _prelude(inter) is None:
                return
            await inter.response.send_modal(BuyAmountModal(self.token))
        await _safe(inter, body, self.custom_id)


class TpSlButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=rf"rh:tpsl:(?P<uid>\d{{1,20}}):(?P<token>{_EVM})"):
    """Opens the take-profit / stop-loss modal for the owner's position. Owner-bound, never defers."""

    def __init__(self, uid: int, token: str):
        self.uid, self.token = int(uid), token
        super().__init__(discord.ui.Button(
            label="TP / SL", style=discord.ButtonStyle.secondary, custom_id=f"rh:tpsl:{self.uid}:{token}",
        ))

    @classmethod
    async def from_custom_id(cls, inter: discord.Interaction, item: discord.ui.Button, match: re.Match, /):
        return cls(int(match["uid"]), match["token"])

    async def interaction_check(self, inter: discord.Interaction, /) -> bool:
        return await _owner_check(inter, self.uid)

    async def callback(self, inter: discord.Interaction) -> None:
        async def body():
            got = await _prelude(inter)
            if got is None:
                return
            cog, _w = got
            # Auto-orders switched off, or the rule limits reached: say so now,
            # not after the person has filled in four fields.
            limits = getattr(cog, "_auto_limits", None)
            if limits is not None and await limits(inter):
                return
            await inter.response.send_modal(TpSlModal(self.uid, self.token))
        await _safe(inter, body, self.custom_id)


class TokenPickSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"rh:pick:(?P<kind>trending|new)"):
    """The picker under a trending / new board. No gate here: picking only opens a size card;
    the buy click gates. Option values are the board's addresses as given (re-resolved on the buy)."""

    def __init__(self, kind: str, options: Optional[Sequence[discord.SelectOption]] = None):
        self.kind = kind
        opts = list(options) if options else []
        select = discord.ui.Select(custom_id=f"rh:pick:{kind}", placeholder="Pick a token to buy",
                                   min_values=1, max_values=1, options=opts)
        super().__init__(select)

    @classmethod
    async def from_custom_id(cls, inter: discord.Interaction, item: discord.ui.Select, match: re.Match, /):
        # The base item is the Select from the message, options included, so the
        # chosen option's label is available without any lookup.
        return cls(match["kind"], getattr(item, "options", None))

    def _label_for(self, value: str) -> str:
        for opt in self.item.options:
            if opt.value == value:
                return opt.label
        return f"{value[:6]}…{value[-4:]}"

    async def callback(self, inter: discord.Interaction) -> None:
        async def body():
            values = list(self.item.values or [])
            if not values:
                values = list((getattr(inter, "data", None) or {}).get("values") or [])
            value = str(values[0]).strip() if values else ""
            if not _ADDRESS_RE.fullmatch(value):
                await _private(inter, NOT_A_TOKEN_TEXT)
                return
            # Picking a token is the moment somebody is deciding, so the cog
            # spends a few requests on the full card. Without the cog loaded
            # the buttons still work; only the context is missing.
            cog = _cog(inter)
            show = getattr(cog, "button_token_card", None) if cog is not None else None
            if show is not None:
                await show(inter, value, self._label_for(value))
                return
            await inter.response.send_message(
                f"**{self._label_for(value)}**{SEP}`{value}`\nHow much?", view=size_card(value), ephemeral=True,
            )
        await _safe(inter, body, self.custom_id)


_NAV = {
    "wallet_create": ("Create wallet", discord.ButtonStyle.success),
    "wallet_show": ("My wallet", discord.ButtonStyle.secondary),
    "trending": ("Trending now", discord.ButtonStyle.primary),
    "tutorial": ("How it works", discord.ButtonStyle.secondary),
}


class NavButton(discord.ui.DynamicItem[discord.ui.Button],
                template=r"rh:nav:(?P<kind>wallet_create|wallet_show|trending|tutorial)"):
    """Navigation: create a wallet, show it, the trending board, the tutorial. Acts on the clicker."""

    def __init__(self, kind: str):
        if kind not in _NAV:
            raise ValueError(f"unknown nav kind {kind!r}")
        self.kind = kind
        label, style = _NAV[kind]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"rh:nav:{kind}"))

    @classmethod
    async def from_custom_id(cls, inter: discord.Interaction, item: discord.ui.Button, match: re.Match, /):
        return cls(match["kind"])

    async def callback(self, inter: discord.Interaction) -> None:
        async def body():
            cog = _cog(inter)
            if cog is None:
                await _private(inter, NOT_LOADED_TEXT)
                return
            if self.kind == "wallet_create":
                if await cog._deny_trade(inter):
                    return
                await inter.response.defer(thinking=True, ephemeral=True)
                await cog._create_wallet(inter, cog.results_private)
            elif self.kind == "wallet_show":
                await cog.button_wallet_show(inter)
            elif self.kind == "trending":
                await cog.button_trending(inter)
            else:
                await cog.button_tutorial(inter)
        await _safe(inter, body, self.custom_id)


class ShareButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=rf"rh:share:(?P<token>{_EVM})"):
    """Drop the bare contract address into the server's share channel.

    The message is the address and nothing else, on purpose: the token bots
    that live in those channels trigger on a plain address, and anything
    wrapped around it — a name, a backtick, a "shared by" — is what stops them
    firing. McCap's own context is already in the post this button sits under.
    """

    def __init__(self, token: str):
        self.token = token
        super().__init__(discord.ui.Button(
            label="📤 Share", style=discord.ButtonStyle.secondary,
            custom_id=f"rh:share:{token}", row=1,
        ))

    @classmethod
    async def from_custom_id(cls, inter: discord.Interaction, item: discord.ui.Button, match: re.Match, /):
        return cls(match["token"])

    async def callback(self, inter: discord.Interaction) -> None:
        await _safe(inter, lambda: _share_flow(inter, self.token), self.custom_id)


async def _share_flow(inter: discord.Interaction, token: str) -> None:
    cog = _cog(inter)
    if cog is None:
        await _private(inter, NOT_LOADED_TEXT)
        return
    await cog.button_share(inter, token)


class FeedVoteButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"rh:vote:(?P<eid>[0-9a-f]{6}):(?P<dir>up|down)"):
    """👍 / 👎 under a feed post: was this call worth making?

    Anyone may press it, and pressing the same side twice takes the vote back.
    It is the one thing the tracker cannot measure for itself — a tokenized
    stock drifting 2% and a rug that has not dumped yet both look fine on
    price — so this is deliberately cheap to give and cheap to change.
    """

    def __init__(self, eid: str, direction: str, count: int = 0):
        self.eid, self.direction, self.count = eid, direction, max(0, int(count))
        up = direction == "up"
        super().__init__(discord.ui.Button(
            label=f"{'👍' if up else '👎'}{f' {self.count}' if self.count else ''}",
            style=discord.ButtonStyle.success if up else discord.ButtonStyle.secondary,
            custom_id=f"rh:vote:{eid}:{direction}", row=1,
        ))

    @classmethod
    async def from_custom_id(cls, inter: discord.Interaction, item: discord.ui.Button, match: re.Match, /):
        return cls(match["eid"], match["dir"])

    async def callback(self, inter: discord.Interaction) -> None:
        await _safe(inter, lambda: _vote_flow(inter, self.eid, self.direction), self.custom_id)


async def _vote_flow(inter: discord.Interaction, eid: str, direction: str) -> None:
    cog = _cog(inter)
    if cog is None:
        await _private(inter, NOT_LOADED_TEXT)
        return
    await cog.button_feed_vote(inter, eid, direction)


DYNAMIC_ITEMS = (SellPctButton, SellMineButton, BuyUsdButton, BuyCustomButton, TpSlButton, TokenPickSelect,
                 NavButton, FeedVoteButton, ShareButton)


# ---------------- modals ----------------

MODAL_TIMEOUT = 15 * 60


def _text(label: discord.ui.Label) -> str:
    """The stripped text a user typed into a labelled TextInput."""
    return (label.component.value or "").strip()


class BuyAmountModal(discord.ui.Modal, title="Buy: how much?"):
    """Pick a dollar or ETH amount; exactly one. The cog's ``modal_buy`` does the XOR check and the quote."""

    usd_text = discord.ui.Label(
        text="USD", description="Leave blank to size the buy in ETH instead",
        component=discord.ui.TextInput(placeholder="5", required=False, max_length=16),
    )
    eth_text = discord.ui.Label(
        text="ETH", description="Leave blank to size the buy in dollars instead",
        component=discord.ui.TextInput(placeholder="0.002", required=False, max_length=24),
    )

    def __init__(self, token: str):
        # Discord accepts a modal submit for 15 minutes at most; the timeout only
        # evicts a dismissed modal from the view store (Discord never says a
        # modal was closed), so a long-lived bot does not keep every one forever.
        super().__init__(timeout=MODAL_TIMEOUT)
        self.token = token

    async def on_submit(self, inter: discord.Interaction, /) -> None:
        async def body():
            cog = _cog(inter)
            if cog is None:
                await _private(inter, NOT_LOADED_TEXT)
                return
            await inter.response.defer(thinking=True, ephemeral=True)
            await cog.modal_buy(inter, self.token, _text(self.usd_text), _text(self.eth_text))
        await _safe(inter, body, f"buy modal {self.token}")


class TpSlModal(discord.ui.Modal, title="Take-profit and stop-loss"):
    """Arm up to two one-shot sell rules behind one Confirm. Blank a trigger to skip that side.

    The cog's ``modal_tpsl`` parses the four texts, shows one Confirm and saves both rules at once.
    """

    tp_at = discord.ui.Label(
        text="Take-profit at", description="2x, +50%, 900k; blank to skip",
        component=discord.ui.TextInput(default="2x", required=False, max_length=16),
    )
    tp_pct = discord.ui.Label(
        text="Sell this % at take-profit",
        component=discord.ui.TextInput(default="50", required=True, max_length=3),
    )
    sl_at = discord.ui.Label(
        text="Stop-loss at", description="-30%, 300k, or trail 25%; blank to skip",
        component=discord.ui.TextInput(default="-30%", required=False, max_length=16),
    )
    sl_pct = discord.ui.Label(
        text="Sell this % at stop-loss",
        component=discord.ui.TextInput(default="100", required=True, max_length=3),
    )

    def __init__(self, uid: int, token: str):
        super().__init__(timeout=MODAL_TIMEOUT)   # see BuyAmountModal
        self.uid, self.token = int(uid), token

    async def on_submit(self, inter: discord.Interaction, /) -> None:
        async def body():
            cog = _cog(inter)
            if cog is None:
                await _private(inter, NOT_LOADED_TEXT)
                return
            await inter.response.defer(thinking=True, ephemeral=True)
            await cog.modal_tpsl(
                inter, self.uid, self.token,
                _text(self.tp_at), _text(self.tp_pct), _text(self.sl_at), _text(self.sl_pct),
            )
        await _safe(inter, body, f"tpsl modal {self.uid}:{self.token}")


# ---------------- view factories ----------------

def _factory(fn):
    """A factory returns a persistent View, or None when its input is bad or anything raises.

    A view bug must never take a receipt, an alert or a board down with it: the
    caller sends the message without buttons instead.
    """
    def wrapper(*args, **kwargs) -> Optional[discord.ui.View]:
        try:
            return fn(*args, **kwargs)
        except Exception:
            log.exception("View factory %s failed for %r %r", fn.__name__, args, kwargs)
            return None
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _view(*items: discord.ui.Item) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for it in items:
        v.add_item(it)
    return v


def _require_token(token: str) -> str:
    if not is_evm_address(token):
        raise ValueError(f"not a Robinhood Chain token address: {token!r}")
    return token


def _require_uid(uid) -> int:
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise ValueError(f"not a user id: {uid!r}")
    return uid


def _ladder() -> List[float]:
    """The buy sizes offered as buttons, in configured order, never above the per-trade cap.

    De-duplicated by the cents that end up in the custom_id: Discord rejects a
    whole message whose components share an id, which would silence every
    alert on the chain for one careless config value.
    """
    out: List[float] = []
    seen = set()
    for s in RHC_BUTTON_USD_SIZES:
        size = float(s)
        cents = _cents(size)
        if 0 < size <= RHC_MAX_TRADE_USD and cents not in seen:
            seen.add(cents)
            out.append(size)
    return out


@_factory
def receipt_row(uid: int, token: str) -> discord.ui.View:
    """Under a buy receipt: [Sell 25%] [Sell 50%] [Sell all] [TP / SL], all owner-bound."""
    uid, token = _require_uid(uid), _require_token(token)
    return _view(SellPctButton(uid, token, 25), SellPctButton(uid, token, 50), SellPctButton(uid, token, 100),
                 TpSlButton(uid, token))


@_factory
def partial_sell_row(uid: int, token: str) -> discord.ui.View:
    """Under a partial sell receipt: [Sell rest] [TP / SL]. A 100% sell gets no view at all."""
    uid, token = _require_uid(uid), _require_token(token)
    return _view(SellPctButton(uid, token, 100, label="Sell rest"), TpSlButton(uid, token))


def _call(t, name: str, *args, default=None):
    """A TokenActivity method if this row has one; boards and tests also pass
    plain stand-ins, and a picker must never be the thing that breaks."""
    fn = getattr(t, name, None)
    if not callable(fn):
        return default
    try:
        return fn(*args)
    except Exception:
        return default


def _pick_label(t, addr: str) -> str:
    """The symbol, with how far it sits under its recent high: the number the
    dip thesis turns on, in the part of the option Discord shows largest."""
    symbol = (str(getattr(t, "symbol", "") or "") or addr[:8])
    off = _call(t, "off_high")
    tag = f"  {pct(off)} off high" if off is not None and off <= -1 else ""
    return f"{symbol}{tag}"[:100]


def _pick_description(t, kind: str) -> str:
    """Under the symbol: who is trading it now, how deep the pool is, and for a
    brand-new pair how old it is. Market cap alone said nothing about whether
    the token could be sold or whether anyone was still there."""
    buyers = _call(t, "buyers", "m5", default=0) or 0
    sells = _call(t, "sells", "m5", default=0) or 0
    depth = _call(t, "depth")
    age_sec = getattr(t, "created_ts", 0.0)
    parts = [
        f"{usd(getattr(t, 'mc_usd', None))} MC",
        f"{buyers} buyers 5m" if buyers else "quiet 5m",
        f"{buyers}/{sells} buy/sell" if (kind == "new" and (buyers or sells)) else "",
        f"liq {usd(getattr(t, 'liq_usd', None))}" + (f" ({pct(depth, signed=False)} of cap)" if depth else ""),
        f"{age(time.time() - age_sec)} old" if (kind == "new" and age_sec) else "",
    ]
    return footer(*parts)[:100]


@_factory
def board_view(kind: str, tokens) -> discord.ui.View:
    """The picker under a trending / new board: one option per token (at most 25)."""
    if kind not in ("trending", "new"):
        raise ValueError(f"unknown board kind {kind!r}")
    options: List[discord.SelectOption] = []
    seen = set()
    for t in list(tokens)[:25]:
        addr = str(getattr(t, "address", "") or "")
        if not is_evm_address(addr) or addr.lower() in seen:
            continue
        seen.add(addr.lower())
        options.append(discord.SelectOption(label=_pick_label(t, addr), value=addr,
                                            description=_pick_description(t, kind) or None))
    if not options:
        raise ValueError("no tokens to pick from")
    return _view(TokenPickSelect(kind, options))


@_factory
def size_card(token: str) -> discord.ui.View:
    """After picking a token: one [Buy $n] per configured size under the cap, plus [Other amount]."""
    token = _require_token(token)
    items: List[discord.ui.Item] = [BuyUsdButton(token, _cents(s)) for s in _ladder()]
    items.append(BuyCustomButton(token))
    return _view(*items)


@_factory
def feed_row(token: str, eid: str, ups: int = 0, downs: int = 0, *, trade: bool = True) -> discord.ui.View:
    """Under a feed post: the alert buttons, and under them the two votes.

    The tally lives in the labels, so the vote is visible to the channel rather
    than a private thing each person does — which is the point: the second
    person to look should be able to see that somebody already called it.
    """
    token = _require_token(token)
    items: List[discord.ui.Item] = []
    if trade:
        items += [BuyUsdButton(token, _cents(s)) for s in _ladder()[:2]]
        items += [SellMineButton(token, 50), SellMineButton(token, 100)]
    items += [FeedVoteButton(eid, "up", ups), FeedVoteButton(eid, "down", downs), ShareButton(token)]
    return _view(*items)


@_factory
def alert_row(token: str) -> discord.ui.View:
    """Under a Robinhood Chain alert: the two smallest buy sizes plus [Sell 50%] [Sell all] for whoever clicks."""
    token = _require_token(token)
    items: List[discord.ui.Item] = [BuyUsdButton(token, _cents(s)) for s in _ladder()[:2]]
    items += [SellMineButton(token, 50), SellMineButton(token, 100), ShareButton(token)]
    return _view(*items)


@_factory
def wallet_nudge() -> discord.ui.View:
    """With the no-wallet refusal for an allowlisted user: [Create wallet] [How it works]."""
    return _view(NavButton("wallet_create"), NavButton("tutorial"))


@_factory
def after_create_row() -> discord.ui.View:
    """After a wallet is created: [Trending now] [How it works]."""
    return _view(NavButton("trending"), NavButton("tutorial"))


@_factory
def tutorial_row(state: str) -> Optional[discord.ui.View]:
    """Under the tutorial embed, by the reader's state.

    ``no_wallet`` (allowed, no wallet) and ``public`` -> [Create wallet] [Trending now];
    ``wallet`` -> [My wallet] [Trending now]; ``none`` (not allowed, or trading paused) -> no view.
    """
    if state in ("no_wallet", "public"):
        return _view(NavButton("wallet_create"), NavButton("trending"))
    if state == "wallet":
        return _view(NavButton("wallet_show"), NavButton("trending"))
    if state == "none":
        return None
    raise ValueError(f"unknown tutorial state {state!r}")
