"""GeckoTerminal client — historical OHLCV used to seed momentum alerts.

Momentum alerts compare the current market cap against a baseline from the
past, so a freshly restarted bot is blind until its window refills. DexScreener
only reports the present. GeckoTerminal serves free historical candles with no
API key, which lets a restart backfill that baseline in one call per token and
arm immediately.

Candles are prices, not market caps. Supply is effectively constant over an
alert window, so historical market cap is derived by scaling each close against
the live market cap we already trust from DexScreener's consensus.
"""

from typing import Dict, List, Optional, Tuple

from .constants import MAJOR_QUOTES
from .http import RateLimiter, get_json
from .logging_setup import log

BASE = "https://api.geckoterminal.com/api/v2"

# The free tier allows roughly 30 calls/min and needs no key. Kept well under,
# and separate from the DexScreener bucket so backfill can never starve the
# alert watcher.
gecko_limiter = RateLimiter(20, burst=8)


def _timeframe_for(window_sec: int) -> Tuple[str, int, int]:
    """Pick (timeframe, aggregate, limit) that comfortably covers a window.

    Enough candles to span the window with room to spare, without asking for
    minute data across days.
    """
    if window_sec <= 3600:
        return "minute", 5, 60        # 5m candles -> up to 5h
    if window_sec <= 6 * 3600:
        return "minute", 15, 60       # 15m candles -> up to 15h
    if window_sec <= 2 * 86400:
        return "hour", 1, 72          # 1h candles -> up to 3d
    return "hour", 4, 90              # 4h candles -> up to 15d


async def top_pool(ca: str, network: str = "solana") -> Optional[Dict]:
    """Deepest pool for a token, preferring a recognised quote asset.

    Same lesson as the 24h-change fix: the deepest pool overall can be quoted in
    a junk token, which makes its price series meaningless.
    """
    data = await get_json(f"{BASE}/networks/{network}/tokens/{ca}/pools?page=1", limiter=gecko_limiter)
    if not data or not data.get("data"):
        return None

    def reserve(p: Dict) -> float:
        try:
            return float((p.get("attributes") or {}).get("reserve_in_usd") or 0)
        except (TypeError, ValueError):
            return 0.0

    def major(p: Dict) -> bool:
        # Pool names look like "BONK / SOL"; the quote is the right-hand side.
        name = ((p.get("attributes") or {}).get("name") or "")
        parts = [x.strip().upper() for x in name.split("/")]
        return len(parts) == 2 and parts[1] in MAJOR_QUOTES

    pools = data["data"]
    preferred = [p for p in pools if major(p)] or pools
    return max(preferred, key=reserve, default=None)


async def ohlcv(
    pool_address: str,
    timeframe: str,
    aggregate: int,
    limit: int,
    network: str = "solana",
) -> List[List[float]]:
    """Return candles as [ts, open, high, low, close, volume], oldest last."""
    url = (
        f"{BASE}/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}"
        f"?aggregate={aggregate}&limit={limit}"
    )
    data = await get_json(url, limiter=gecko_limiter)
    if not data:
        return []
    try:
        return data["data"]["attributes"]["ohlcv_list"] or []
    except (KeyError, TypeError):
        return []


async def history_points(
    ca: str,
    window_sec: int,
    current_mc: float,
    network: str = "solana",
) -> List[Tuple[float, float]]:
    """Historical (timestamp, market_cap) points covering the window.

    Returns [] when the token can't be resolved — callers must treat that as
    "no baseline available", never as a flat price.
    """
    if not current_mc or current_mc <= 0:
        return []

    pool = await top_pool(ca, network)
    if not pool:
        return []
    pool_addr = (pool.get("attributes") or {}).get("address")
    if not pool_addr:
        return []

    timeframe, aggregate, limit = _timeframe_for(window_sec)
    candles = await ohlcv(pool_addr, timeframe, aggregate, limit, network)
    if len(candles) < 2:
        return []

    # Newest first from the API; the newest close corresponds to "now", which is
    # the market cap DexScreener already gave us.
    try:
        latest_close = float(candles[0][4])
    except (TypeError, ValueError, IndexError):
        return []
    if latest_close <= 0:
        return []

    scale = current_mc / latest_close
    points: List[Tuple[float, float]] = []
    for row in candles:
        try:
            ts, close = float(row[0]), float(row[4])
        except (TypeError, ValueError, IndexError):
            continue
        if close > 0:
            points.append((ts, close * scale))

    points.sort(key=lambda p: p[0])
    return points


async def backfill(ca: str, window_sec: int, current_mc: float) -> int:
    """Seed a token's history from GeckoTerminal. Returns samples added."""
    from . import history

    points = await history_points(ca, window_sec, current_mc)
    if not points:
        log.debug("No GeckoTerminal history for %s", ca)
        return 0
    added = history.seed(ca, points)
    log.info(
        "Backfilled %d historical sample(s) for %s (%.1fh of history)",
        added, ca, history.span_seconds(ca) / 3600,
    )
    return added
