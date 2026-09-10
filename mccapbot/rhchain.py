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

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from .config import RHCHAIN_CACHE_SECONDS, RHCHAIN_NETWORK, RHCHAIN_NEW_PAGES, RHCHAIN_PAGES
from . import gecko
from .gecko import BASE, gecko_limiter
from .helpers import UNKNOWN, age
from .http import get_json
from .logging_setup import log

# GeckoTerminal reports volume and price change over all of these. Price change
# doubles as market-cap change: supply does not move inside a window.
WINDOWS = ("m5", "m15", "m30", "h1", "h6", "h24")
WINDOW_LABELS = {"m5": "5m", "m15": "15m", "m30": "30m", "h1": "1h", "h6": "6h", "h24": "24h"}
WINDOW_SECONDS = {"m5": 300, "m15": 900, "m30": 1800, "h1": 3600, "h6": 21600, "h24": 86400}
SORTS = ("volume", "gainers", "losers", "new", "active", "retrace")
# GeckoTerminal's trending list accepts these windows.
TRENDING_DURATIONS = ("5m", "1h", "6h", "24h")

# A "retrace" row has to be at least this far under its high to be interesting,
# and not so far under it that the token has simply gone to zero. The thesis is
# that something which ran once can run again; a chart down 99% is not a dip in
# a live token, it is a rug with its 24h buyer count still on the board.
RETRACE_MIN_PCT = 20.0
RETRACE_MAX_PCT = 90.0

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
    # From the included base-token record. Decimals known here spare the
    # symbol resolver an RPC call; 0 means "not reported", never "zero decimals".
    base_decimals: int = 0
    base_image_url: str = ""

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

    @property
    def reference(self) -> Pool:
        """The pool whose price is worth reading: the deepest one quoted in a
        major (WETH, USDG, ...). A token's deepest pool can be quoted in another
        memecoin or a tokenised stock, and its price change then says as much
        about the quote as about the token. Falls back to the deepest pool."""
        majors = [p for p in self.pools if p.quote_symbol.upper() in CHAIN_MAJORS]
        return max(majors, key=lambda p: p.liq_usd) if majors else self.deepest

    @property
    def quote_is_major(self) -> bool:
        return self.reference.quote_symbol.upper() in CHAIN_MAJORS

    def volume(self, window: str) -> float:
        return sum(p.volume.get(window, 0.0) for p in self.pools)

    def change(self, window: str) -> Optional[float]:
        """Price change from the reference pool; thin pools print junk swings."""
        return self.reference.change.get(window)

    @property
    def liq_usd(self) -> float:
        return sum(p.liq_usd for p in self.pools)

    @property
    def mc_usd(self) -> Optional[float]:
        return self.reference.mc_usd

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

    def mc_path(self) -> List[Tuple[int, float]]:
        """(seconds ago, market cap) at every point the listing lets us see:
        now, and each window's start implied by its price change. Seven cheap
        points, no extra request, enough to tell a token that is holding its
        level from one that has already halved."""
        mc = self.mc_usd
        if not mc:
            return []
        out = [(0, mc)]
        for w in WINDOWS:
            before = self.mc_before(w)
            if before:
                out.append((WINDOW_SECONDS[w], before))
        return out

    def mc_high(self) -> Optional[float]:
        """The highest market cap visible in the last 24 hours. An estimate: it
        reads the window boundaries, so a spike that began and ended inside one
        window is invisible. ``rhchain.price_highs`` buys the real figure."""
        path = self.mc_path()
        return max(v for _s, v in path) if path else None

    def off_high(self) -> Optional[float]:
        """How far under that high the token sits now, as a percent at or below
        zero. -60% means it traded at 2.5x this price inside the day."""
        mc, high = self.mc_usd, self.mc_high()
        if not mc or not high or high <= 0:
            return None
        return (mc / high - 1.0) * 100.0

    def buyer_share(self, window: str = "h1") -> Optional[float]:
        """Share of the window's trading wallets that were buying, 0..100.
        Above 50 means more wallets bought than sold — pressure, not price."""
        buyers, sellers = self.buyers(window), self.sellers(window)
        total = buyers + sellers
        return (buyers / total * 100.0) if total else None

    def turnover(self, window: str = "h24") -> Optional[float]:
        """Window volume against liquidity: how many times the pool turned over.
        A big pool nobody trades and a small one trading hard look identical by
        volume alone."""
        return (self.volume(window) / self.liq_usd) if self.liq_usd > 0 else None

    def depth(self) -> Optional[float]:
        """Liquidity as a share of market cap, 0..100. Low means most of the
        'cap' cannot be sold into at anything like this price."""
        mc = self.mc_usd
        return (self.liq_usd / mc * 100.0) if mc and mc > 0 else None

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
            base_decimals=int(_f(base.get("decimals")) or 0),
            base_image_url=str(base.get("image_url") or ""),
        ))
    return out


def dedup_pools(*lists: Iterable[Pool]) -> List[Pool]:
    """Merge several pool lists keeping the first record per pool address, so
    a pool present in both the trending and the busiest list counts once."""
    seen: set = set()
    out: List[Pool] = []
    for pools in lists:
        for p in pools:
            if p.address in seen:
                continue
            seen.add(p.address)
            out.append(p)
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
    ``new`` is by the earliest pool's creation time. ``active`` is by distinct
    buying wallets in the window: who is actually here, not how much money.
    ``retrace`` is for the dip thesis: tokens trading well under the high they
    made today that still have buyers.
    """
    rows = [t for t in tokens if include_majors or t.symbol.upper() not in CHAIN_MAJORS]
    if sort == "retrace":
        # Well under its own recent high, but people are still trading it. The
        # score blends the two so a token 40% down with fifty buyers outranks
        # one 90% down with three; the board shows both figures either way.
        rows = [t for t in rows if t.off_high() is not None
                and -RETRACE_MAX_PCT <= t.off_high() <= -RETRACE_MIN_PCT]
        rows.sort(key=lambda t: abs(t.off_high()) * math.sqrt(1 + t.buyers(window)), reverse=True)
    elif sort == "active":
        rows.sort(key=lambda t: (t.buyers(window), t.volume(window)), reverse=True)
    elif sort == "gainers":
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

# What the last request to GeckoTerminal did. Every fetcher below reports
# through ``_fetch`` so a caller can tell "the list is empty" from "the refresh
# failed and this is the cached list": the boards footer it, the feed skips a
# tick on it. ``request_count`` lets the feed meter its own budget.
last_ok_ts = 0.0
last_error = ""          # "" while the most recent request succeeded
last_error_ts = 0.0
request_count = 0

_INCLUDE = "include=base_token,quote_token,dex"


async def _fetch(url: str) -> Optional[Dict]:
    """One GeckoTerminal GET through the shared limiter, recording the outcome."""
    global last_ok_ts, last_error, last_error_ts, request_count
    request_count += 1
    data = await get_json(url, limiter=gecko_limiter)
    now = time.time()
    if data is None:
        last_error = "GeckoTerminal unreachable"
        last_error_ts = now
    else:
        last_ok_ts = now
        last_error = ""
    return data


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
        url = f"{BASE}/networks/{RHCHAIN_NETWORK}/pools?sort=h24_volume_usd_desc&page={page}&{_INCLUDE}"
        pools = parse_pools(await _fetch(url))
        if not pools:
            if page == 1:
                log.debug("GeckoTerminal returned no %s pools", RHCHAIN_NETWORK)
            break
        fresh.extend(pools)

    if fresh:
        _cache[:] = dedup_pools(fresh)
        _cached_at = now
    return _cache


_new_cache: List[Pool] = []
_new_cached_at = 0.0
_new_cached_pages = 0


async def new_pools(force: bool = False, pages: Optional[int] = None) -> List[Pool]:
    """The chain's most recently created pools, newest first, ``pages`` deep
    (default ``RHCHAIN_NEW_PAGES``; one page is 20 pools, about 75 seconds of
    a chain that mints a pool every few seconds).

    A separate feed from the busiest pools: a pair minutes old has no volume
    yet, so it would never appear in ``top_pools``. Most of these are dust;
    the caller decides what to hide. Cached 60s; a cached list fetched with
    fewer pages than asked for is refreshed, one with more is served as is.
    """
    global _new_cached_at, _new_cached_pages
    pages = max(1, int(pages or RHCHAIN_NEW_PAGES))
    now = time.time()
    if _new_cache and not force and (now - _new_cached_at) < RHCHAIN_CACHE_SECONDS and _new_cached_pages >= pages:
        return _new_cache

    fresh: List[Pool] = []
    for page in range(1, pages + 1):
        url = f"{BASE}/networks/{RHCHAIN_NETWORK}/new_pools?page={page}&{_INCLUDE}"
        pools = parse_pools(await _fetch(url))
        if not pools:
            break
        fresh.extend(pools)

    if fresh:
        fresh = dedup_pools(fresh)
        fresh.sort(key=lambda p: -p.created_ts)
        _new_cache[:] = fresh
        _new_cached_at = now
        _new_cached_pages = pages
    return _new_cache


_trending_cache: Dict[str, List[Pool]] = {}
_trending_cached_at: Dict[str, float] = {}


async def trending_pools(duration: str = "5m", force: bool = False) -> List[Pool]:
    """GeckoTerminal's trending pools over a short window, cached 60s per window.

    The busiest-by-24h list cannot see a pool that woke up five minutes ago;
    this one is ranked on recent activity, which is where the feed's spikes
    and movers come from. Same parser, same limiter, one request per window
    per minute. A failed refresh keeps the previous list.
    """
    if duration not in TRENDING_DURATIONS:
        raise ValueError(f"duration must be one of {', '.join(TRENDING_DURATIONS)}")
    now = time.time()
    cached = _trending_cache.get(duration)
    if cached and not force and (now - _trending_cached_at.get(duration, 0.0)) < RHCHAIN_CACHE_SECONDS:
        return cached
    url = f"{BASE}/networks/{RHCHAIN_NETWORK}/trending_pools?duration={duration}&{_INCLUDE}"
    pools = parse_pools(await _fetch(url))
    if pools:
        _trending_cache[duration] = dedup_pools(pools)
        _trending_cached_at[duration] = now
    return _trending_cache.get(duration, [])


def clear_caches() -> None:
    """Forget every cached list and the last-request state (tests, and a
    manual refresh)."""
    global _cached_at, _new_cached_at, _new_cached_pages, last_ok_ts, last_error, last_error_ts
    _cache.clear()
    _new_cache.clear()
    _trending_cache.clear()
    _trending_cached_at.clear()
    _cached_at = _new_cached_at = 0.0
    _new_cached_pages = 0
    last_ok_ts = last_error_ts = 0.0
    last_error = ""


def age_str(created_ts: float, now: Optional[float] = None) -> str:
    """'3m', '2h', '5d' since the pool was created; the unknown mark when unknown."""
    if not created_ts:
        return UNKNOWN
    return age((now if now is not None else time.time()) - created_ts)


# ---------------- how far a token is off its real high ----------------

# The board's off-high figure reads window boundaries and costs nothing. This
# is the true one, from hourly candles, and costs two GeckoTerminal requests —
# so it is fetched only for a token somebody actually picked, and cached.
HIGHS_CACHE_SECONDS = 600
HIGHS_HOURS = 168                      # a week of hourly candles
_highs_cache: Dict[str, Tuple[float, "Highs"]] = {}


@dataclass
class Highs:
    """Where a token trades now against the best it managed recently."""

    price_now: Optional[float] = None
    high_24h: Optional[float] = None
    high_7d: Optional[float] = None
    high_7d_ts: float = 0.0
    hours: int = 0                     # candles actually seen; < 24 means "young"
    series: List[Tuple[float, float]] = field(default_factory=list)   # (ts, close), oldest first

    @staticmethod
    def _off(now: Optional[float], high: Optional[float]) -> Optional[float]:
        if not now or not high or high <= 0:
            return None
        return (now / high - 1.0) * 100.0

    def mc_of(self, price: Optional[float], mc_now: Optional[float]) -> Optional[float]:
        """One of these candle prices as a market cap, so a post can talk in the
        units the rest of McCap uses. Supply does not move over a week, so the
        ratio holds; converting against this series' own last close rather than
        a listing price keeps the high and the figure it is measured from on
        the same footing."""
        if not price or not mc_now or not self.price_now or self.price_now <= 0:
            return None
        return mc_now * price / self.price_now

    @property
    def off_24h(self) -> Optional[float]:
        return self._off(self.price_now, self.high_24h)

    @property
    def off_7d(self) -> Optional[float]:
        return self._off(self.price_now, self.high_7d)


def clear_highs_cache() -> None:
    _highs_cache.clear()


async def price_highs(ca: str, network: str = RHCHAIN_NETWORK, now: Optional[float] = None) -> Optional[Highs]:
    """The token's real 24h and 7d highs against its price now, or None when
    GeckoTerminal cannot say. Never raises: a board or a card renders without
    the figure rather than failing."""
    now = time.time() if now is None else now
    key = f"{network}:{(ca or '').lower()}"
    hit = _highs_cache.get(key)
    if hit and now - hit[0] < HIGHS_CACHE_SECONDS:
        return hit[1]
    try:
        # A card is opened for this figure, so a throttled read is worth one
        # more try; every other GeckoTerminal caller here has a next tick.
        pool = await gecko.top_pool(ca, network, retry_429=1)
        addr = ((pool or {}).get("attributes") or {}).get("address")
        if not addr:
            return None
        candles = await gecko.ohlcv(addr, "hour", 1, HIGHS_HOURS, network, retry_429=1)
    except Exception:
        log.debug("Could not read highs for %s", ca, exc_info=True)
        return None
    rows: List[List[float]] = []
    for row in candles or []:
        try:
            rows.append([float(row[0]), float(row[2]), float(row[4])])   # ts, high, close
        except (TypeError, ValueError, IndexError):
            continue
    if not rows:
        return None
    rows.sort(key=lambda r: -r[0])                                       # newest first
    day = [r for r in rows if now - r[0] <= 86400] or rows[:24]
    best = max(rows, key=lambda r: r[1])
    highs = Highs(
        price_now=rows[0][2],
        high_24h=max(r[1] for r in day),
        high_7d=best[1],
        high_7d_ts=best[0],
        hours=len(rows),
        # The same candles drawn rather than reduced: a chart costs no request
        # of its own, it is what this read already paid for.
        series=[(r[0], r[2]) for r in reversed(rows)],
    )
    _highs_cache[key] = (now, highs)
    if len(_highs_cache) > 300:
        for old in list(_highs_cache)[:100]:
            _highs_cache.pop(old, None)
    return highs
