"""Auto-orders: sells and buys that fire once, without asking again.

A rule is confirmed by its owner when it is armed and then watched here. The
price feed is the watcher's ``token_cache`` (no poller of our own); a rule
needs two fresh samples meeting its condition, then a fresh DexScreener read
at fire time, before it trades through ``trade.plan_* / settle_*`` exactly as
a slash command would after its Confirm click.

What this module owns: parsing a rule (``parse_expiry``, ``sell_rule``,
``buy_rule``), the one way a rule is written out (``describe_rule``), and the
``Engine`` that ticks, debounces, fires each rule in its own task and reports.

What it must never do:
- move money itself: ``swap.execute`` is called only inside ``trade.settle_*``;
- refund a daily-cap reservation: ``trade.settle_buy`` is the only refund site;
- import ``mccapbot.cogs``: the cog is fetched with ``bot.get_cog`` at tick time
  so extension reloads and test patches keep working;
- fire a rule twice: ``status="firing"`` is on disk before the money step, and
  a ``firing`` record found after a restart is retired and reported, never run;
- leave a rule in ``firing``: every exit from a fire is filled, pending, armed
  again (hold / retry) or retired.
"""

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set, Tuple

import discord

from .. import storage
from ..cache import TOKEN_CACHE_LOCK, token_cache
from ..config import (
    POLL_TICK_SECONDS,
    RHC_AUTO_BUY_TTL,
    RHC_AUTO_ENABLE,
    RHC_AUTO_MAX_TTL,
    RHC_AUTO_SELL_TTL,
    RHC_MAX_TRADE_USD,
    RHC_PENDING_BLOCK_SECONDS,
    RHC_TX_TIMEOUT,
)
from ..helpers import SEP, RelativeTargetError, human_window, meets, parse_mc_input, parse_target, pct, plural, usd
from ..logging_setup import log
from ..models import AutoOrder
from . import chain, guard, kyber, ledger, swap, trade, wallets

__all__ = [
    "CONFIRM_SAMPLES", "STALE_SECONDS", "RETRY_SECONDS", "MAX_ATTEMPTS", "PRICE_DISAGREE_PCT",
    "PENDING_POLL_SECONDS", "RHC_AUTO_ENABLE", "RHC_AUTO_SELL_TTL", "RHC_AUTO_BUY_TTL", "RHC_AUTO_MAX_TTL",
    "POLL_TICK_SECONDS", "RHC_PENDING_BLOCK_SECONDS", "RHC_TX_TIMEOUT",
    "parse_expiry", "Rule", "sell_rule", "buy_rule", "already_met", "describe_rule", "classify", "Engine",
]

# Tuning knobs. Module constants, not env: nine env keys are enough surface.
CONFIRM_SAMPLES = 2          # fresh cache samples that must agree before a fire
STALE_SECONDS = 120          # a cache sample older than this never counts
RETRY_SECONDS = 60           # wall-clock backoff after a transient failure or a hold
MAX_ATTEMPTS = 5             # transient failures before a rule gives up
PRICE_DISAGREE_PCT = 25.0    # Kyber implied price vs DexScreener, auto-buys only
PENDING_POLL_SECONDS = 30    # how often a pending fill is checked on chain

BUY_CONDITIONS = {
    "mc_below": ("mc", "below"),
    "mc_above": ("mc", "above"),
    "vol1h_above": ("vol1h", "above"),
}

_DURATION = re.compile(r"(\d*\.?\d+)\s*([mhd])")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}
MIN_EXPIRY_SECONDS = 3600


def _now() -> float:
    """The engine's clock; tests replace it."""
    return time.time()


# ---------------- parsing ----------------

def _duration_seconds(raw: str) -> int:
    s = (raw or "").strip().lower().replace(" ", "")
    m = _DURATION.fullmatch(s)
    if not m:
        raise ValueError(f"Could not read `{raw}` as a duration; use something like 12h, 3d or 30d.")
    return int(float(m.group(1)) * _UNIT_SECONDS[m.group(2)])


def parse_expiry(raw: Optional[str], default: str, maximum: str) -> float:
    """Absolute expiry timestamp for a rule. ``raw`` like 1h / 12h / 3d / 30d;
    None or "" takes ``default``. Under one hour or over ``maximum`` is refused
    with text the user can act on. Not ``helpers.parse_window``: that caps at 7d."""
    secs = _duration_seconds(raw if (raw or "").strip() else default)
    cap = _duration_seconds(maximum)
    if secs < MIN_EXPIRY_SECONDS:
        raise ValueError("Expiry must be at least 1h; the rule needs two fresh price reads before it can fire.")
    if secs > cap:
        raise ValueError(f"Expiry must be at most {maximum}.")
    return _now() + secs


@dataclass
class Rule:
    """The trigger half of an AutoOrder, before it is combined with size, wallet and channel."""
    metric: str                        # "mc" | "vol1h"
    direction: str                     # "above" | "below"
    target: float
    spec: str = ""                     # "2x", "-30%"; "" for an absolute target
    anchor_mc: Optional[float] = None
    anchor: str = ""                   # "entry" | "now" | ""


def sell_rule(at: str, anchor_mc: Optional[float], mc_now: Optional[float], anchor: str) -> Rule:
    """A sell trigger from what the user typed. Direction comes from the spec,
    never from where the price sits: a 2x is always "above" even if the token
    already trades there (the caller refuses that case with ``already_met``)."""
    try:
        target, spec = parse_target(at, anchor_mc)
    except RelativeTargetError:
        raise ValueError(f"McCap has no market cap to anchor `{at}` to; give an absolute target like 500k.")
    except ValueError:
        raise ValueError(f"Could not read `{at}` as a target; use 2x, +50%, -30% or 500k.")
    if target <= 0:
        raise ValueError("The target must be above zero.")
    if spec.endswith("x"):
        direction = "above" if float(spec[:-1]) >= 1 else "below"
    elif spec.startswith("-"):
        direction = "below"
    elif spec:
        direction = "above"
    else:
        direction = "above" if mc_now is None or target > mc_now else "below"
    return Rule(metric="mc", direction=direction, target=target, spec=spec,
                anchor_mc=anchor_mc if spec else None, anchor=anchor if spec else "")


def buy_rule(condition: str, value: str) -> Rule:
    """A buy trigger from the condition Choice and the typed value (250k, 1.2m)."""
    try:
        metric, direction = BUY_CONDITIONS[condition]
    except KeyError:
        raise ValueError("Pick a condition: market cap at or below, market cap at or above, or 1h volume at or above.")
    try:
        target = parse_mc_input(value)
    except ValueError:
        raise ValueError(f"Could not read `{value}` as a dollar figure; use 200k, 1.5m or 2500000.")
    if target <= 0:
        raise ValueError("The value must be above zero.")
    return Rule(metric=metric, direction=direction, target=target)


def already_met(direction: str, current: Optional[float], target: float) -> bool:
    """Whether the rule would fire on the spot (an unknown current value never counts)."""
    return meets(direction, current, target)


# ---------------- the one way a rule is written ----------------

def _size(o: AutoOrder) -> str:
    return f"{int(round(o.size))}%" if o.side == "sell" else usd(o.size)


def _metric(o: AutoOrder) -> str:
    return "1h volume" if o.metric == "vol1h" else "MC"


def describe_rule(o: AutoOrder) -> str:
    """'sell 50% PONS when MC ≥ $500K (2x from your $250K entry)' — used by the
    confirm prompt, the armed notice, /rh auto list and every report."""
    sign = "≥" if o.direction == "above" else "≤"
    text = f"{o.side} {_size(o)} {o.symbol} when {_metric(o)} {sign} {usd(o.target)}"
    if o.spec:
        if o.anchor == "entry" and o.anchor_mc:
            text += f" ({o.spec} from your {usd(o.anchor_mc)} entry)"
        else:
            text += f" ({o.spec} from now)"
    return text


def classify(res: swap.SwapResult) -> str:
    """filled | pending | busy | failed. Busy is the wallet's own trade in flight: a hold, not a failure.

    A pending token APPROVAL is busy too: the swap was never sent, so the rule
    must stay armed and try again once the approval lands. Treating it as a
    pending fill once reported a stop-loss as sold while every token was still
    in the wallet."""
    if res.ok:
        return "filled"
    if res.pending:
        return "busy" if getattr(res, "stage", "swap") == "approve" else "pending"
    if swap.is_busy_error(res.error):
        return "busy"
    return "failed"


# ---------------- the engine ----------------

_MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False, replied_user=False)


class _Retry(Exception):
    """A transient problem: back off and try again, counting toward MAX_ATTEMPTS."""


class _Retire(Exception):
    """A fact about the trade: the rule is removed and the owner told why."""

    def __init__(self, reason: str, icon: str = "🚫", quiet: bool = False):
        super().__init__(reason)
        self.reason, self.icon, self.quiet = reason, icon, quiet


class _Hold(Exception):
    """Not now (gate, wick, busy wallet): the rule stays armed and nothing counts against it.
    ``reason`` is remembered so /rh auto list can say why a rule keeps waiting."""

    def __init__(self, reason: str = "", backoff: bool = False):
        super().__init__(reason or "hold")
        self.reason = reason
        self.backoff = backoff


class Engine:
    """Ticks over the armed rules, fires each in its own task, reports the outcome."""

    def __init__(self, bot):
        self.bot = bot
        self._hits: Dict[str, Tuple[int, float]] = {}       # order id -> (consecutive hits, last sample ts)
        self._retry_after: Dict[str, float] = {}            # order id -> earliest next fire
        self._pending_polled: Dict[str, float] = {}         # order id -> last poll_pending
        self._user_polled: Dict[int, float] = {}            # user id -> last refresh of their in-flight tx
        self._held: Dict[str, Tuple[str, float]] = {}       # order id -> (why it last held, when)
        self._settled: Dict[str, swap.SwapResult] = {}      # order id -> result of a settle still being reported
        self._firing: Set[str] = set()
        self._firing_users: Set[int] = set()
        self._tasks: Set[asyncio.Task] = set()
        self._hold_notified: Set[Tuple[str, int]] = set()   # ("ch", channel) / ("dm", user) already told
        self._loop_task: Optional[asyncio.Task] = None

    def held_for(self, order_id: str) -> Optional[Tuple[str, float]]:
        """(reason, when) if this rule's last attempt was a hold, else None."""
        return self._held.get(order_id)

    # ----- lifecycle -----

    async def run(self) -> None:
        self._loop_task = asyncio.current_task()
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("auto-orders tick error")
            await asyncio.sleep(POLL_TICK_SECONDS)

    async def stop(self, grace: float = 10.0) -> None:
        """Cancel the loop, give in-flight fires ``grace`` seconds, cancel the rest.
        Safe because swap journals a broadcast synchronously and ``firing`` is on disk."""
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            _done, still = await asyncio.wait(pending, timeout=grace)
            for t in still:
                t.cancel()
            if still:
                await asyncio.gather(*still, return_exceptions=True)

    async def drain(self) -> None:
        """Wait for every in-flight fire to finish (tests, and an orderly stop)."""
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def held_reason(self) -> Optional[str]:
        """Why nothing fires right now, for the list header; None when live."""
        cog = self.bot.get_cog("RhcCog")
        if cog is None:
            return "trading is not loaded"
        if not RHC_AUTO_ENABLE:
            return "auto-orders are switched off (`RHC_AUTO_ENABLE=0`)"
        return cog.gate_reason()

    # ----- the tick -----

    async def tick(self) -> None:
        now = _now()
        cog = self.bot.get_cog("RhcCog")

        # 1. Expiry runs whatever else is going on: a held rule still ends on time.
        for o in list(storage.auto_orders):
            if o.status == "armed" and o.expires_ts <= now:
                await self._remove(o)
                snap = token_cache.get(o.ca)
                await self._post(o, f"🤖 `{o.id}` expired{SEP}{describe_rule(o)}{SEP}now {usd(snap.mc if snap else None)}",
                                 ping=False)

        # 2. A `firing` record nobody here is firing was mid-trade when McCap went down.
        for o in list(storage.auto_orders):
            if o.status == "firing" and o.id not in self._firing:
                await self._remove(o)
                await self._post(o, f"🤖 `{o.id}`{SEP}⚠️ McCap restarted while this rule was executing; check "
                                    f"`/rh history` and the explorer before re-creating it{SEP}{describe_rule(o)}")

        # 3. Fills that were still unconfirmed when they were reported.
        for o in list(storage.auto_orders):
            if o.status == "pending" and now - self._pending_polled.get(o.id, 0.0) >= PENDING_POLL_SECONDS:
                self._pending_polled[o.id] = now
                await self._resolve_pending(o)

        # 4. Nothing fires while trading is off; say so once per channel.
        reason = self.held_reason()
        if reason is not None:
            await self._announce_hold(reason)
            return
        self._hold_notified.clear()

        armed = [o for o in storage.auto_orders if o.status == "armed"]
        if not armed:
            return

        # 4b. A wallet waiting on its own broadcast (a token approval that took
        # too long, a manual trade) blocks its rules through has_inflight. Ask
        # the chain about it here, or the block would last until the wallet's
        # next manual trade.
        for uid in {o.user_id for o in armed}:
            if swap.pending_tx(uid) and now - self._user_polled.get(uid, 0.0) >= PENDING_POLL_SECONDS:
                self._user_polled[uid] = now
                try:
                    await swap.refresh_pending(uid)
                except Exception:
                    log.exception("Could not refresh the pending transaction for user %s", uid)

        # 5. One consistent view of the cache.
        async with TOKEN_CACHE_LOCK:
            snaps = {o.ca: token_cache.get(o.ca) for o in armed}

        # 6. Debounce, then 7. spawn.
        for o in armed:
            snap = snaps.get(o.ca)
            count, last_ts = self._hits.get(o.id, (0, 0.0))
            if snap is None or snap.updated_ts == last_ts or now - snap.updated_ts > STALE_SECONDS:
                continue
            value = snap.mc if o.metric == "mc" else snap.vol1h
            if value is None:
                continue
            count = count + 1 if meets(o.direction, value, o.target) else 0
            self._hits[o.id] = (count, snap.updated_ts)
            if (count >= CONFIRM_SAMPLES and o.id not in self._firing and o.user_id not in self._firing_users
                    and not swap.has_inflight(o.user_id) and now >= self._retry_after.get(o.id, 0.0)):
                self._spawn(o)

    def _spawn(self, o: AutoOrder) -> None:
        self._firing.add(o.id)
        self._firing_users.add(o.user_id)
        t = asyncio.create_task(self._fire(o), name=f"auto-order-{o.id}")
        self._tasks.add(t)

        def _done(task: asyncio.Task, oid=o.id, uid=o.user_id) -> None:
            self._tasks.discard(task)
            self._firing.discard(oid)
            self._firing_users.discard(uid)

        t.add_done_callback(_done)

    async def _announce_hold(self, reason: str) -> None:
        armed = [o for o in storage.auto_orders if o.status == "armed"]
        groups: Dict[Tuple[str, int], list] = {}
        for o in armed:
            groups.setdefault(("dm", o.user_id) if o.private else ("ch", o.channel_id), []).append(o)
        for key, group in groups.items():
            if key in self._hold_notified:
                continue
            self._hold_notified.add(key)
            if reason.startswith("auto-orders are switched off"):
                head = f"⏸ Auto-orders are switched off (`RHC_AUTO_ENABLE=0`)"
                back = "resume when they are switched back on"
            else:
                head = f"⏸ Trading is paused ({reason.rstrip('.')})"
                back = "resume when it returns"
            await self._post(group[0], f"{head}{SEP}{plural(len(group), 'auto-order')} here are on hold and {back}"
                                       f"{SEP}they still expire on schedule", ping=False)

    async def _resolve_pending(self, o: AutoOrder) -> None:
        try:
            st = await swap.poll_pending(o.user_id, o.tx)
        except Exception:
            log.exception("poll_pending failed for auto-order %s", o.id)
            return
        if st == "pending":
            return
        link = f"\n[Transaction]({chain.explorer_tx(o.tx)})" if o.tx else ""
        # Only a buy or a sell is a fill. A rule left pending on a token
        # approval (older records) goes back to armed: nothing was swapped.
        entry = ledger.entry_for_tx(o.user_id, o.tx) if o.tx else None
        if entry is not None and entry.get("kind") not in ("buy", "sell"):
            o.status, o.tx = "armed", ""
            self._hits.pop(o.id, None)
            await storage.save_orders()
            await self._post(o, f"🤖 `{o.id}`{SEP}the token approval {st}; the {o.side} itself was never sent, so the "
                                f"rule is armed again{SEP}{describe_rule(o)}{link}", ping=False)
            return
        if st == "confirmed":
            text = f"✅ confirmed on chain{link}"
        elif st == "reverted":
            text = f"❌ reverted on chain; nothing was swapped{link}"
        elif st == "dropped":
            text = f"🚫 dropped after {human_window(RHC_PENDING_BLOCK_SECONDS)}; nothing moved{link}"
        else:
            log.warning("Auto-order %s: journal has no record of tx %s", o.id, o.tx)
            text = f"⚠️ McCap lost track of this transaction; check `/rh history` and the explorer{link}"
        await self._remove(o)
        await self._post(o, f"🤖 `{o.id}`{SEP}{text}")

    # ----- firing -----

    async def _fire(self, o: AutoOrder) -> None:
        try:
            if o.side == "sell":
                await self._fire_sell(o)
            else:
                await self._fire_buy(o)
        except asyncio.CancelledError:
            raise
        except _Hold as h:
            await self._hold(o, h.reason, backoff=h.backoff)
        except _Retry as r:
            await self._retry(o, str(r))
        except _Retire as r:
            await self._retire(o, r.reason, icon=r.icon, quiet=r.quiet)
        except Exception:
            log.exception("auto-order %s failed unexpectedly", o.id)
            res = self._settled.get(o.id)
            if res is not None and (res.ok or res.pending):
                # Money moved and then the bookkeeping broke. Never say
                # "nothing was sold"; hand over the transaction instead.
                await self._recorded_badly(o, res)
            else:
                await self._retire(o, "internal error; nothing further will run for this rule", icon="❌")
        finally:
            self._settled.pop(o.id, None)

    async def _recorded_badly(self, o: AutoOrder, res: swap.SwapResult) -> None:
        try:
            await self._remove(o)
        except Exception:
            log.exception("Could not remove auto-order %s after a fill that failed to record", o.id)
        link = f"\n[Transaction]({res.explorer})" if res.tx else ""
        await self._post(o, f"🤖 `{o.id}` executed, but McCap could not finish recording it{SEP}check `/rh history` "
                            f"and the explorer{SEP}{describe_rule(o)}{link}")

    def _preflight(self, o: AutoOrder):
        """Steps 1-2 of both fire paths: may this person still trade, and with what wallet."""
        cog = self.bot.get_cog("RhcCog")
        if cog is None:
            raise _Hold("trading is not loaded")
        why = cog.deny(o.user_id, o.guild_id or None)
        if why is not None:
            kind, text = why
            if kind == "allowlist":
                raise _Retire("you are no longer on the trader allowlist")
            if kind == "guild":
                raise _Retire("trading is not enabled in that server any more")
            raise _Hold(f"trading is paused ({text.rstrip('.')})")
        w = wallets.get(o.user_id)
        if w is None:
            raise _Retire("your wallet is gone from the vault")
        return w

    async def _fresh(self, o: AutoOrder) -> Dict[str, Any]:
        """Step 3: DexScreener now, not the cache. A wick that is over by the time we look is a hold."""
        info = await trade.summary(o.ca)
        if info is None:
            raise _Retry(f"No DexScreener data for {o.symbol} right now")
        value = info.get("mc") if o.metric == "mc" else info.get("vol1h")
        if not meets(o.direction, value, o.target):
            raise _Hold(f"the fresh read ({usd(value)}) no longer met the level")
        return info

    async def _mark_firing(self, o: AutoOrder) -> None:
        """Steps 5-6: the cancel race, then `firing` on disk before any money moves."""
        if o not in storage.auto_orders:
            raise _Hold("cancelled while quoting")
        o.status = "firing"
        o.attempts += 1
        o.last_attempt_ts = _now()
        self._held.pop(o.id, None)
        await storage.save_orders()

    def _tags(self, o: AutoOrder) -> Dict[str, Any]:
        return {"source": "auto", "order_id": o.id, "rule": describe_rule(o)}

    async def _fire_sell(self, o: AutoOrder) -> None:
        w = self._preflight(o)
        info = await self._fresh(o)
        try:
            plan = await trade.plan_sell(o.user_id, w.address, o.ca, o.symbol, o.decimals, int(o.size), o.slippage_bps)
        except trade.Refusal as e:
            if e.retry:
                raise _Retry(e.text)
            if e.text.startswith("You hold no"):
                raise _Retire("nothing left to sell", quiet=True)
            raise _Retire(f"no route to sell {o.symbol} for ETH; its liquidity may be gone")
        except (kyber.KyberUnavailable, chain.ChainError) as e:
            raise _Retry(str(e) or type(e).__name__)
        await self._mark_firing(o)
        extra = trade.sell_extra(o.user_id, o.ca, o.decimals, info) | self._tags(o)
        res, built = await self._settle(o, trade.settle_sell(o.user_id, w.address, plan, confirmed_floor=None, extra=extra))
        success = self._success_text(o, lambda: trade.sell_success(res, built, plan, extra))
        view = self._panel("partial_sell_row", o) if plan.pct < 100 else None
        await self._conclude(o, res, success, view)

    async def _fire_buy(self, o: AutoOrder) -> None:
        w = self._preflight(o)
        info = await self._fresh(o)
        eth_price = await trade.eth_usd()
        if not eth_price:
            raise _Retry("No ETH price from DexScreener right now")
        amount = int(o.size / eth_price * 1e18)
        sized_down = False
        try:
            try:
                plan = await trade.plan_buy(o.user_id, w.address, o.ca, o.symbol, o.decimals, amount, o.slippage_bps,
                                            eth_price, info=info)
            except trade.Refusal as e:
                # A rule sized right at the cap can be priced a little over it
                # by Kyber at fire time. Shrink the ETH leg to the cap once
                # rather than throw away a dip buy the user waited a day for.
                if e.usd and "per-trade cap" in e.text and e.usd <= RHC_MAX_TRADE_USD * 1.05:
                    amount = int(amount * RHC_MAX_TRADE_USD / e.usd * 0.995)
                    sized_down = True
                    plan = await trade.plan_buy(o.user_id, w.address, o.ca, o.symbol, o.decimals, amount,
                                                o.slippage_bps, eth_price, info=info)
                else:
                    raise
        except trade.Refusal as e:
            if e.retry:
                raise _Retry(e.text)
            raise _Retire(e.text)
        # Kyber's implied price against DexScreener's: a lagging feed or an odd
        # route is a reason to wait, never to buy at the wrong price.
        price = info.get("price")
        tokens = plan.rt.amount_out / 10 ** o.decimals if o.decimals >= 0 else 0
        if price and tokens > 0 and plan.rt.amount_in_usd > 0:
            implied = plan.rt.amount_in_usd / tokens
            gap = abs(implied / price - 1.0) * 100.0
            if gap > PRICE_DISAGREE_PCT:
                log.warning("Auto-order %s: Kyber implies $%.6g vs DexScreener $%.6g for %s; holding",
                            o.id, implied, price, o.symbol)
                raise _Hold(f"KyberSwap's price is {pct(gap, signed=False)} off DexScreener's", backoff=True)
        await self._mark_firing(o)
        extra = {"decimals": o.decimals, "eth_usd": eth_price, "mc_usd": info.get("mc"),
                 "price_usd": info.get("price")} | self._tags(o)
        res, built, _usd = await self._settle(o, trade.settle_buy(o.user_id, w.address, plan, confirmed_floor=None,
                                                                   extra=extra))
        success = self._success_text(o, lambda: trade.buy_success(res, built, plan))
        if sized_down:
            success += f"{SEP}sized to the {usd(RHC_MAX_TRADE_USD)} cap"
        await self._conclude(o, res, success, self._panel("receipt_row", o))

    @staticmethod
    def _success_text(o: AutoOrder, make) -> str:
        """The receipt line, or a plain fallback: a formatting slip must never turn a fill into an error."""
        try:
            return make()
        except Exception:
            log.exception("Could not format the receipt for auto-order %s", o.id)
            return f"{'Sold' if o.side == 'sell' else 'Bought'} {o.symbol} (details in /rh history)"

    async def _settle(self, o: AutoOrder, coro):
        """Run a settle coroutine, mapping its refusals onto retry / retire.
        ``trade.settle_buy`` has already refunded any reservation by the time we see an error.
        The result is remembered until the fire is concluded, so an error after
        the money moved can still be reported as what it is."""
        try:
            result = await coro
        except trade.Refusal as e:
            if e.retry:
                raise _Retry(e.text)
            raise _Retire(e.text, icon=e.icon or "🚫")
        except kyber.NoRoute as e:
            raise _Retire(str(e), icon="❌")
        except (kyber.KyberUnavailable, chain.RpcUnavailable) as e:
            raise _Retry(str(e) or type(e).__name__)
        except kyber.KyberError as e:
            text = str(e) or type(e).__name__
            if "Refusing" in text:
                raise _Retire(text, icon="❌")
            raise _Retry(text)
        self._settled[o.id] = result[0]
        return result

    async def _conclude(self, o: AutoOrder, res: swap.SwapResult, success: str, view) -> None:
        kind = classify(res)
        rule = describe_rule(o)
        link = f"\n[Transaction]({res.explorer})" if res.tx else ""
        if kind == "filled":
            o.fired_ts = _now()
            await self._remove(o)
            await self._post(o, f"🤖 `{o.id}` fired{SEP}✅ {success}{SEP}{rule}{link}", view=view)
        elif kind == "pending":
            o.status, o.tx, o.fired_ts = "pending", res.tx or "", _now()
            await storage.save_orders()
            await self._post(o, f"🤖 `{o.id}` fired{SEP}⏳ Sent but unconfirmed after {human_window(RHC_TX_TIMEOUT)}; "
                                f"your wallet is locked until it resolves{SEP}{rule}{link}")
        elif kind == "busy":
            why = ("the token approval is still confirming" if getattr(res, "stage", "swap") == "approve"
                   else "your wallet has a transaction in flight")
            await self._hold(o, why, backoff=True)
        elif guard.is_slippage_revert(res.error):
            # A stop-loss exists for exactly the minute the pool is moving too
            # fast to fill at its slippage. Keep trying at the same bps (never
            # widen); give up only after MAX_ATTEMPTS real failures.
            nothing = "nothing was bought" if o.side == "buy" else "nothing was sold"
            await self._retry(o, f"the price moved past the slippage limit before the swap could be sent; {nothing}")
        else:
            await self._retire(o, res.error or "the swap failed", icon="❌")

    # ----- outcomes -----

    async def _remove(self, o: AutoOrder) -> None:
        if o in storage.auto_orders:
            storage.auto_orders.remove(o)
        self._hits.pop(o.id, None)
        self._retry_after.pop(o.id, None)
        self._pending_polled.pop(o.id, None)
        self._held.pop(o.id, None)
        try:
            await storage.save_orders()
        except Exception:
            log.exception("Could not save the order file after removing %s", o.id)

    async def _hold(self, o: AutoOrder, reason: str = "", *, backoff: bool = False) -> None:
        """Stay armed; hits reset; attempts untouched. Disk only if we had marked `firing`."""
        was_firing = o.status == "firing"
        o.status = "armed"
        self._hits.pop(o.id, None)
        if reason:
            self._held[o.id] = (reason, _now())
        if backoff:
            self._retry_after[o.id] = _now() + RETRY_SECONDS
        if was_firing and o in storage.auto_orders:
            await storage.save_orders()

    async def _retry(self, o: AutoOrder, error: str) -> None:
        if o.status != "firing":          # a firing attempt was already counted when it was marked
            o.attempts += 1
        o.status = "armed"
        o.last_error = error
        o.last_attempt_ts = _now()
        self._hits.pop(o.id, None)
        self._retry_after[o.id] = _now() + RETRY_SECONDS
        if o not in storage.auto_orders:
            return
        if o.attempts >= MAX_ATTEMPTS:
            await self._remove(o)
            await self._post(o, f"🤖 `{o.id}` gave up after {plural(o.attempts, 'try', 'tries')}{SEP}❌ {error}"
                                f"{SEP}{describe_rule(o)}")
            return
        await storage.save_orders()

    async def _retire(self, o: AutoOrder, reason: str, *, icon: str = "🚫", quiet: bool = False) -> None:
        try:
            await self._remove(o)
        except Exception:
            log.exception("Could not save the order file while retiring %s", o.id)
        if quiet:
            await self._post(o, f"🤖 `{o.id}` retired{SEP}{reason}{SEP}{describe_rule(o)}", ping=False)
            return
        nothing = "nothing was bought" if o.side == "buy" else "nothing was sold"
        await self._post(o, f"🤖 `{o.id}` stopped{SEP}{icon} {reason}{SEP}{describe_rule(o)}{SEP}{nothing}")

    # ----- reporting -----

    @staticmethod
    def _panel(kind: str, o: AutoOrder):
        """A button row from views, or None while that module is missing or refuses."""
        try:
            from .. import views
            return getattr(views, kind)(o.user_id, o.ca)
        except Exception:
            return None

    async def _post(self, o: AutoOrder, text: str, *, ping: bool = True, view=None) -> None:
        """Report in the rule's channel (or by DM for a private rule); fall back to a
        DM when the channel is gone or closed to us. State is saved before this is
        called, so a failed post can never re-arm a rule. Both failing is logged
        with the full text: the transaction hash has to land somewhere."""
        content = f"<@{o.user_id}>{SEP}{text}" if ping else text
        kw: Dict[str, Any] = {"allowed_mentions": _MENTIONS, "suppress_embeds": True}
        if view is not None:
            kw["view"] = view
        try:
            dest = await (self.bot.fetch_user(o.user_id) if o.private else self.bot.fetch_channel(o.channel_id))
            await dest.send(content, **kw)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if o.private:
                log.exception("Could not DM auto-order report to user %s: %s", o.user_id, content)
                return
            log.warning("Auto-order report to channel %s failed (%s); trying a DM", o.channel_id, e)
        try:
            user = await self.bot.fetch_user(o.user_id)
            await user.send(content, allowed_mentions=_MENTIONS, suppress_embeds=True)
        except Exception:
            log.exception("Could not deliver auto-order report to user %s: %s", o.user_id, content)
