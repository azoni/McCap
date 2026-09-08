"""The text behind ``/rh tutorial``: what Robinhood Chain trading is and how it behaves.

Pure functions. ``build(topic, state)`` turns a :class:`TutorialState` the cog
has already gathered (allowlist, wallet, balance, first trade, gate) into one
embed; ``row_state`` and ``next_step`` tell the cog which button row and which
single next command match that state. Every figure comes from ``config`` so
the tutorial can never disagree with the caps, timeouts and expiries the code
enforces.

This module must never talk to Discord, the chain, Kyber or DexScreener, and
must never read a wallet or a ledger itself: the cog reads those (with its own
timeouts) and hands the results over. It must never show a personal fact when
``state.personal`` is False; the public variant is what a channel sees.

``CUSTODY_WARNING`` lives here from now on; ``cogs/rhc.py`` imports it so the
wallet text and the safety topic can never drift apart.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

import discord

from ..config import (
    RHC_AUTO_BUY_TTL,
    RHC_AUTO_MAX_TTL,
    RHC_AUTO_SELL_TTL,
    RHC_BUTTON_USD_SIZES,
    RHC_CONFIRM_TIMEOUT,
    RHC_DEFAULT_SLIPPAGE_BPS,
    RHC_MAX_DAILY_USD,
    RHC_MAX_TRADE_USD,
)
from ..helpers import NEUTRAL, SEP, UNKNOWN, footer, human_window, pct, usd

# Ordered (value, label) pairs; the value is what the slash Choice sends.
TOPICS: List[Tuple[str, str]] = [
    ("start", "Start here"),
    ("buy", "Buying"),
    ("sell", "Selling"),
    ("buttons", "Buttons"),
    ("auto", "Auto-orders"),
    ("safety", "Safety"),
]

TITLE = "How McCap trading works"

# Shown once at wallet creation and again under the safety topic. Wording is
# fixed; the cog imports this constant instead of keeping its own copy.
CUSTODY_WARNING = (
    "**Read this once.** McCap holds this wallet's key, encrypted, on its server. Whoever runs the "
    "server can control it. Keep only what you are actively trading here, withdraw profits, and "
    "treat it like cash in a friend's drawer, not a bank."
)

DONE, TODO = "✅", "⬜"

FOOTER_PRIVACY = "Quotes, refusals and anything about your keys are only ever shown to you"
FOOTER_HELP = "/help lists every command"
FOOTER_PAUSED = "trading is paused right now"

BUTTONS_TEXT = (
    "Buttons that act on a position (Sell 25/50/all, TP / SL under your receipt) answer only to you. "
    "Buttons that open a position (Buy $5 / $20 on /rh trending, /rh new and alerts) act on whoever "
    "clicks, with their own wallet and caps. Every one of them opens the same private quote and "
    "Confirm. They keep working after McCap restarts."
)


@dataclass
class TutorialState:
    """What the cog learned about the reader before asking for an embed.

    ``balance_wei`` None means the balance could not be read in time; it renders
    as unknown, never as funded. ``personal`` False drops every personal fact
    (address, balance, checklist, remaining cap) for the channel-wide variant.
    """

    display_name: str
    allowed: bool
    has_wallet: bool
    address: str = ""
    balance_wei: Optional[int] = None
    eth_usd: Optional[float] = None
    traded: bool = False
    gate_reason: Optional[str] = None
    remaining_usd: Optional[float] = None
    personal: bool = True


# ---------------- state helpers ----------------

def _button_money(size: float) -> str:
    """'$5' / '$20' / '$7.50': the dollar figure the way a Buy button prints it (no room for '.00')."""
    text = usd(size)
    return text[:-3] if text.endswith(".00") else text


def _funded(state: TutorialState) -> bool:
    return state.balance_wei is not None and state.balance_wei > 0


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


def row_state(state: TutorialState) -> str:
    """Which button row fits: ``public``, ``no_wallet``, ``wallet`` or ``none``.

    Buttons are offered only to someone who could act on them right now: on
    the allowlist, trading not paused. Everyone else reads without buttons.
    """
    if not state.personal:
        return "public"
    if not state.allowed or state.gate_reason:
        return "none"
    return "wallet" if state.has_wallet else "no_wallet"


def next_step(state: TutorialState) -> str:
    """The one command that moves this reader forward."""
    if not state.allowed:
        return "Ask whoever runs McCap to add you to the trader allowlist."
    if state.gate_reason:
        return f"Wait{SEP}{FOOTER_PAUSED} ({state.gate_reason})."
    if not state.has_wallet:
        return "`/rh wallet create` makes your wallet."
    if not _funded(state):
        where = f" to `{state.address}`" if state.address else ""
        return f"Send some ETH on Robinhood Chain{where}, then `/rh trending` to pick a token."
    if not state.traded:
        return f"`/rh buy <token> usd:5` shows a quote and asks you to confirm{SEP}`/rh trending` lists tokens."
    return f"`/rh holdings` shows what you own{SEP}`/rh sell <token> percent:50` sells part of it."


# ---------------- pieces ----------------

def _footer(state: TutorialState) -> str:
    return footer(FOOTER_PRIVACY, FOOTER_HELP, FOOTER_PAUSED if state.gate_reason else "")


def _new_embed(state: TutorialState) -> discord.Embed:
    e = discord.Embed(title=TITLE, colour=NEUTRAL)
    e.set_footer(text=_footer(state))
    return e


def _mark(done: bool) -> str:
    return DONE if done else TODO


def _checklist(state: TutorialState) -> str:
    """Four lines, one per step, ticked from the reader's own state."""
    allow = f"{_mark(state.allowed)} **On the allowlist**{SEP}ask whoever runs McCap"
    wallet = f"{_mark(state.has_wallet)} **Wallet**{SEP}`/rh wallet create`"
    if state.has_wallet and state.balance_wei is None:
        funded = f"{TODO} **Funded**{SEP}balance {UNKNOWN}{SEP}could not read it just now"
    elif _funded(state):
        funded = f"{DONE} **Funded**{SEP}{_eth_line(state)}"
    else:
        where = f"`{state.address}`" if state.address else "your wallet"
        funded = f"{TODO} **Funded**{SEP}send ETH on Robinhood Chain to {where}"
    trade = (f"{_mark(state.traded)} **First trade**{SEP}"
             f"`/rh buy PONS usd:5` shows a quote and asks you to confirm")
    return "\n".join([allow, wallet, funded, trade])


def _eth_line(state: TutorialState) -> str:
    eth_amt = (state.balance_wei or 0) / 1e18
    s = f"{eth_amt:,.6f}".rstrip("0").rstrip(".") or "0"
    if state.eth_usd:
        return f"**{s} ETH** ({usd(eth_amt * state.eth_usd)})"
    return f"**{s} ETH**"


def _generic_steps() -> str:
    return "\n".join([
        f"1. **Get on the allowlist**{SEP}ask whoever runs McCap",
        f"2. **Make a wallet**{SEP}`/rh wallet create`",
        f"3. **Fund it**{SEP}send ETH on Robinhood Chain to the address it shows you",
        f"4. **First trade**{SEP}`/rh buy PONS usd:5` shows a quote and asks you to confirm",
    ])


# ---------------- topics ----------------

def _start(state: TutorialState) -> discord.Embed:
    e = _new_embed(state)
    e.description = (
        "Robinhood Chain trading in four steps. Anything that moves money asks you to press "
        "**Confirm** first, unless it is an auto-order you confirmed yourself."
    )
    if state.personal:
        e.add_field(name=f"Your checklist, {state.display_name}", value=_checklist(state), inline=False)
        e.add_field(name="Next", value=next_step(state), inline=False)
    else:
        e.add_field(name="The four steps", value=_generic_steps(), inline=False)
        e.add_field(
            name="Next",
            value=f"`/rh tutorial` on your own shows where you are{SEP}trading is allowlist-only",
            inline=False,
        )
    e.add_field(name="Buttons", value=BUTTONS_TEXT, inline=False)
    return e


def _buy(state: TutorialState) -> discord.Embed:
    e = _new_embed(state)
    e.description = (
        f"`/rh buy PONS usd:5` (or `eth:0.002`) shows you a quote first. Nothing is bought until "
        f"you press **Confirm**."
    )
    e.add_field(
        name="What the quote shows",
        value="\n".join([
            f"**Buy X PONS for Y ETH ($)**{SEP}what you get and what it costs, at this second",
            f"**Sells straight back for …**{SEP}McCap checks the token can be sold again (the round "
            f"trip). No sell route means a honeypot, and McCap refuses to buy",
            f"**liquidity**{SEP}how much money sits in the pool; a thin pool moves a lot when you trade",
            f"**gas**{SEP}the network fee, paid in ETH on top",
            f"**Slippage**{SEP}how far the price may move before the trade fails instead of "
            f"filling worse; default {pct(RHC_DEFAULT_SLIPPAGE_BPS / 100, signed=False)}",
            f"**expires**{SEP}how long the Confirm button lives",
        ]),
        inline=False,
    )
    limits = [
        f"**{usd(RHC_MAX_TRADE_USD)}** per buy",
        f"**{usd(RHC_MAX_DAILY_USD)}** per day, across all your buys",
    ]
    if state.personal and state.remaining_usd is not None:
        limits.append(f"**{usd(state.remaining_usd)}** left today")
    limits.append(f"**{human_window(RHC_CONFIRM_TIMEOUT)}** to press Confirm")
    e.add_field(name="Limits", value="\n".join(limits), inline=False)
    e.add_field(
        name="Why the price can move",
        value=(
            "Other people trade while you read the quote. After Confirm, McCap asks for a fresh "
            "price; if it is worse than the quote allowed, the buy is refused and nothing moves. "
            "The daily cap is held while a buy is in flight and given back if it fails."
        ),
        inline=False,
    )
    return e


def _sell(state: TutorialState) -> discord.Embed:
    e = _new_embed(state)
    e.description = (
        "`/rh sell PONS percent:50` sells that share of what you hold, at the live price, after "
        "you press **Confirm**."
    )
    e.add_field(
        name="Percent, not amount",
        value=(
            f"**50** means half of your PONS right now, whatever that is{SEP}**100** means all of it. "
            "McCap reads your balance fresh at each click, so selling 50% twice leaves a quarter."
        ),
        inline=False,
    )
    e.add_field(
        name="The multiple line",
        value=(
            "A receipt says **2.1x** from your entry ($250K → $525K MC): the market cap now against "
            "the market cap when you bought, restated at today's supply. Below 1x you sold at a loss."
        ),
        inline=False,
    )
    e.add_field(
        name="No cap on sells",
        value=(
            f"Buys are capped at **{usd(RHC_MAX_TRADE_USD)}**; sells never are. Getting out is "
            "always allowed, in one go."
        ),
        inline=False,
    )
    e.add_field(
        name="Receipt buttons",
        value=(
            f"Under a buy receipt: **Sell 25%**, **Sell 50%**, **Sell all**, **TP / SL**. Each sell "
            f"button opens the same private quote and Confirm. After a partial sell you get "
            f"**Sell rest** and **TP / SL**. Only you can press them."
        ),
        inline=False,
    )
    return e


def _buttons(state: TutorialState) -> discord.Embed:
    e = _new_embed(state)
    e.description = BUTTONS_TEXT
    sizes = [s for s in RHC_BUTTON_USD_SIZES if s <= RHC_MAX_TRADE_USD]
    size_text = " / ".join(_button_money(s) for s in sizes) if sizes else UNKNOWN
    e.add_field(
        name="Where they appear",
        value="\n".join([
            f"**Buy receipt**{SEP}Sell 25% / 50% / all, TP / SL (yours only)",
            f"**/rh trending and /rh new**{SEP}pick a token, then Buy {size_text} or Other amount",
            f"**/mc alerts on Robinhood Chain tokens**{SEP}Buy {size_text}, Sell 50%, Sell all. "
            f"An alert that had to fall back to a second data source carries no buttons",
            f"**TP / SL**{SEP}a small form that arms a take-profit and a stop-loss in one go",
        ]),
        inline=False,
    )
    e.add_field(
        name="What a click does",
        value="\n".join([
            f"1. McCap checks **you**{SEP}allowlist, this server, trading switched on, your wallet",
            f"2. You get a **private quote**{SEP}nobody else sees it",
            f"3. You press **Confirm** within {human_window(RHC_CONFIRM_TIMEOUT)}, or nothing happens",
            f"4. The **receipt** posts with its own buttons",
        ]),
        inline=False,
    )
    e.add_field(
        name="Good to know",
        value=(
            f"A buy button above the **{usd(RHC_MAX_TRADE_USD)}** cap is refused, never shrunk. "
            f"Pressing a button on someone else's receipt says so and does nothing."
        ),
        inline=False,
    )
    return e


def _auto(state: TutorialState) -> discord.Embed:
    e = _new_embed(state)
    e.description = (
        "An auto-order is a rule you confirm once. When its condition is met it fires "
        "**without asking again**, once, and then it is gone."
    )
    e.add_field(
        name="Sell rules",
        value="\n".join([
            f"`/rh auto sell PONS percent:50 at:2x`{SEP}sell half when the market cap doubles from your entry",
            f"`at:-30%`{SEP}a stop-loss, 30% below",
            f"`at:500k`{SEP}an exact market cap",
            f"**TP / SL** under a receipt arms both at once",
        ]),
        inline=False,
    )
    e.add_field(
        name="Buy rules",
        value="\n".join([
            f"`/rh auto buy PONS usd:10 condition:<Market cap at or below> value:200k`{SEP}buy the dip",
            f"Also **Market cap at or above** and **1h volume at or above**",
        ]),
        inline=False,
    )
    e.add_field(
        name="What to expect",
        value="\n".join([
            f"Checked every **10–60s**; a fast move can be seen a little late",
            f"Expires after **{RHC_AUTO_SELL_TTL}** for sells, **{RHC_AUTO_BUY_TTL}** for buys, "
            f"**{RHC_AUTO_MAX_TTL}** at most",
            f"Fires with **no Confirm click**; you agreed when you armed it",
            f"Needs **gas ETH** in your wallet at that moment",
        ]),
        inline=False,
    )
    e.add_field(
        name="Still protected",
        value=(
            "Your daily cap, the sell-back (honeypot) check, the kill switch and your slippage all "
            "apply when it fires. A sell that cannot meet its slippage is **retried about every minute** and the "
            "rule is dropped after five failures; McCap never widens it for you. Every fill, failure or expiry posts "
            "in the channel you set it in; a rule that keeps waiting says why in `/rh auto list`."
        ),
        inline=False,
    )
    e.add_field(
        name="Manage",
        value=f"`/rh auto list` shows your rules{SEP}`/rh auto cancel <id>` removes one",
        inline=False,
    )
    return e


def _safety(state: TutorialState) -> discord.Embed:
    e = _new_embed(state)
    e.description = "What stands between a click and your money."
    e.add_field(
        name="Who can trade",
        value="\n".join([
            f"**Allowlist**{SEP}only people whoever runs McCap has added",
            f"**Kill switch**{SEP}the operator can pause all trading at once; armed rules wait",
            f"**Caps**{SEP}{usd(RHC_MAX_TRADE_USD)} per buy, {usd(RHC_MAX_DAILY_USD)} per day",
        ]),
        inline=False,
    )
    e.add_field(
        name="Every trade",
        value="\n".join([
            f"**Sell-back check**{SEP}before a buy, McCap confirms the token can be sold again; "
            f"a honeypot is refused",
            f"**One router**{SEP}every swap goes to the same pinned KyberSwap contract, nowhere else",
            f"**Minimum out**{SEP}the trade fails rather than fill below what your slippage allows",
            f"**Reserve, then refund**{SEP}a buy takes its share of your daily cap first and gives "
            f"it back if the trade fails",
            f"**One at a time**{SEP}while one of your trades is in flight, no other can start",
        ]),
        inline=False,
    )
    e.add_field(name="Your wallet", value=CUSTODY_WARNING, inline=False)
    e.add_field(
        name="Never",
        value=(
            "McCap never asks for your seed phrase, and nobody working on it will either. "
            "Anything about your keys is only ever shown to you."
        ),
        inline=False,
    )
    return e


_BUILDERS = {
    "start": _start,
    "buy": _buy,
    "sell": _sell,
    "buttons": _buttons,
    "auto": _auto,
    "safety": _safety,
}


def build(topic: str, state: TutorialState) -> discord.Embed:
    """One embed for ``topic`` (unknown topics fall back to ``start``)."""
    return _BUILDERS.get((topic or "start").lower(), _start)(state)
