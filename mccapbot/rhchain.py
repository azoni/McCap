"""Robinhood chain DEX activity, from GeckoTerminal.

``/rh_trending`` used to rank the majors listed in Robinhood's app (BTC, ETH,
...) by CoinGecko price change, which said nothing about the Robinhood chain
itself. What people want to watch is the chain's own DEX pools: which tokens are
trading, on which venues, and how much.

DexScreener has no per-chain listing; its search endpoint returns 30 pairs
matched on text. GeckoTerminal does: ``/networks/robinhood/pools`` sorted by
24h volume, 20 per page, with base/quote token, venue, liquidity, market cap
and price change over several windows. Free, keyless, and it shares the
GeckoTerminal rate limiter with the momentum backfill so neither starves the
other.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from .config import RHCHAIN_CACHE_SECONDS, RHCHAIN_NETWORK, RHCHAIN_PAGES
from .gecko import BASE, gecko_limiter
from .http import get_json
from .logging_setup import log

# GeckoTerminal reports volume and price change over all of these. Price change
# doubles as market-cap change: supply does not move inside a window.
WINDOWS = ("m5", "m15", "m30", "h1", "h6", "h24")
WINDOW_LABELS = {"m5": "5m", "m15": "15m", "m30": "30m", "h1": "1h", "h6": "6h", "h24": "24h"}
WINDOW_SECONDS = {"m5": 300, "m15": 900, "m30": 1800, "h1": 3600, "h6": 21600, "h24": 86400}
SORTS = ("volume", "gainers", "losers", "new")

# The chain's plumbing rather than something to trade: hidden by default so the
# board is not two-thirds WETH/USDG pools.
CHAIN_MAJORS = {"WETH", "ETH", "USDG", "USDC", "USDT", "DAI", "WBTC", "CBBTC"}


@dataclass
class Pool:
    address: str
    name: str
    dex: str
    base_symbol: str
    base_name: str
    base_address: str
    quote_symbol: str
    price_usd: Optional[float]
    liq_usd: float
    mc_usd: Optional[float]                   # market cap, else FDV
    volume: Dict[str, float]                  # window -> USD
    change: Dict[str, Optional[float]]        # window -> percent
    buys_h24: int
    sells_h24: int
    created_ts: float
    # window -> {"buys", "sells", "buyers", "sellers"}: how many trades and how
    # many distinct wallets, per window. Unique buyers in the last 5 minutes is
    # the cleanest "is anyone actually here" signal GeckoTerminal offers.
    tx: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def url(self) -> str:
        return f"https://www.geckoterminal.com/{RHCHAIN_NETWORK}/pools/{self.address}"

    def count(self, window: str, what: str) -> int:
        return int((self.tx.get(window) or {}).get(what) or 0)


@dataclass
class TokenActivity:
    """One base token, every pool it trades in."""

    symbol: str
    name: str
    address: str
    pools: List[Pool] = field(default_factory=list)

    @property
    def deepest(self) -> Pool:
        return max(self.pools, key=lambda p: p.liq_usd)

    def volume(self, window: str) -> float:
        return sum(p.volume.get(window, 0.0) for p in self.pools)

    def change(self, window: str) -> Optional[float]:
        """Price change from the deepest pool; thin pools print junk swings."""
        return self.deepest.change.get(window)

    @property
    def liq_usd(self) -> float:
        return sum(p.liq_usd for p in self.pools)

    @property
    def mc_usd(self) -> Optional[float]:
        return self.deepest.mc_usd

    def mc_before(self, window: str) -> Optional[float]:
        """Market cap at the start of the window, implied by the price change.

        "$800K to $1.1M in 15 minutes" is the number people actually want, and
        with a fixed supply it is exactly mc / (1 + change)."""
        mc, chg = self.mc_usd, self.change(window)
        if mc is None or chg is None or chg <= -100:
            return None
        return mc / (1.0 + chg / 100.0)

    @property
    def buys_h24(self) -> int:
        return sum(p.buys_h24 for p in self.pools)

    @property
    def sells_h24(self) -> int:
        return sum(p.sells_h24 for p in self.pools)

    def buys(self, window: str) -> int:
        return sum(p.count(window, "buys") for p in self.pools)

    def sells(self, window: str) -> int:
        return sum(p.count(window, "sells") for p in self.pools)

    def buyers(self, window: str) -> int:
        """Distinct buying wallets across the token's pools (a wallet active in
        two pools counts twice; close enough for a signal)."""
        return sum(p.count(window, "buyers") for p in self.pools)

    def sellers(self, window: str) -> int:
        return sum(p.count(window, "sellers") for p in self.pools)

    def volume_pace(self, short: str = "m5", long: str = "h1") -> Optional[float]:
        """How much faster money is moving now than over the longer window:
        the short window's volume scaled to the long window's length, divided
        by the long window's volume. 3.0 means the last five minutes ran at
        three times the hour's pace. None when the long window is empty."""
        long_vol = self.volume(long)
        if long_vol <= 0:
            return None
        return (self.volume(short) * WINDOW_SECONDS[long] / WINDOW_SECONDS[short]) / long_vol

    @property
    def venues(self) -> List[str]:
        return sorted({p.dex for p in self.pools})

    @property
    def created_ts(self) -> float:
        return min((p.created_ts for p in self.pools if p.created_ts), default=0.0)


# ---------------- parsing ----------------

def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ts(iso: Optional[str]) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _dex_name(raw: str) -> str:
    """'Uniswap V3 (Robinhood)' -> 'Uniswap V3'; the chain is implied."""
    return raw.replace("(Robinhood)", "").strip() or raw


def parse_pools(payload: Optional[Dict]) -> List[Pool]:
    """Pools from one GeckoTerminal page (JSON:API shape, tokens/dex in ``included``)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    included = {
        (i.get("type"), i.get("id")): (i.get("attributes") or {})
        for i in payload.get("included") or []
        if isinstance(i, dict)
    }

    def rel(p: Dict, name: str) -> Dict:
        ref = ((p.get("relationships") or {}).get(name) or {}).get("data") or {}
        return included.get((ref.get("type"), ref.get("id")), {})

    out: List[Pool] = []
    for p in payload["data"]:
        if not isinstance(p, dict):
            continue
        a = p.get("attributes") or {}
        base, quote, dex = rel(p, "base_token"), rel(p, "quote_token"), rel(p, "dex")
        address = (a.get("address") or "").lower()
        base_addr = (base.get("address") or "").lower()
        if not address or not base_addr:
            continue
        vol = a.get("volume_usd") or {}
        chg = a.get("price_change_percentage") or {}
        all_tx = a.get("transactions") or {}
        tx = all_tx.get("h24") or {}
        per_window = {
            w: {k: int(_f((all_tx.get(w) or {}).get(k)) or 0) for k in ("buys", "sells", "buyers", "sellers")}
            for w in WINDOWS if isinstance(all_tx.get(w), dict)
        }
        out.append(Pool(
            address=address,
            name=a.get("name") or "",
            dex=_dex_name(dex.get("name") or ""),
            base_symbol=(base.get("symbol") or "?").strip(),
            base_name=base.get("name") or base.get("symbol") or "?",
            base_address=base_addr,
            quote_symbol=(quote.get("symbol") or "?").strip(),
            price_usd=_f(a.get("base_token_price_usd")),
            liq_usd=_f(a.get("reserve_in_usd")) or 0.0,
            mc_usd=_f(a.get("market_cap_usd")) or _f(a.get("fdv_usd")),
            volume={w: (_f(vol.get(w)) or 0.0) for w in WINDOWS},
            change={w: _f(chg.get(w)) for w in WINDOWS},
            buys_h24=int(_f(tx.get("buys")) or 0),
            sells_h24=int(_f(tx.get("sells")) or 0),
            created_ts=_ts(a.get("pool_created_at")),
            tx=per_window,
        ))
    return out


def aggregate(pools: Iterable[Pool]) -> List[TokenActivity]:
    """Group pools by base token. PONS trades in three pools; it is one row."""
    by_addr: Dict[str, TokenActivity] = {}
    for p in pools:
        tok = by_addr.get(p.base_address)
        if tok is None:
            tok = by_addr[p.base_address] = TokenActivity(
                symbol=p.base_symbol, name=p.base_name, address=p.base_address
            )
        tok.pools.append(p)
    return list(by_addr.values())


def rank(
    tokens: Iterable[TokenActivity],
    window: str = "h24",
    sort: str = "volume",
    include_majors: bool = False,
    n: int = 10,
) -> List[TokenActivity]:
    """Order tokens for display.

    ``volume`` is what the board is for. ``gainers``/``losers`` use the deepest
    pool's change and skip tokens with none, rather than ranking them as flat.
    ``new`` is by the earliest pool's creation time.
    """
    rows = [t for t in tokens if include_majors or t.symbol.upper() not in CHAIN_MAJORS]
    if sort == "gainers":
        rows = [t for t in rows if t.change(window) is not None]
        rows.sort(key=lambda t: t.change(window), reverse=True)
    elif sort == "losers":
        rows = [t for t in rows if t.change(window) is not None]
        rows.sort(key=lambda t: t.change(window))
    elif sort == "new":
        rows.sort(key=lambda t: t.created_ts, reverse=True)
    else:
        rows.sort(key=lambda t: t.volume(window), reverse=True)
    return rows[:n]


# ---------------- fetching ----------------

_cache: List[Pool] = []
_cached_at = 0.0


async def top_pools(force: bool = False) -> List[Pool]:
    """The chain's busiest pools by 24h volume, ``RHCHAIN_PAGES`` pages deep.

    Cached briefly. A failed refresh keeps the previous result rather than
    blanking the board over a blip.
    """
    global _cached_at
    now = time.time()
    if _cache and not force and (now - _cached_at) < RHCHAIN_CACHE_SECONDS:
        return _cache

    fresh: List[Pool] = []
    for page in range(1, max(1, RHCHAIN_PAGES) + 1):
        url = (
            f"{BASE}/networks/{RHCHAIN_NETWORK}/pools"
            f"?sort=h24_volume_usd_desc&page={page}&include=base_token,quote_token,dex"
        )
        data = await get_json(url, limiter=gecko_limiter)
        pools = parse_pools(data)
        if not pools:
            if page == 1:
                log.debug("GeckoTerminal returned no %s pools", RHCHAIN_NETWORK)
            break
        fresh.extend(pools)

    if fresh:
        _cache[:] = fresh
        _cached_at = now
    return _cache


_new_cache: List[Pool] = []
_new_cached_at = 0.0


async def new_pools(force: bool = False) -> List[Pool]:
    """The chain's most recently created pools, newest first.

    A separate feed from the busiest pools: a pair minutes old has no volume
    yet, so it would never appear in ``top_pools``. Most of these are dust;
    the caller decides what to hide.
    """
    global _new_cached_at
    now = time.time()
    if _new_cache and not force and (now - _new_cached_at) < RHCHAIN_CACHE_SECONDS:
        return _new_cache
    url = f"{BASE}/networks/{RHCHAIN_NETWORK}/new_pools?page=1&include=base_token,quote_token,dex"
    pools = parse_pools(await get_json(url, limiter=gecko_limiter))
    if pools:
        pools.sort(key=lambda p: -p.created_ts)
        _new_cache[:] = pools
        _new_cached_at = now
    return _new_cache


def age_str(created_ts: float, now: Optional[float] = None) -> str:
    """'3m', '2h', '5d' since the pool was created; '?' when unknown."""
    if not created_ts:
        return "?"
    secs = max(0.0, (now if now is not None else time.time()) - created_ts)
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"
