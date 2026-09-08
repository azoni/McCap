"""What the custodial wallets hold, in total, for the bot's status line and profile.

The status line and About Me are global (one text for every server), so this
is an aggregate across all wallets: ETH, the tokens people have traded through
McCap, and their rough dollar value. Per-person figures stay behind
``/rhc holdings``. Everything here is best effort and cached: a wallet whose
balance cannot be read right now is skipped, never reported as zero.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..dex import token_summary
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


def _usd(v: Optional[float]) -> str:
    return f"${v:,.0f}" if v is not None and v >= 100 else (f"${v:,.2f}" if v is not None else "")


def presence_fragment(s: Optional[Summary]) -> str:
    """A short piece for the status line, or '' when there is nothing to say."""
    if s is None or s.wallets == 0:
        return ""
    text = f"RH {s.eth:.3f} ETH"
    if s.positions:
        text += f" + {len(s.positions)} token{'s' if len(s.positions) != 1 else ''}"
    total = s.total_usd
    if total is not None:
        text += f" ({_usd(total)})"
    return text


def about_me(s: Optional[Summary]) -> str:
    """The bot's profile text: what it does, and what the group's wallets hold."""
    lines = ["Market-cap alerts (/mc) and Robinhood Chain trading (/rhc)."]
    if s is None or s.wallets == 0:
        lines.append("No Robinhood Chain wallets yet. /rhc wallet create makes one.")
        return "\n".join(lines)
    total = s.total_usd
    lines.append(
        f"Robinhood Chain: {s.wallets} wallet{'s' if s.wallets != 1 else ''}, {s.eth:.4f} ETH"
        + (f" ({_usd(s.eth_value_usd)})" if s.eth_value_usd is not None else "")
    )
    for sym, amount, usd in s.positions[:6]:
        lines.append(f"• {amount:,.2f} {sym}" + (f" ({_usd(usd)})" if usd else ""))
    if total is not None:
        lines.append(f"Total ≈ {_usd(total)}")
    lines.append(f"Updated {time.strftime('%H:%M', time.gmtime(s.fetched_ts))} UTC · /rhc holdings for yours")
    return "\n".join(lines)[:400]
