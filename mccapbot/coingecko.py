"""CoinGecko market data — price and movement for mainstream coins.

Robinhood's Crypto API is execution-only: accounts, holdings, orders, trading
pairs and quotes. It has no trending, movers or discovery endpoint, and its
quote endpoint returns a current bid/ask with no historical reference, so
"what's moving" cannot be answered from Robinhood alone.

CoinGecko fills that in: free, keyless, and one request returns the whole top
250 with 1h/24h/7d change. DexScreener and Jupiter are no use here — they cover
DEX pairs and Solana mints, not BTC or ETH.
"""

import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from .config import COINGECKO_TIMEOUT, COINGECKO_TOP_N, COINGECKO_URL
from .http import RateLimiter, get_json
from .logging_setup import log

# CoinGecko's keyless tier is roughly 10-30 calls/min and publishes no headers,
# so stay far below anything that could look abusive.
gecko_limiter = RateLimiter(12, burst=3)

_cache: Dict[str, "Coin"] = {}
_cached_at = 0.0
CACHE_SECONDS = 60


@dataclass
class Coin:
    symbol: str
    name: str
    price: Optional[float]
    change_1h: Optional[float]
    change_24h: Optional[float]
    change_7d: Optional[float]
    market_cap: Optional[float]


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def top_markets(force: bool = False) -> Dict[str, Coin]:
    """Top coins keyed by upper-case symbol, cached briefly.

    Ordered by market cap, so when two coins share a ticker the larger one wins
    — the symbol collision that would otherwise put a micro-cap impostor next to
    a Robinhood listing.
    """
    global _cached_at
    now = time.time()
    if _cache and not force and (now - _cached_at) < CACHE_SECONDS:
        return _cache

    data = await get_json(
        COINGECKO_URL,
        params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": str(COINGECKO_TOP_N),
            "page": "1",
            "price_change_percentage": "1h,24h,7d",
        },
        limiter=gecko_limiter,
        timeout=COINGECKO_TIMEOUT,
    )
    if not isinstance(data, list):
        log.debug("CoinGecko returned %s", type(data).__name__)
        return _cache  # keep whatever we had rather than blanking it

    fresh: Dict[str, Coin] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = (row.get("symbol") or "").upper()
        if not sym or sym in fresh:      # first (largest) wins
            continue
        fresh[sym] = Coin(
            symbol=sym,
            name=row.get("name") or sym,
            price=_f(row.get("current_price")),
            change_1h=_f(row.get("price_change_percentage_1h_in_currency")),
            change_24h=_f(row.get("price_change_percentage_24h_in_currency")),
            change_7d=_f(row.get("price_change_percentage_7d_in_currency")),
            market_cap=_f(row.get("market_cap")),
        )

    if fresh:
        _cache.clear()
        _cache.update(fresh)
        _cached_at = now
    return _cache


async def for_symbols(symbols: Iterable[str]) -> List[Coin]:
    """Market data for specific tickers, in no particular order."""
    markets = await top_markets()
    out = []
    for s in symbols:
        coin = markets.get(s.upper())
        if coin:
            out.append(coin)
    return out
