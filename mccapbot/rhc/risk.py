"""Robinhood Chain token risk context from GeckoTerminal's token-info endpoint.

Solana alerts carry holder and dev context from Jupiter; Robinhood Chain tokens
had nothing. One cached ``GET /networks/{network}/tokens/{addr}/info`` gives
the holder count, the top-10 holder share, the developer's share, GeckoTerminal's
honeypot verdict and its overall ``gt_score``. This module fetches and caches
that payload and renders it as the one-line ``Risk:`` context that alerts, the
size card, feed posts and the buy quote append.

What this module must never do:
    - raise into a caller: ``token_info`` returns ``None`` on any failure and
      remembers the failure for a minute so a dead endpoint is not hammered;
    - starve the alert watcher: it shares ``gecko.gecko_limiter`` and skips the
      request entirely when fewer than two tokens are left in the bucket;
    - gate money on its own: holder data lags hours and a minute-old pair has
      none. ``is_honeypot`` is a soft signal for the Engine; the ``plan_buy``
      sell-back round trip stays the hard honeypot defence;
    - format outside ``helpers``: every figure in ``risk_line`` goes through
      ``plural``/``pct``/``footer``/``UNKNOWN``.
"""

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .. import gecko
from ..config import (
    RHC_EXPLORER,
    RHC_GMGN_SLUG,
    RHC_RISK_CACHE_SECONDS,
    RHCHAIN_NETWORK,
    TOP_HOLDER_WARN_PCT,
)
from ..helpers import UNKNOWN, footer, pct, plural
from ..http import get_json
from ..logging_setup import log

# A failed lookup (429, timeout, unknown token) is remembered this long so a
# board of forty tokens does not retry the same dead address every render.
NEGATIVE_CACHE_SECONDS = 60
# Oldest entries are evicted past this; a token seen once is not kept forever.
CACHE_MAX_ENTRIES = 500
# Below this many limiter tokens the request is skipped: the alert watcher's
# backfill and the discovery feed matter more than a context line.
MIN_LIMITER_TOKENS = 2.0
# Developer holdings above this share earn the warning prefix.
DEV_WARN_PCT = 20.0
WARN_PREFIX = "⚠️ "


@dataclass
class TokenInfo:
    """What GeckoTerminal knows about a token; every field may be unknown."""

    holders: Optional[int] = None
    top10_pct: Optional[float] = None
    dev_pct: Optional[float] = None
    honeypot: str = "unknown"          # "yes" | "no" | "unknown"
    gt_score: Optional[float] = None
    verified: bool = False
    socials: int = 0
    # label -> url for whatever the project published (X, Telegram, Discord,
    # Site, ...), so a post can be clicked through instead of copied.
    links: Dict[str, str] = field(default_factory=dict)
    categories: List[str] = field(default_factory=list)
    fetched_ts: float = 0.0


# key -> (info or None for a negative entry, fetched monotonic-free wall ts)
_cache: "OrderedDict[str, Tuple[Optional[TokenInfo], float]]" = OrderedDict()


def clear_cache() -> None:
    """Forget every cached lookup (tests, and a manual refresh)."""
    _cache.clear()


# ---------------- parsing ----------------


def _num(v: Any) -> Optional[float]:
    """A float from a number or numeric string; None for anything else."""
    if v is None or isinstance(v, bool):
        return None
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def _first(d: Dict[str, Any], *keys: str) -> Any:
    """The first present, non-None value among several candidate field names."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def _honeypot(v: Any) -> str:
    """Fold GeckoTerminal's ``is_honeypot`` (bool, string or missing) into yes/no/unknown."""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1"):
            return "yes"
        if s in ("false", "no", "0"):
            return "no"
    return "unknown"


def _socials(a: Dict[str, Any]) -> int:
    """How many places the project can be reached: a website counts once."""
    return len(_links(a))


# Where a handle turns into something clickable. A field already holding a URL
# is used as it is; a bare handle gets its site's prefix.
_LINK_SOURCES = (
    ("X", "twitter_handle", "https://x.com/{}"),
    ("Telegram", "telegram_handle", "https://t.me/{}"),
    ("Discord", "discord_url", "{}"),
    ("Farcaster", "farcaster_url", "{}"),
    ("Zora", "zora_url", "{}"),
)


def _clean_handle(v: str) -> str:
    v = v.strip().lstrip("@")
    for prefix in ("https://x.com/", "https://twitter.com/", "https://t.me/", "http://t.me/"):
        if v.lower().startswith(prefix):
            v = v[len(prefix):]
    return v.strip("/")


def _links(a: Dict[str, Any]) -> Dict[str, str]:
    """``{"X": "https://x.com/ponsdotfamily", "Site": "https://..."}`` — only
    the ones the project actually published, in a fixed order so a feed post
    and a token card list them the same way."""
    out: Dict[str, str] = {}
    for label, key, template in _LINK_SOURCES:
        raw = a.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        value = raw.strip()
        url = value if value.lower().startswith("http") else template.format(_clean_handle(value))
        if url.lower().startswith("http"):
            out[label] = url
    sites = a.get("websites")
    if isinstance(sites, str):
        sites = [sites]
    if isinstance(sites, list):
        first = next((s.strip() for s in sites if isinstance(s, str) and s.strip().lower().startswith("http")), "")
        if first:
            out["Site"] = first
    return out


def parse_info(data: Any, now: Optional[float] = None) -> Optional[TokenInfo]:
    """Build a ``TokenInfo`` from the raw endpoint document.

    Tolerates anything missing: an empty ``attributes`` yields an all-unknown
    record rather than an error. Returns None only when the document has no
    ``data`` object at all (an error body or a non-JSON reply).
    """
    if not isinstance(data, dict):
        return None
    doc = data.get("data")
    if not isinstance(doc, dict):
        return None
    a = doc.get("attributes")
    if not isinstance(a, dict):
        a = {}

    holders_raw = a.get("holders")
    holders: Optional[int] = None
    top10: Optional[float] = None
    if isinstance(holders_raw, dict):
        h = _num(_first(holders_raw, "count", "total", "holders"))
        holders = int(h) if h is not None and h >= 0 else None
        dist = holders_raw.get("distribution_percentage")
        if isinstance(dist, dict):
            top10 = _num(_first(dist, "top_10", "top10", "top_10_percentage"))
        if top10 is None:
            top10 = _num(_first(holders_raw, "top_10_percentage", "top_holders_percentage", "top10_pct"))
    else:
        h = _num(_first(a, "holders", "holders_count", "holder_count"))
        holders = int(h) if h is not None and h >= 0 else None
    if top10 is None:
        top10 = _num(_first(a, "top_10_holders_percentage", "top_holders_percentage", "top_10_percentage"))

    dev = _num(_first(a, "developer_holding_percentage", "dev_holding_percentage", "developer_percentage"))
    score = _num(_first(a, "gt_score", "score"))
    cats_raw = a.get("categories")
    cats = [str(c) for c in cats_raw if c] if isinstance(cats_raw, list) else []

    return TokenInfo(
        holders=holders,
        top10_pct=top10,
        dev_pct=dev,
        honeypot=_honeypot(a.get("is_honeypot")),
        gt_score=score,
        verified=bool(a.get("gt_verified") or a.get("verified") or False),
        socials=_socials(a),
        links=_links(a),
        categories=cats,
        fetched_ts=now if now is not None else time.time(),
    )


# ---------------- fetching + cache ----------------


def _limiter_available() -> float:
    """Tokens in the shared GeckoTerminal bucket right now, refill included.

    ``RateLimiter.tokens`` is only brought up to date inside ``acquire()``, so a
    bucket that has sat idle looks emptier than it is; add the refill since the
    last update the same way ``acquire`` would. Uses ``available()`` when the
    limiter grows one, reading the plain attributes otherwise.
    """
    lim = gecko.gecko_limiter
    avail = getattr(lim, "available", None)
    if callable(avail):
        try:
            return float(avail())
        except Exception:
            pass
    try:
        tokens = float(getattr(lim, "tokens", 0.0))
        capacity = float(getattr(lim, "capacity", tokens))
        refill = float(getattr(lim, "refill_per_sec", 0.0))
        updated = float(getattr(lim, "updated", time.monotonic()))
        return min(capacity, tokens + max(0.0, time.monotonic() - updated) * refill)
    except (TypeError, ValueError):
        return 0.0


def _key(addr: str, network: str) -> str:
    return f"{network}:{(addr or '').strip().lower()}"


def _remember(key: str, info: Optional[TokenInfo], now: float) -> None:
    if key in _cache:
        del _cache[key]
    _cache[key] = (info, now)
    while len(_cache) > CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)


def _cached(key: str, now: float) -> Tuple[bool, Optional[TokenInfo]]:
    """``(fresh, info)``: ``fresh`` says whether the entry may be served as is."""
    hit = _cache.get(key)
    if hit is None:
        return False, None
    info, ts = hit
    ttl = RHC_RISK_CACHE_SECONDS if info is not None else NEGATIVE_CACHE_SECONDS
    return (now - ts) < ttl, info


async def token_info(addr: str, network: str = RHCHAIN_NETWORK) -> Optional[TokenInfo]:
    """GeckoTerminal's token info, cached; None when unavailable. Never raises.

    A hit inside ``RHC_RISK_CACHE_SECONDS`` (or a failure inside
    ``NEGATIVE_CACHE_SECONDS``) costs no request. When the shared limiter is
    nearly empty the request is skipped and whatever the cache holds, fresh or
    stale, is returned so a context line never delays a trade or a backfill.
    """
    if not addr:
        return None
    key = _key(addr, network)
    now = time.time()
    fresh, info = _cached(key, now)
    if fresh:
        return info
    if _limiter_available() < MIN_LIMITER_TOKENS:
        return info
    try:
        data = await get_json(f"{gecko.BASE}/networks/{network}/tokens/{addr}/info", limiter=gecko.gecko_limiter)
        parsed = parse_info(data, now) if data else None
    except Exception as e:  # noqa: BLE001 - a context line must never take a caller down
        log.debug("token_info failed for %s on %s: %s: %s", addr, network, type(e).__name__, e)
        parsed = None
    _remember(key, parsed, now)
    return parsed


# ---------------- rendering ----------------


def is_honeypot(info: Optional[TokenInfo]) -> bool:
    """True only on an explicit GeckoTerminal "yes"; unknown is not a verdict."""
    return info is not None and info.honeypot == "yes"


def is_risky(info: Optional[TokenInfo]) -> bool:
    """Whether the risk line earns its warning prefix."""
    if info is None:
        return False
    if info.top10_pct is not None and info.top10_pct > TOP_HOLDER_WARN_PCT:
        return True
    if info.dev_pct is not None and info.dev_pct > DEV_WARN_PCT:
        return True
    return info.honeypot == "yes"


def risk_line(info: Optional[TokenInfo]) -> str:
    """``Risk: 8,120 holders · top-10 **73.6%** · dev 4% · gt 62 · honeypot: no``.

    Unknown parts are left out; with nothing known the line is ``Risk: —``.
    The top-10 share is the one bold figure. A concentrated top-10, a heavy
    developer bag or a honeypot verdict prefixes the line with a warning.
    """
    if info is None:
        return f"Risk: {UNKNOWN}"
    parts: List[str] = []
    if info.holders is not None:
        parts.append(plural(info.holders, "holder"))
    if info.top10_pct is not None:
        parts.append(f"top-10 **{pct(info.top10_pct, signed=False)}**")
    if info.dev_pct is not None:
        parts.append(f"dev {pct(info.dev_pct, signed=False)}")
    if info.gt_score is not None:
        parts.append(f"gt {info.gt_score:.0f}")
    if info.honeypot != "unknown":
        parts.append(f"honeypot: {info.honeypot}")
    if not parts:
        return f"Risk: {UNKNOWN}"
    prefix = WARN_PREFIX if is_risky(info) else ""
    return f"{prefix}Risk: {footer(*parts)}"


def links_line(info: Optional[TokenInfo], ca: str = "", network: str = RHCHAIN_NETWORK) -> str:
    """The token's own links plus the ones McCap can always build: its chart,
    GMGN and the explorer. Markdown, so a post is one click from the project's
    X account instead of a copy-paste. Empty only when there is no address."""
    parts: List[str] = []
    for label, url in (info.links if info is not None else {}).items():
        parts.append(f"[{label}]({url})")
    if ca:
        parts.append(f"[Chart](https://www.geckoterminal.com/{network}/tokens/{ca})")
        if RHC_GMGN_SLUG:
            parts.append(f"[GMGN](https://gmgn.ai/{RHC_GMGN_SLUG}/token/{ca})")
        parts.append(f"[Explorer]({RHC_EXPLORER}/token/{ca})")
    return footer(*parts)


__all__ = [
    "TokenInfo", "token_info", "parse_info", "risk_line", "links_line", "is_honeypot", "is_risky",
    "clear_cache", "NEGATIVE_CACHE_SECONDS", "CACHE_MAX_ENTRIES", "MIN_LIMITER_TOKENS",
]
