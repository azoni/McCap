"""Robinhood Crypto Trading API client.

The official credentialed brokerage API at trading.robinhood.com. Every request
is signed with Ed25519 over ``api_key + timestamp + path + method + body``, sent
as ``x-api-key`` / ``x-timestamp`` / ``x-signature``.

This module places real orders against a real account, so it is deliberately
narrow: read endpoints, one order-placement call, one cancel. No strategy, no
automation, no background loop. Everything that decides *whether* to trade lives
above this layer, where the caps and the confirmation step are.

The private key is a signing secret. It is never logged, never returned, and
never rendered into a Discord message.
"""

import base64
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .config import (
    RH_API_KEY,
    RH_BASE_URL,
    RH_PRIVATE_KEY_B64,
    RH_TIMEOUT,
)
from .http import get_session
from .logging_setup import log

ACCOUNT_PATH = "/api/v1/crypto/trading/accounts/"
HOLDINGS_PATH = "/api/v1/crypto/trading/holdings/"
ORDERS_PATH = "/api/v1/crypto/trading/orders/"
PAIRS_PATH = "/api/v1/crypto/trading/trading_pairs/"
BID_ASK_PATH = "/api/v1/crypto/marketdata/best_bid_ask/"


class RobinhoodError(RuntimeError):
    """A request was rejected, or the client is not configured."""


@dataclass
class Quote:
    symbol: str
    bid: Optional[float]
    ask: Optional[float]

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


def configured() -> bool:
    return bool(RH_API_KEY and RH_PRIVATE_KEY_B64)


def _signing_key():
    # Imported lazily so the bot still starts if PyNaCl is missing and trading
    # is switched off.
    from nacl.signing import SigningKey

    seed = base64.b64decode(RH_PRIVATE_KEY_B64)
    # Robinhood issues a 32-byte seed; some exports carry the 64-byte expanded
    # form, whose first 32 bytes are the seed.
    return SigningKey(seed[:32])


def sign_message(api_key: str, timestamp: int, path: str, method: str, body: str, key) -> str:
    """Ed25519 signature over the exact concatenation Robinhood expects.

    Kept as a free function so the ordering can be tested without credentials —
    a wrong field order here means every request is rejected.
    """
    message = f"{api_key}{timestamp}{path}{method}{body}"
    return base64.b64encode(key.sign(message.encode("utf-8")).signature).decode("utf-8")


def auth_headers(method: str, path: str, body: str = "", key=None, now: Optional[int] = None) -> Dict[str, str]:
    ts = int(now if now is not None else time.time())
    signature = sign_message(RH_API_KEY, ts, path, method, body, key or _signing_key())
    return {
        "x-api-key": RH_API_KEY,
        "x-signature": signature,
        "x-timestamp": str(ts),
        "Content-Type": "application/json",
    }


async def _request(method: str, path: str, body: Optional[Dict] = None) -> Any:
    if not configured():
        raise RobinhoodError("Robinhood credentials are not configured.")

    payload = json.dumps(body) if body is not None else ""
    headers = auth_headers(method, path, payload, None)
    session = await get_session()
    url = RH_BASE_URL + path

    try:
        async with session.request(
            method, url, headers=headers,
            data=(payload if body is not None else None),
            timeout=__import__("aiohttp").ClientTimeout(total=RH_TIMEOUT),
        ) as r:
            text = await r.text()
            if r.status >= 400:
                # Never echo the body verbatim into Discord; it can contain
                # account identifiers.
                log.warning("Robinhood %s %s -> HTTP %s", method, path, r.status)
                raise RobinhoodError(f"Robinhood rejected the request (HTTP {r.status}).")
            return json.loads(text) if text else {}
    except RobinhoodError:
        raise
    except Exception as e:
        log.debug("Robinhood request failed: %s", type(e).__name__)
        raise RobinhoodError(f"Could not reach Robinhood ({type(e).__name__}).") from e


# ---------------- reads ----------------


async def get_account() -> Dict:
    return await _request("GET", ACCOUNT_PATH)


async def get_holdings() -> Dict:
    return await _request("GET", HOLDINGS_PATH)


async def get_quote(symbol: str) -> Quote:
    """Best bid/ask for a pair like BTC-USD."""
    path = f"{BID_ASK_PATH}?symbol={symbol}"
    data = await _request("GET", path)
    results = (data or {}).get("results") or []
    if not results:
        raise RobinhoodError(f"No market data for {symbol}.")
    row = results[0]
    return Quote(
        symbol=symbol,
        bid=_f(row.get("bid_inclusive_of_sell_spread") or row.get("price")),
        ask=_f(row.get("ask_inclusive_of_buy_spread") or row.get("price")),
    )


async def get_orders() -> Dict:
    return await _request("GET", ORDERS_PATH)


# Fallback list for when credentials aren't configured yet. Robinhood's own
# trading_pairs endpoint is authoritative and is preferred whenever it can be
# reached; this only exists so /rh_trending is useful before setup, and it is
# labelled as approximate wherever it gets used.
KNOWN_PAIRS = (
    "BTC", "ETH", "SOL", "DOGE", "XRP", "ADA", "AVAX", "LINK", "LTC", "BCH",
    "ETC", "UNI", "XLM", "AAVE", "COMP", "SHIB", "PEPE", "DOT", "USDC", "XTZ",
)


async def get_trading_pairs() -> Tuple[list, bool]:
    """Base symbols Robinhood will actually trade.

    Returns ``(symbols, authoritative)``. ``authoritative`` is False when the
    list came from KNOWN_PAIRS because credentials are missing or the call
    failed — the caller says so rather than implying the list is verified.
    """
    if not configured():
        return list(KNOWN_PAIRS), False
    try:
        data = await _request("GET", PAIRS_PATH)
    except RobinhoodError:
        return list(KNOWN_PAIRS), False

    symbols = []
    for row in (data or {}).get("results") or []:
        sym = row.get("symbol") or ""          # e.g. "BTC-USD"
        base = sym.split("-")[0].strip().upper()
        # Only surface pairs that are actually tradeable right now.
        if base and row.get("status", "tradable") == "tradable":
            symbols.append(base)
    return (symbols, True) if symbols else (list(KNOWN_PAIRS), False)


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------- writes ----------------


async def place_market_order(symbol: str, side: str, asset_quantity: str) -> Dict:
    """Place a market order. The caller is responsible for the caps.

    Quantity is a string on purpose — floats round, and rounding an order size
    is not a rounding error, it is a different order.
    """
    if side not in ("buy", "sell"):
        raise RobinhoodError(f"Invalid side: {side!r}")
    body = {
        "client_order_id": str(uuid.uuid4()),
        "side": side,
        "type": "market",
        "symbol": symbol,
        "market_order_config": {"asset_quantity": asset_quantity},
    }
    log.info("Robinhood order: %s %s %s", side, asset_quantity, symbol)
    return await _request("POST", ORDERS_PATH, body)


async def cancel_order(order_id: str) -> Dict:
    return await _request("POST", f"{ORDERS_PATH}{order_id}/cancel/")


# ---------------- sizing ----------------


def quantity_for_usd(usd: float, price: float, decimals: int = 8) -> Tuple[str, float]:
    """Convert a dollar amount into an asset quantity at a given price.

    Returns (quantity_string, actual_usd). Rounds the quantity DOWN so the
    resulting order can never cost more than the caller authorised — rounding up
    would quietly breach the cap it was checked against.
    """
    if price <= 0:
        raise RobinhoodError("Cannot size an order at a non-positive price.")
    raw = usd / price
    factor = 10 ** decimals
    qty = int(raw * factor) / factor          # floor, not round
    if qty <= 0:
        raise RobinhoodError("Amount is too small to buy any of this asset.")
    return f"{qty:.{decimals}f}".rstrip("0").rstrip("."), qty * price
