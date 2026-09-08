"""What the custodial wallets hold, in total, for the bot's status line and profile.

The status line and About Me are global (one text for every server), so this
is an aggregate across all wallets: ETH, the tokens people have traded through
McCap, and their rough dollar value. Per-person figures stay behind
``/rh holdings``. Everything here is best effort and cached: a wallet whose
balance cannot be read right now is skipped, never reported as zero.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..dex import token_summary
from ..helpers import eth_str, footer, mult, plural, qty, usd
from ..logging_setup import log
from . import chain, ledger, wallets

CACHE_SECONDS = 60
MAX_TOKENS_PER_WALLET = 8
MAX_TOKEN_LOOKUPS = 40


@dataclass
class Summary:
    wallets: int = 0
    readable: int = 0                     # wallets whose ETH balance was read
    eth_wei: int = 0
    eth_usd: Optional[float] = None       # price per ETH
    tokens_usd: float = 0.0
    positions: List[Tuple[str, float, float]] = field(default_factory=list)   # (symbol, amount, usd)
    fetched_ts: float = field(default_factory=time.time)

    @property
    def eth(self) -> float:
        return self.eth_wei / 1e18

    @property
    def eth_value_usd(self) -> Optional[float]:
        return self.eth * self.eth_usd if self.eth_usd else None

    @property
    def total_usd(self) -> Optional[float]:
        if self.eth_value_usd is None and not self.tokens_usd:
            return None
        return (self.eth_value_usd or 0.0) + self.tokens_usd


_cache: Optional[Summary] = None


async def summary(force: bool = False) -> Summary:
    global _cache
    if _cache and not force and time.time() - _cache.fetched_ts < CACHE_SECONDS:
        return _cache

    s = Summary(wallets=wallets.count())
    if not s.wallets:
        _cache = s
        return s

    try:
        eth = await token_summary(chain.WETH)
        s.eth_usd = (eth or {}).get("price")
    except Exception:
        s.eth_usd = None

    balances = await asyncio.gather(*(chain.native_balance(w.address) for w in wallets.wallets), return_exceptions=True)
    for w, bal in zip(wallets.wallets, balances):
        if isinstance(bal, BaseException):
            log.debug("Could not read balance for %s: %s", w.address, bal)
            continue
        s.readable += 1
        s.eth_wei += int(bal)

    # Tokens: what each wallet has traded through McCap, valued at DexScreener's price.
    by_token: Dict[str, float] = {}
    lookups = 0
    prices: Dict[str, Tuple[str, int, Optional[float]]] = {}   # addr -> (symbol, decimals, price)
    for w in wallets.wallets:
        for addr in ledger.tokens_touched(w.user_id)[:MAX_TOKENS_PER_WALLET]:
            if lookups >= MAX_TOKEN_LOOKUPS:
                break
            lookups += 1
            try:
                have = await chain.erc20_balance(addr, w.address)
                if have <= 0:
                    continue
                if addr not in prices:
                    sym, dec = await chain.erc20_meta(addr)
                    ts = await token_summary(addr)
                    prices[addr] = (sym, dec, (ts or {}).get("price"))
                sym, dec, price = prices[addr]
                by_token[addr] = by_token.get(addr, 0.0) + have / 10 ** dec
            except Exception as e:  # noqa: BLE001
                log.debug("Could not value %s in %s: %s", addr, w.address, e)
    for addr, amount in by_token.items():
        sym, _dec, price = prices[addr]
        usd = amount * price if price else 0.0
        s.tokens_usd += usd
        s.positions.append((sym, amount, usd))
    s.positions.sort(key=lambda p: -p[2])
    s.fetched_ts = time.time()
    _cache = s
    return s


ABOUT_ME_LIMIT = 400   # Discord's cap on an application's description


def presence_fragment(s: Optional[Summary]) -> str:
    """A short piece for the status line, or '' when there is nothing to say:
    the group's total in dollars, or in ETH when there is no ETH price."""
    if s is None or s.wallets == 0:
        return ""
    total = s.total_usd
    return f"RH {usd(total)}" if total is not None else f"RH {eth_str(s.eth)} ETH"


def about_me(s: Optional[Summary]) -> str:
    """The bot's profile text: what it does, and what the group's wallets hold.

    Built to fit Discord's limit rather than sliced at it: positions are
    dropped from the end first, then the group line, so what remains is
    always whole sentences.
    """
    intro = "Market-cap alerts (/mc) and Robinhood Chain trading (/rh)."
    if s is None or s.wallets == 0:
        return f"{intro}\nNo Robinhood Chain wallets yet. /rh wallet create makes one."
    total = s.total_usd
    holdings = f"{eth_str(s.eth)} ETH" + (f" ({usd(s.eth_value_usd)})" if s.eth_value_usd is not None else "")
    positions = [f"{qty(amount)} {sym}" + (f" ({usd(u)})" if u else "") for sym, amount, u in s.positions]
    group = ""
    try:
        from . import pnl
        g = pnl.group_stats()
        if g.buys or g.sells:
            group = f"Group: {plural(g.buys + g.sells, 'trade')}, {usd(g.volume_usd)} volume"
            if g.best_multiple:
                group += f", best {mult(g.best_multiple)} {g.best_symbol}".rstrip()
    except Exception:
        log.debug("Group stats unavailable for About Me", exc_info=True)
    tail = footer("/rh holdings", "/rh pnl", "/rh stats")

    def build(n_positions: int, with_group: bool) -> str:
        held = " + ".join([holdings] + positions[:n_positions])
        head = f"{plural(s.wallets, 'wallet')} hold" + (f" {usd(total)}" if total is not None else "") + f": {held}"
        return "\n".join([intro, head] + ([group] if with_group and group else []) + [tail])

    for with_group in (True, False):
        for n in range(len(positions), -1, -1):
            text = build(n, with_group)
            if len(text) <= ABOUT_ME_LIMIT:
                return text
    return build(0, False)[:ABOUT_ME_LIMIT]
