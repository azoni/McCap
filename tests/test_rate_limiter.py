"""The limiter is the last line of defence against getting 429'd."""

import asyncio
import time

import pytest

from mccapbot.config import DEX_BURST, DEX_MAX_REQUESTS_PER_MIN
from mccapbot.http import RateLimiter

DEX_HARD_LIMIT = 300  # DexScreener's documented token-endpoint limit, per minute


def test_burst_is_capped_below_the_rate():
    """A full bucket equal to the rate would double the effective rate.

    Cold start: `capacity` requests fire instantly, then `rate` more arrive over
    the following 60 seconds — all inside one rolling window.
    """
    lim = RateLimiter(240)
    assert lim.capacity < lim.rate_per_minute
    assert lim.capacity + lim.rate_per_minute <= DEX_HARD_LIMIT


def test_configured_defaults_respect_the_upstream_limit():
    assert DEX_BURST + DEX_MAX_REQUESTS_PER_MIN <= DEX_HARD_LIMIT


def test_burst_then_throttle():
    """Burst tokens are free; the next one waits for a refill."""

    async def run():
        lim = RateLimiter(60, burst=2)  # 2 free, then 1/sec
        t0 = time.perf_counter()
        await lim.acquire()
        await lim.acquire()
        burst_span = time.perf_counter() - t0
        assert burst_span < 0.2, "burst tokens should not block"

        t1 = time.perf_counter()
        await lim.acquire()
        return time.perf_counter() - t1

    throttled = asyncio.run(run())
    assert throttled >= 0.7, f"third request should have waited ~1s, waited {throttled:.2f}s"


def test_sustained_rate_is_honoured():
    """Draining past the burst settles to the configured rate."""

    async def run():
        lim = RateLimiter(120, burst=1)  # 2/sec sustained
        await lim.acquire()  # consume the burst
        t0 = time.perf_counter()
        for _ in range(4):
            await lim.acquire()
        return time.perf_counter() - t0

    span = asyncio.run(run())
    # 4 requests at 2/sec ~= 2s; allow slack for scheduler jitter.
    assert 1.2 <= span <= 3.5, f"expected ~2s of throttling, got {span:.2f}s"


def test_explicit_burst_is_respected():
    lim = RateLimiter(600, burst=5)
    assert lim.capacity == 5
    assert lim.tokens == pytest.approx(5.0)


def test_tiny_rate_still_usable():
    lim = RateLimiter(1)
    assert lim.capacity >= 1
    assert lim.tokens >= 1
