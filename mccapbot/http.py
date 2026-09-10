"""Shared aiohttp session + a client-side rate limiter.

The old code opened a fresh ``aiohttp.ClientSession`` for every single request,
which meant a new TCP+TLS handshake per token per poll and no way to bound the
outgoing request rate. One long-lived session plus a token bucket fixes both.
"""

import asyncio
import time
from typing import Any, Dict, Optional, Tuple

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
        self.strikes = 0                    # consecutive refusals from the provider
        self.hold_until = 0.0               # monotonic time this bucket may fire again
        self._lock = asyncio.Lock()

    def penalise(self) -> float:
        """The provider refused a request as too many. Our own bucket is under
        its per-minute ceiling, so the disagreement is theirs to settle: a
        shared egress IP, a shorter window than the documented one, a bad
        minute. Either way, asking again at the same rate spends the budget on
        answers we will not get, so the bucket empties and holds — longer each
        time, until something gets through. Returns the hold in seconds."""
        self.strikes += 1
        hold = min(PENALTY_MAX, PENALTY_BASE * (2 ** (self.strikes - 1)))
        self.tokens = 0.0
        self.updated = time.monotonic()
        self.hold_until = max(self.hold_until, self.updated + hold)
        return hold

    def forgive(self) -> None:
        """Something came back. Whatever the provider was unhappy about has
        passed, so the next refusal starts the backoff over rather than
        resuming where a burst an hour ago left off."""
        self.strikes = 0

    def held_for(self) -> float:
        """Seconds until this bucket may fire again; 0 when it is free."""
        return max(0.0, self.hold_until - time.monotonic())

    def available(self) -> float:
        """Tokens a caller could take right now, refill applied, nothing consumed.

        ``tokens`` alone goes stale between acquires: it only moves when
        someone acquires, so a bucket that emptied a minute ago still reads
        as empty. The discovery feed yields to interactive callers when the
        bucket is under half, and needs the refilled figure to decide that.
        Pure: the bucket itself is not written, so a concurrent ``acquire``
        (which may be sleeping while holding the lock) sees exactly the state
        it left. A bucket the provider has refused reads as empty for as long
        as it is held, which is how the feed and the holder lookups stand down
        of their own accord instead of each having to know about 429s.
        """
        if time.monotonic() < self.hold_until:
            return 0.0
        elapsed = max(0.0, time.monotonic() - self.updated)
        return min(float(self.capacity), self.tokens + elapsed * self.refill_per_sec)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                if now < self.hold_until:
                    await asyncio.sleep(self.hold_until - now)
                    continue
                elapsed = now - self.updated
                self.updated = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                # Sleep just long enough for one token to regenerate.
                await asyncio.sleep((1.0 - self.tokens) / self.refill_per_sec)


dex_limiter = RateLimiter(DEX_MAX_REQUESTS_PER_MIN, burst=DEX_BURST)

RETRY_429_DELAY = 2.5           # seconds before an opt-in second attempt
PENALTY_BASE = 20.0             # first hold after a refusal, doubling per consecutive strike
PENALTY_MAX = 300.0


async def get_json(
    url: str,
    *,
    limiter: Optional[RateLimiter] = None,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
    retry_429: int = 0,
) -> Optional[Any]:
    """GET a JSON document. Returns ``None`` on any non-200 or transport error.

    ``retry_429`` is for the few reads whose absence a person would notice — a
    token card losing the one figure it was opened for, say. Our own bucket
    keeps us under the per-minute ceiling, but a provider that also measures a
    shorter window can still refuse a burst it will happily serve a moment
    later. Off by default: a caller with a cache or a next tick should not pay
    for a second attempt.
    """
    for attempt in range(max(0, int(retry_429)) + 1):
        if attempt:
            await asyncio.sleep(RETRY_429_DELAY * attempt)
        got, throttled = await _get_json_once(url, limiter=limiter, headers=headers,
                                              params=params, timeout=timeout)
        if not throttled:
            return got
    return None


async def _get_json_once(
    url: str,
    *,
    limiter: Optional[RateLimiter] = None,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
) -> Tuple[Optional[Any], bool]:
    """The document, and whether the provider refused it as too many requests
    (the one failure worth trying again)."""
    if limiter is not None:
        # A held bucket means the last request was refused. Waiting it out here
        # would hang whatever asked — a slash command has seconds, not minutes —
        # and the callers all have a cached answer or a next tick. So say no
        # now, and let the board render its previous list with a stale note.
        held = limiter.held_for()
        if held > 0:
            log.debug("Skipping %s: bucket held for another %.0fs", url.split("?")[0], held)
            return None, True
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
                if limiter is not None:
                    log.warning("Rate limited (429) on %s — holding this bucket %.0fs",
                                url.split("?")[0], limiter.penalise())
                else:
                    log.warning("Rate limited (429) on %s", url.split("?")[0])
                return None, True
            if limiter is not None:
                limiter.forgive()
            if r.status != 200:
                log.debug("HTTP %s on %s", r.status, url.split("?")[0])
                return None, False
            return await r.json(content_type=None), False
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.debug("Request failed for %s: %s: %s", url.split("?")[0], type(e).__name__, e)
        return None, False


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
