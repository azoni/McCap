"""Jupiter token data — holder/risk context and a market-cap fallback.

Two jobs DexScreener cannot do:

1. **Risk context.** Holder count, top-10 concentration, mint/freeze authority and
   dev mint count. A market cap alone says a token moved; these say whether the
   move is worth acting on.

2. **Market-cap fallback.** DexScreener stops returning pairs for tokens whose
   pools have thinned out, which made 12 of this bot's tracked tokens permanently
   unfireable. Jupiter still reports a market cap for 10 of those 12.

Batching is the reason this is cheap: up to 100 mints resolve in one request, so
the entire watchlist costs a single call — unlike DexScreener, where the
multi-address form silently drops tokens (see dex.fetch_dex_token).

Solana only. EVM addresses are filtered out before any request is made.
"""

import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from .config import (
    JUPITER_BATCH,
    JUPITER_MAX_REQUESTS_PER_MIN,
    JUPITER_TIMEOUT,
    JUPITER_URL,
)
from .helpers import is_solana_address
from .http import RateLimiter, get_json
from .logging_setup import log

jupiter_limiter = RateLimiter(JUPITER_MAX_REQUESTS_PER_MIN, burst=5)


@dataclass
class JupToken:
    """The subset of Jupiter's payload McCap actually displays."""

    ca: str
    symbol: str = ""
    name: str = ""
    mcap: Optional[float] = None
    holders: Optional[int] = None
    top10_pct: Optional[float] = None
    mint_disabled: Optional[bool] = None
    freeze_disabled: Optional[bool] = None
    dev_mints: Optional[int] = None
    organic: str = ""
    liquidity: Optional[float] = None
    fetched_ts: float = 0.0

    def concentrated(self, warn_pct: float) -> bool:
        return self.top10_pct is not None and self.top10_pct >= warn_pct

    def authorities_live(self) -> bool:
        """True when mint or freeze authority is still active — a real red flag."""
        return self.mint_disabled is False or self.freeze_disabled is False


def _num(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse(raw: Dict) -> Optional[JupToken]:
    ca = raw.get("id")
    if not ca:
        return None
    audit = raw.get("audit") or {}
    holders = raw.get("holderCount")
    return JupToken(
        ca=ca,
        symbol=raw.get("symbol") or "",
        name=raw.get("name") or "",
        mcap=_num(raw.get("mcap")),
        holders=int(holders) if isinstance(holders, (int, float)) else None,
        top10_pct=_num(audit.get("topHoldersPercentage")),
        mint_disabled=audit.get("mintAuthorityDisabled"),
        freeze_disabled=audit.get("freezeAuthorityDisabled"),
        dev_mints=audit.get("devMints"),
        organic=raw.get("organicScoreLabel") or "",
        liquidity=_num(raw.get("liquidity")),
        fetched_ts=time.time(),
    )


def _chunks(items: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def fetch_many(mints: Iterable[str]) -> Dict[str, JupToken]:
    """Look up many mints at once. Returns only what resolved.

    Never raises: enrichment is decoration, and a Jupiter outage must not stop an
    alert from firing.
    """
    wanted = sorted({m for m in mints if is_solana_address(m)})
    if not wanted:
        return {}

    out: Dict[str, JupToken] = {}
    for batch in _chunks(wanted, max(1, JUPITER_BATCH)):
        data = await get_json(
            JUPITER_URL,
            params={"query": ",".join(batch)},
            limiter=jupiter_limiter,
            timeout=JUPITER_TIMEOUT,
        )
        if not isinstance(data, list):
            log.debug("Jupiter returned %s for a %d-mint batch", type(data).__name__, len(batch))
            continue
        for raw in data:
            tok = _parse(raw) if isinstance(raw, dict) else None
            if tok:
                out[tok.ca] = tok
    return out


async def fetch_one(mint: str) -> Optional[JupToken]:
    return (await fetch_many([mint])).get(mint)


def risk_line(tok: Optional[JupToken], warn_pct: float) -> str:
    """One-line holder/risk summary for an embed, or "" if nothing is known.

    Deliberately reports only what Jupiter actually returned — a missing field is
    omitted rather than rendered as zero.
    """
    if tok is None:
        return ""
    bits: List[str] = []
    if tok.holders is not None:
        bits.append(f"{tok.holders:,} holders")
    if tok.top10_pct is not None:
        flag = "⚠️ " if tok.concentrated(warn_pct) else ""
        bits.append(f"{flag}top 10 hold {tok.top10_pct:.0f}%")
    if tok.mint_disabled is False:
        bits.append("⚠️ mint authority live")
    if tok.freeze_disabled is False:
        bits.append("⚠️ freeze authority live")
    if tok.dev_mints:
        bits.append(f"dev minted {tok.dev_mints}")
    if tok.organic:
        bits.append(f"organic: {tok.organic}")
    return " · ".join(bits)
