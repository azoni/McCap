"""Shared aiohttp session + a client-side rate limiter.

The old code opened a fresh ``aiohttp.ClientSession`` for every single request,
which meant a new TCP+TLS handshake per token per poll and no way to bound the
outgoing request rate. One long-lived session plus a token bucket fixes both.
"""

import asyncio
import time
from typing import Any, Dict, Optional

import aiohttp

from .config import DEX_BURST, DEX_MAX_REQUESTS_PER_MIN, HTTP_TIMEOUT_SECONDS, HTTP_USER_AGENT
from .logging_setup import log

_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()


async def get_session() -> aiohttp.ClientSession:
    """Return the process-wide session, creating it on first use."""
    global _session
    if _session is not None and not _session.closed:
        return _session
    async with _session_lock:
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
                headers={"User-Agent": HTTP_USER_AGENT, "Accept": "application/json"},
                connector=aiohttp.TCPConnector(limit=32, ttl_dns_cache=300),
            )
    return _session


async def close_session() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


class RateLimiter:
    """Token bucket. ``acquire()`` waits until a request slot is available.

    ``burst`` is deliberately smaller than the per-minute rate. If the bucket
    started full at ``rate_per_minute`` tokens, a cold start could fire that
    many requests instantly *and* another full rate's worth over the following
    minute — double the intended rate inside one rolling 60s window, which is
    exactly what the upstream limit measures. Keeping ``burst + rate`` under the
    provider's ceiling makes the guarantee hold for any window.
    """

    def __init__(self, rate_per_minute: int, burst: Optional[int] = None):
        self.rate_per_minute = max(1, rate_per_minute)
        self.refill_per_sec = self.rate_per_minute / 60.0
        self.capacity = max(1, burst if burst is not None else min(self.rate_per_minute, 50))
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.updated
                self.updated = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                # Sleep just long enough for one token to regenerate.
                await asyncio.sleep((1.0 - self.tokens) / self.refill_per_sec)


dex_limiter = RateLimiter(DEX_MAX_REQUESTS_PER_MIN, burst=DEX_BURST)


async def get_json(
    url: str,
    *,
    limiter: Optional[RateLimiter] = None,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
) -> Optional[Any]:
    """GET a JSON document. Returns ``None`` on any non-200 or transport error."""
    if limiter is not None:
        await limiter.acquire()
    session = await get_session()
    kwargs: Dict[str, Any] = {}
    if headers:
        kwargs["headers"] = headers
    if params:
        kwargs["params"] = params
    if timeout is not None:
        kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
    try:
        async with session.get(url, **kwargs) as r:
            if r.status == 429:
                log.warning("Rate limited (429) on %s", url.split("?")[0])
                return None
            if r.status != 200:
                log.debug("HTTP %s on %s", r.status, url.split("?")[0])
                return None
            return await r.json(content_type=None)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.debug("Request failed for %s: %s: %s", url.split("?")[0], type(e).__name__, e)
        return None


async def post_json(
    url: str,
    payload: Dict[str, Any],
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
) -> tuple[int, Optional[Any]]:
    """POST JSON and return ``(status, body)``. Status is 0 on transport error."""
    session = await get_session()
    kwargs: Dict[str, Any] = {"json": payload}
    if headers:
        kwargs["headers"] = headers
    if timeout is not None:
        kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
    try:
        async with session.post(url, **kwargs) as r:
            try:
                body = await r.json(content_type=None)
            except Exception:
                body = None
            return r.status, body
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.debug("POST failed for %s: %s: %s", url, type(e).__name__, e)
        return 0, None
