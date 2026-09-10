"""The limiter is the last line of defence against getting 429'd."""

import asyncio
import time

import pytest

from mccapbot.config import DEX_BURST, DEX_MAX_REQUESTS_PER_MIN
from mccapbot import http
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


def test_available_applies_the_refill_without_consuming():
    """``tokens`` only moves on acquire, so a bucket drained a while ago still
    reads as empty; the feed's yield check needs the refilled figure."""
    lim = RateLimiter(60, burst=4)           # 1 token/sec
    lim.tokens = 0.0
    lim.updated = time.monotonic() - 2.0
    got = lim.available()
    assert 1.9 <= got <= 2.3
    assert lim.tokens == 0.0, "reading must not take a token"
    assert lim.available() <= lim.capacity
    lim.updated = time.monotonic() - 60.0
    assert lim.available() == pytest.approx(lim.capacity), "capped at the burst"


# ---------------- the one failure worth trying again ----------------


class _Response:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._payload


class _Session:
    """Answers with the queued statuses in order, then repeats the last."""

    def __init__(self, *statuses):
        self.queue = list(statuses)
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        status = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        return _Response(status, {"ok": status == 200})


def _patch(monkeypatch, session):
    async def get_session():
        return session

    async def no_sleep(_):
        return None

    monkeypatch.setattr(http, "get_session", get_session)
    monkeypatch.setattr(http.asyncio, "sleep", no_sleep)


@pytest.mark.asyncio
async def test_a_throttled_read_is_not_retried_unless_the_caller_asked(monkeypatch):
    """Almost every caller here has a cache or a next tick. Paying for a second
    attempt by default would multiply exactly the traffic that caused the 429."""
    session = _Session(429)
    _patch(monkeypatch, session)
    assert await http.get_json("https://example/x") is None
    assert session.calls == 1


@pytest.mark.asyncio
async def test_an_opt_in_retry_asks_again_and_takes_the_answer(monkeypatch):
    session = _Session(429, 200)
    _patch(monkeypatch, session)
    assert await http.get_json("https://example/x", retry_429=1) == {"ok": True}
    assert session.calls == 2


@pytest.mark.asyncio
async def test_a_retry_gives_up_rather_than_hammering(monkeypatch):
    session = _Session(429)
    _patch(monkeypatch, session)
    assert await http.get_json("https://example/x", retry_429=2) is None
    assert session.calls == 3, "the first attempt plus the two that were asked for"


@pytest.mark.asyncio
async def test_a_plain_failure_is_never_retried_however_many_were_offered(monkeypatch):
    """A 404 will still be a 404 in two seconds; only a rate limit changes."""
    session = _Session(404)
    _patch(monkeypatch, session)
    assert await http.get_json("https://example/x", retry_429=3) is None
    assert session.calls == 1
