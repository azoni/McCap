"""The money path, shared by slash commands, buttons and auto-orders.

Every buy or sell, however it was started, goes ``plan_*`` (every pre-trade
check, no money moves) -> a human Confirm click or an armed rule -> ``settle_*``
(fresh quote, reserve, sign, send). ``swap.execute`` is called from the
``settle_*`` functions and nowhere else; a test greps for that, so the guards
written here are the guards every door gets.

Nothing in this module touches a Discord Interaction. Callers turn a
``Refusal`` into a message; the auto-order engine turns it into retire/retry.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ..dex import token_summary
from ..helpers import SEP, UNKNOWN, footer, mult, pct, usd
from ..logging_setup import log
from . import chain, guard, kyber, ledger, pnl, swap

THIN_POOL_USD = 25_000   # below this, warn that 2% slippage will often not survive the send
THIN_POOL_MIN_BPS = 500  # the slippage at which the warning stops


class Refusal(Exception):
    """A reason not to trade, in the user's words.

    ``retry`` says whether the situation may pass on its own (Kyber not
    answering, the price moved while confirming) or is a fact about the trade
    (over the cap, no sell route, not enough ETH). ``icon`` is what a human
    sees in front of the text: 🚫 for a refusal, ❌ for something broken.
    """

    def __init__(self, text: str, retry: bool = False, icon: str = "🚫", usd: Optional[float] = None):
        super().__init__(text)
        self.text = text
        self.retry = retry
        self.icon = icon
        self.usd = usd            # the dollar basis that tripped a cap, when that is the reason

    def __str__(self) -> str:
        return f"{self.icon} {self.text}"


@dataclass
class BuyPlan:
    addr: str
    sym: str
    dec: int
    amount: int                       # wei of ETH going in
    bps: int
    eth_usd: Optional[float]
    rt: kyber.Route
    usd: float                        # cap basis (the larger of Kyber's and DexScreener's figure)
    back: Optional[kyber.Route]       # the sell-back route (None only when Kyber could not be asked)
    info: Optional[Dict[str, Any]]    # DexScreener summary at plan time
    liq: Optional[float]

    @property
    def thin_pool(self) -> bool:
        return self.liq is not None and self.liq < THIN_POOL_USD and self.bps < THIN_POOL_MIN_BPS

    @property
    def tokens_out(self) -> float:
        return self.rt.amount_out / 10 ** self.dec


@dataclass
class SellPlan:
    addr: str
    sym: str
    dec: int
    have: int                         # raw balance
    pct: int
    amount: int                       # raw tokens going in
    bps: int
    rt: kyber.Route


# ---------------- small shared pieces ----------------

def eth(wei: Optional[int]) -> str:
    """An ETH amount from integer wei, exact, trailing zeros dropped."""
    return chain.fmt_units(wei, 18) if wei is not None else UNKNOWN


def entry_mc(usd_in: Optional[float], tokens: float, info: Optional[Dict[str, Any]]) -> Optional[float]:
    """The market cap at the price just paid, in today's supply terms: what the
    buyer will later compare against. None when the price is unknown."""
    price = (info or {}).get("price")
    mc = (info or {}).get("mc")
    if not usd_in or tokens <= 0 or not price or not mc:
        return None
    return (usd_in / tokens) * (mc / price)


async def eth_usd() -> Optional[float]:
    try:
        s = await token_summary(chain.WETH)
        return (s or {}).get("price")
    except Exception:
        return None


async def summary(addr: str) -> Optional[Dict[str, Any]]:
    """DexScreener's view of a token, or None; never raises."""
    try:
        return await token_summary(addr)
    except Exception:
        return None


async def usd_basis(rt: kyber.Route, amount_wei: int, eth_price: Optional[float] = None) -> Tuple[Optional[float], str]:
    """Dollar size of an ETH-in trade for the caps: the LARGER of Kyber's
    amountInUsd and amount × DexScreener's ETH price, so a single wrong feed
    cannot shrink a trade under the cap. (None, reason) when neither prices it.
    Pass ``eth_price`` when it was already fetched; the post-confirm path must
    not spend a network round trip while a fresh quote is aging."""
    if eth_price is None:
        eth_price = await eth_usd()
    alt = amount_wei / 1e18 * eth_price if eth_price else None
    kyber_usd = rt.amount_in_usd if rt.amount_in_usd > 0 else None
    if kyber_usd and alt:
        if abs(alt - kyber_usd) / max(alt, kyber_usd) > 0.2:
            log.warning("USD disagreement for %s wei: Kyber $%.2f vs DexScreener $%.2f", amount_wei, kyber_usd, alt)
        return max(kyber_usd, alt), ""
    if kyber_usd or alt:
        return kyber_usd or alt, ""
    return None, ("Could not price this trade in USD (neither KyberSwap nor DexScreener gave a figure), "
                  "so the caps cannot be checked. Refusing.")


async def round_trip(token: str, amount_out: int) -> Tuple[Optional[kyber.Route], bool]:
    """(sell-back route or None, kyber_unavailable).

    None with unavailable=False means Kyber says there is no way back: honeypot
    territory. None with unavailable=True means we simply could not ask.
    """
    try:
        return await kyber.route(token, chain.NATIVE, amount_out), False
    except kyber.NoRoute:
        return None, False
    except kyber.KyberError:
        return None, True


def refund(user_id: int, amount_usd: float) -> None:
    try:
        ledger.refund(user_id, amount_usd)
    except Exception:
        log.exception("Ledger refund failed for user %s", user_id)


async def _build(rt: kyber.Route, wallet_address: str, bps: int) -> kyber.BuiltSwap:
    """kyber.build with its errors in Refusal shape: a stale quote or a 4xx may
    pass on a retry; a 'Refusing' (wrong router, malformed calldata) never will."""
    try:
        return await kyber.build(rt, wallet_address, bps)
    except kyber.KyberUnavailable as e:
        raise Refusal(str(e), retry=True, icon="❌")
    except kyber.KyberError as e:
        text = str(e) or type(e).__name__
        raise Refusal(text, retry="Refusing" not in text, icon="❌")


# ---------------- texts (a receipt reads the same whichever door opened it) ----------------

def quote_text(rt: kyber.Route, addr: str, sym: str, dec: int, back: Optional[kyber.Route],
               unavailable: bool, liq: Optional[float] = None) -> str:
    """The confirm prompt's body: the deal in one line, then what backs it."""
    head = (f"Buy **{chain.fmt_units(rt.amount_out, dec)} {sym}** for "
            f"**{eth(rt.amount_in)} ETH ({usd(rt.amount_in_usd)})**?")
    if unavailable:
        back_line = "⚠️ Could not check the sell route back to ETH (KyberSwap did not answer)"
    elif back is None:
        back_line = "⚠️ **No sell route back to ETH.** Honeypot until proven otherwise"
    elif rt.amount_in > 0:
        rt_pct = (back.amount_out / rt.amount_in - 1.0) * 100.0
        flag = "⚠️ " if rt_pct < -25 else ""
        back_line = f"{flag}Sells straight back for {eth(back.amount_out)} ETH ({pct(rt_pct)} round trip)"
    else:
        back_line = ""
    facts = footer(back_line, f"liquidity {usd(liq)}" if liq is not None else "", f"gas ≈ {usd(rt.gas_usd)}")
    return f"{head}\n{facts}\n`{addr}`"


def thin_pool_line(bps: int) -> str:
    return (f"⚠️ **Thin pool**{SEP}{pct(bps / 100, signed=False)} slippage often fails here; "
            f"re-run with `slippage_bps:{THIN_POOL_MIN_BPS}` if it does")


def describe(res: swap.SwapResult, success: str) -> str:
    if res.ok:
        return f"✅ {success}\n[Transaction]({res.explorer})"
    if res.pending:
        return f"⏳ {res.error}\n[Transaction]({res.explorer})"
    link = f"\n[Transaction]({res.explorer})" if res.tx else ""
    return f"❌ {res.error}{link}"


def buy_success(res: swap.SwapResult, built: kyber.BuiltSwap, plan: BuyPlan) -> str:
    """'Bought 36 PONS for 0.01 ETH ($24.80) · in at $492M MC'."""
    out_raw = res.amount_out if res.amount_out is not None else built.amount_out
    got = f"{chain.fmt_units(out_raw, plan.dec)} {plan.sym}" + ("" if res.amount_out is not None else " (quoted)")
    spent = f"{eth(plan.amount)} ETH ({usd(built.amount_in_usd)})"
    at = entry_mc(built.amount_in_usd, out_raw / 10 ** plan.dec, plan.info)
    tail = f"{SEP}in at {usd(at)} MC" if at else ""
    return f"Bought {got} for {spent}{tail}"


def sell_success(res: swap.SwapResult, built: kyber.BuiltSwap, plan: SellPlan, extra: Dict[str, Any]) -> str:
    """'Sold 18 PONS for 0.005 ETH ($24.70) · **1.26x** from your entry ($159K → $200K MC)'."""
    out_raw = res.amount_out if res.amount_out is not None else built.amount_out
    got = f"{eth(out_raw)} ETH ({usd(built.amount_out_usd)})" + ("" if res.amount_out is not None else " (quoted)")
    tail = ""
    multiple, at, mc_now = extra.get("multiple"), extra.get("entry_mc"), extra.get("mc_usd")
    if res.ok and multiple is not None:
        tail = f"{SEP}**{mult(multiple)}** from your entry"
        if at and mc_now:
            tail += f" ({usd(at)} → {usd(mc_now)} MC)"
    return f"Sold {chain.fmt_units(plan.amount, plan.dec)} {plan.sym} for {got}{tail}"


# ---------------- buy ----------------

async def plan_buy(user_id: int, wallet_address: str, addr: str, sym: str, dec: int, amount: int, bps: int,
                   eth_price: Optional[float], info: Optional[Dict[str, Any]] = None) -> BuyPlan:
    """Every pre-trade check for a buy, in the order the confirm prompt needs
    them. Raises ``Refusal``; moves nothing."""
    try:
        rt = await kyber.route(chain.NATIVE, addr, amount)
    except kyber.NoRoute as e:
        raise Refusal(str(e), icon="❌")
    except kyber.KyberError as e:
        raise Refusal(str(e), retry=True, icon="❌")

    basis, why = await usd_basis(rt, amount, eth_price)
    if basis is None:
        raise Refusal(why, retry=True)
    ok, why = ledger.check(user_id, basis)
    if not ok:
        raise Refusal(why, usd=basis)

    # Enough ETH for the trade AND its gas, said with numbers, before the
    # confirm prompt rather than after a reservation.
    try:
        bal = await chain.native_balance(wallet_address)
        gas_price = await chain.gas_price()
    except chain.ChainError:
        raise Refusal("Could not read your balance on Robinhood Chain. Try again.", retry=True, icon="❌")
    need = amount + (rt.gas * (100 + swap.GAS_BUFFER_PCT) // 100) * gas_price * swap.FEE_MULTIPLIER
    if bal < need:
        raise Refusal(f"Not enough ETH: you have {eth(bal)} ETH and this needs about "
                      f"{eth(need)} ETH including gas. Fund the wallet or size down.")

    back, unavailable = await round_trip(addr, rt.amount_out)
    if unavailable:
        raise Refusal("KyberSwap is not answering right now, so the sell-back check cannot run. "
                      "Try again in a minute.", retry=True, icon="❌")
    if back is None:
        raise Refusal(f"**{sym}** cannot be sold back for ETH right now (no route). That is what a honeypot "
                      f"looks like; refusing to buy.")

    # Liquidity tells the user whether the default slippage will survive the
    # few seconds between quote and send.
    if info is None:
        info = await summary(addr)
    liq = (info or {}).get("liq")
    return BuyPlan(addr=addr, sym=sym, dec=dec, amount=amount, bps=bps, eth_usd=eth_price, rt=rt,
                   usd=basis, back=back, info=info, liq=liq)


async def settle_buy(user_id: int, wallet_address: str, plan: BuyPlan, *, confirmed_floor: Optional[int],
                     extra: Dict[str, Any]) -> Tuple[swap.SwapResult, kyber.BuiltSwap, float]:
    """Fresh quote, reserve the spend, sign and send. Returns (result, built, usd reserved).

    ``confirmed_floor`` is the least a human agreed to receive; None for a rule
    that was confirmed when armed (the rule's slippage is its protection).
    The daily-cap reservation is refunded HERE and only here: on any exception,
    and on a definite failure (not ok, not pending). A pending broadcast keeps
    it; swap's pending tracking returns it if the transaction is dropped.
    """
    try:
        rt = await kyber.route(chain.NATIVE, plan.addr, plan.amount)
    except kyber.NoRoute as e:
        raise Refusal(str(e), icon="❌")
    except kyber.KyberError as e:
        raise Refusal(str(e), retry=True, icon="❌")
    if confirmed_floor is not None and rt.amount_out < confirmed_floor:
        raise Refusal(f"The price moved while you were confirming: {chain.fmt_units(rt.amount_out, plan.dec)} "
                      f"{plan.sym} now vs the {chain.fmt_units(confirmed_floor, plan.dec)} floor you confirmed. "
                      f"Nothing was bought; run the command again.", retry=True)
    basis, why = await usd_basis(rt, plan.amount, plan.eth_usd)
    if basis is None:
        raise Refusal(why, retry=True)
    ok, why = ledger.check(user_id, basis)
    if not ok:
        raise Refusal(why)
    # Reserve BEFORE the swap so two confirms cannot both pass the cap.
    try:
        ledger.record(user_id, basis)
    except Exception:
        log.exception("Ledger write failed for user %s", user_id)
        raise Refusal("The spend ledger could not be written; refusing to trade.", icon="❌")
    floor = confirmed_floor or 0
    try:
        built = await _build(rt, wallet_address, plan.bps)
        built.min_out = max(built.min_out, floor)
        res = await swap.execute(user_id, built, plan.addr, plan.sym, extra)
        # A thin pool moves in the seconds between quote and send. When the
        # pre-flight simulation says the slippage floor would not be met,
        # nothing was sent, so one fresh quote and retry is free. A human's
        # confirmed floor still applies: they never get less than they agreed to.
        if not res.ok and not res.pending and guard.is_slippage_revert(res.error):
            rt2 = await kyber.route(chain.NATIVE, plan.addr, plan.amount)
            if rt2.amount_out >= floor:
                built = await _build(rt2, wallet_address, plan.bps)
                built.min_out = max(built.min_out, floor)
                res = await swap.execute(user_id, built, plan.addr, plan.sym, extra)
    except Exception:
        refund(user_id, basis)
        raise
    if not res.ok and not res.pending:
        refund(user_id, basis)
    return res, built, basis


# ---------------- sell ----------------

async def plan_sell(user_id: int, wallet_address: str, addr: str, sym: str, dec: int, percent: int, bps: int) -> SellPlan:
    """Balance and a quote for selling ``percent`` of a holding. Raises ``Refusal``."""
    pct_sold = max(1, min(int(percent), 100))
    have = await chain.erc20_balance(addr, wallet_address)
    if have <= 0:
        raise Refusal(f"You hold no {sym}.", icon="")
    amount = have * pct_sold // 100
    try:
        rt = await kyber.route(addr, chain.NATIVE, amount)
    except kyber.NoRoute as e:
        raise Refusal(str(e), icon="❌")
    except kyber.KyberError as e:
        raise Refusal(str(e), retry=True, icon="❌")
    return SellPlan(addr=addr, sym=sym, dec=dec, have=have, pct=pct_sold, amount=amount, bps=bps, rt=rt)


def sell_extra(user_id: int, addr: str, dec: int, info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """What a sell journals: where this token stands against the entry. The
    multiple people quote ("bought at 50K, sold at 200K, 4x") is the price now
    over the price paid, with the entry restated as a market cap at today's
    supply (see pnl.TokenPnl)."""
    mc_now = (info or {}).get("mc")
    price_now = (info or {}).get("price")
    entry = pnl.entry_for(user_id, addr)
    multiple = at = None
    if entry is not None:
        entry.price_now, entry.mc_now = price_now, mc_now
        multiple, at = entry.multiple_now, entry.entry_mc
    return {"decimals": dec, "mc_usd": mc_now, "price_usd": price_now, "entry_mc": at, "multiple": multiple}


async def settle_sell(user_id: int, wallet_address: str, plan: SellPlan, *, confirmed_floor: Optional[int],
                      extra: Dict[str, Any]) -> Tuple[swap.SwapResult, kyber.BuiltSwap]:
    """Fresh quote, sign and send. Exits are never capped, so no ledger here."""
    try:
        rt = await kyber.route(plan.addr, chain.NATIVE, plan.amount)
    except kyber.NoRoute as e:
        raise Refusal(str(e), icon="❌")
    except kyber.KyberError as e:
        raise Refusal(str(e), retry=True, icon="❌")
    if confirmed_floor is not None and rt.amount_out < confirmed_floor:
        raise Refusal(f"The price moved while you were confirming: {eth(rt.amount_out)} ETH now vs "
                      f"the {eth(confirmed_floor)} floor you confirmed. Nothing was sold; run it again.", retry=True)
    floor = confirmed_floor or 0
    built = await _build(rt, wallet_address, plan.bps)
    built.min_out = max(built.min_out, floor)
    res = await swap.execute(user_id, built, plan.addr, plan.sym, extra)
    if not res.ok and not res.pending and guard.is_slippage_revert(res.error):
        rt2 = await kyber.route(plan.addr, chain.NATIVE, plan.amount)
        if rt2.amount_out >= floor:
            built = await _build(rt2, wallet_address, plan.bps)
            built.min_out = max(built.min_out, floor)
            res = await swap.execute(user_id, built, plan.addr, plan.sym, extra)
    return res, built
