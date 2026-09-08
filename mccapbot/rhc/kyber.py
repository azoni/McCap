"""KyberSwap aggregator on Robinhood Chain.

Two calls per trade: ``GET /routes`` for the best path and its USD figures, then
``POST /route/build`` for the calldata. The aggregator sees every DEX on the
chain (Uniswap V2/V3/V4, Ramses, Pons, ...) and encodes the swap itself, which
is also how we stay clear of the Robinhood-forked Uniswap V4 router that breaks
stock SDK encodings on token/token pools.

Nothing in a response is trusted on its own. The routerAddress must equal the
pinned canonical address in config; the value and input amount in the built
transaction must equal the route we asked for. And "no route" is kept apart from
"Kyber did not answer": the first is a fact about the token (and what the
honeypot check keys on), the second is a fact about the network.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from ..config import RHC_KYBER_CLIENT_ID, RHC_KYBER_ROUTER, RHC_KYBER_URL
from ..http import RateLimiter, get_session, post_json
from ..logging_setup import log
from .chain import NATIVE, to_checksum

# KyberSwap publishes no keyless quota; stay conservative and identify ourselves.
kyber_limiter = RateLimiter(30, burst=5)
HEADERS = {"X-Client-Id": RHC_KYBER_CLIENT_ID}

# A routeSummary is a snapshot; Kyber says not to reuse one beyond 5-10 seconds.
ROUTE_MAX_AGE = 10.0
# Kyber's "route not found" code. Other non-zero codes are API errors, not facts
# about the token, and must not read as "honeypot".
NO_ROUTE_CODE = 4008


class KyberError(Exception):
    """Base for aggregator failures."""


class NoRoute(KyberError):
    """Kyber answered: there is no way to swap this pair (or the token is unknown)."""


class KyberUnavailable(KyberError):
    """Kyber did not answer usefully: rate limit, outage, transport failure."""


@dataclass
class Route:
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int
    amount_in_usd: float
    amount_out_usd: float
    gas: int
    gas_usd: float
    router: str
    summary: Dict[str, Any]
    hops: List[str] = field(default_factory=list)
    fetched_ts: float = field(default_factory=time.time)

    @property
    def price_impact_pct(self) -> Optional[float]:
        if self.amount_in_usd <= 0:
            return None
        return (self.amount_out_usd / self.amount_in_usd - 1.0) * 100.0

    def is_stale(self, now: Optional[float] = None) -> bool:
        return ((now if now is not None else time.time()) - self.fetched_ts) > ROUTE_MAX_AGE


@dataclass
class BuiltSwap:
    router: str
    data: str            # 0x calldata for the router
    value: int           # wei to send with the call (the input amount for ETH-in swaps)
    amount_in: int
    amount_out: int
    amount_in_usd: float
    amount_out_usd: float
    gas: int
    gas_usd: float
    slippage_bps: int
    min_out: int
    route: Route

    @property
    def is_buy(self) -> bool:
        return self.route.token_in.lower() == NATIVE.lower()


def min_out(amount_out: int, slippage_bps: int) -> int:
    return (int(amount_out) * (10_000 - int(slippage_bps))) // 10_000


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _addr(a: str) -> str:
    return a if a.lower() == NATIVE.lower() else to_checksum(a)


async def _get(url: str) -> Dict[str, Any]:
    """GET with the status code kept, so a 429 is not mistaken for 'no route'."""
    await kyber_limiter.acquire()
    session = await get_session()
    try:
        async with session.get(url, headers=HEADERS) as r:
            status = r.status
            try:
                body = await r.json(content_type=None)
            except Exception:
                body = None
    except asyncio.CancelledError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise KyberUnavailable(f"KyberSwap is unreachable ({type(e).__name__}). Try again shortly.") from e
    if status == 429:
        raise KyberUnavailable("KyberSwap is rate-limiting us. Try again in a minute.")
    if status >= 500:
        raise KyberUnavailable(f"KyberSwap is having trouble (HTTP {status}). Try again shortly.")
    if not isinstance(body, dict):
        raise KyberUnavailable(f"KyberSwap returned an unreadable answer (HTTP {status}).")
    if status != 200:
        msg = str(body.get("message") or body.get("error") or f"HTTP {status}")
        raise KyberError(f"KyberSwap: {msg}")
    return body


async def route(token_in: str, token_out: str, amount_in: int) -> Route:
    """Best route for ``amount_in`` of ``token_in`` into ``token_out``."""
    if amount_in <= 0:
        raise KyberError("Amount must be greater than zero.")
    url = (
        f"{RHC_KYBER_URL}/routes?tokenIn={_addr(token_in)}&tokenOut={_addr(token_out)}"
        f"&amountIn={int(amount_in)}&gasInclude=true"
    )
    data = await _get(url)
    body = data.get("data") or {}
    rs = body.get("routeSummary") if isinstance(body, dict) else None
    code = data.get("code")
    if code not in (0, None) or not isinstance(rs, dict) or not rs.get("amountOut"):
        msg = str(data.get("message") or "no route")
        looks_like_no_route = (
            code == NO_ROUTE_CODE
            or code in (0, None)                       # a 200 with nothing in it
            or any(k in msg.lower() for k in ("route", "liquidity", "no pool"))
        )
        if looks_like_no_route:
            raise NoRoute(f"No route for that pair on Robinhood Chain ({msg}): no liquidity, or an unknown token.")
        raise KyberError(f"KyberSwap error {code}: {msg}")
    hops = []
    for leg in rs.get("route") or []:
        for hop in leg or []:
            ex = str((hop or {}).get("exchange") or "")
            if ex and ex not in hops:
                hops.append(ex)
    return Route(
        token_in=str(rs.get("tokenIn") or token_in),
        token_out=str(rs.get("tokenOut") or token_out),
        amount_in=int(rs.get("amountIn") or amount_in),
        amount_out=int(rs["amountOut"]),
        amount_in_usd=_f(rs.get("amountInUsd")),
        amount_out_usd=_f(rs.get("amountOutUsd")),
        gas=int(_f(rs.get("gas"))),
        gas_usd=_f(rs.get("gasUsd")),
        router=str(body.get("routerAddress") or ""),
        summary=rs,
        hops=hops,
    )


async def build(rt: Route, sender: str, slippage_bps: int, recipient: Optional[str] = None) -> BuiltSwap:
    """Calldata for a route. ``sender`` pays and, by default, receives."""
    if rt.is_stale():
        raise KyberError("That quote is stale; get a fresh one.")
    payload = {
        "routeSummary": rt.summary,
        "sender": to_checksum(sender),
        "recipient": to_checksum(recipient or sender),
        "slippageTolerance": int(slippage_bps),
        "source": RHC_KYBER_CLIENT_ID,
        "enableGasEstimation": False,
    }
    status, body = await post_json(f"{RHC_KYBER_URL}/route/build", payload, headers=HEADERS)
    if status == 429 or status >= 500 or status == 0:
        raise KyberUnavailable(f"KyberSwap could not build the transaction right now (HTTP {status}). Try again shortly.")
    if status != 200 or not isinstance(body, dict):
        log.warning("KyberSwap build failed: HTTP %s %s", status, str(body)[:200])
        raise KyberError(f"KyberSwap could not build the transaction (HTTP {status}).")
    d = body.get("data") or {}
    if body.get("code") not in (0, None) or not d.get("data") or not d.get("routerAddress"):
        raise KyberError(f"KyberSwap: {body.get('message') or 'build returned no calldata'}")

    router = str(d["routerAddress"])
    if router.lower() != RHC_KYBER_ROUTER.lower():
        # An address arriving in an API response is not something to send money
        # to on a chain with address-squatting drainers. Pinned or nothing.
        raise KyberError(f"KyberSwap returned router {router}, expected {RHC_KYBER_ROUTER}. Refusing.")

    calldata = str(d["data"])
    if not calldata.startswith("0x") or len(calldata) < 10:
        raise KyberError("KyberSwap returned malformed calldata. Refusing.")
    value = int(d.get("transactionValue") or 0)
    amount_in = int(d.get("amountIn") or rt.amount_in)
    is_buy = rt.token_in.lower() == NATIVE.lower()
    # The built transaction must spend exactly what was quoted: the value field
    # is ETH leaving the wallet, and nothing in a response gets to raise it.
    if amount_in != rt.amount_in or value != (rt.amount_in if is_buy else 0):
        raise KyberError(
            f"KyberSwap's built transaction (amountIn {amount_in}, value {value}) does not match the quote "
            f"(amountIn {rt.amount_in}). Refusing."
        )

    amount_out = int(d.get("amountOut") or rt.amount_out)
    return BuiltSwap(
        router=router,
        data=calldata,
        value=value,
        amount_in=amount_in,
        amount_out=amount_out,
        amount_in_usd=_f(d.get("amountInUsd"), rt.amount_in_usd),
        amount_out_usd=_f(d.get("amountOutUsd"), rt.amount_out_usd),
        gas=int(_f(d.get("gas"), rt.gas)),
        gas_usd=_f(d.get("gasUsd"), rt.gas_usd),
        slippage_bps=int(slippage_bps),
        min_out=min_out(amount_out, slippage_bps),
        route=rt,
    )
