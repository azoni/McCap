"""Robinhood Chain RPC access.

Every read falls back across the configured endpoints: one dead RPC must not
look like "the wallet is empty" or "the token has no code". Endpoints are ranked
by measured latency (re-measured every few minutes), because "fastest" is a
property of the network right now, not of the config file.

Two kinds of failure are kept apart, because callers must treat them
differently: ``RevertError`` is the chain's answer (the same on every endpoint,
raised immediately), ``RpcUnavailable`` is every endpoint failing to answer.
"Refuse to sign because the swap would revert" and "refuse to sign because we
are blind" are both refusals, but they are different messages to a trader.

Addresses below were verified by direct RPC probing (mccap-web, 2026-08-30;
re-probed 2026-09-07), never copied from a chain list. This chain hosts live
drainers squatting Ethereum mainnet's canonical Uniswap addresses, each a 2,109
byte stub that accepts bare ETH and returns success. They are denylisted here
so no code path can ever send value to them by accident.
"""

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple, TypeVar

import aiohttp
from web3 import AsyncHTTPProvider, AsyncWeb3

from ..config import RHC_CHAIN_ID, RHC_EXPLORER, RHC_RPC_TIMEOUT, RHC_RPC_URLS
from ..logging_setup import log

T = TypeVar("T")

CHAIN_ID = RHC_CHAIN_ID

# KyberSwap's sentinel for the native token (ETH here).
NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
ZERO = "0x0000000000000000000000000000000000000000"
# Verified: symbol() == "WETH", decimals() == 18; the SwapRouter02's WETH9().
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
# Verified: symbol() == "USDG", decimals() == 6.
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"

# Squatted Uniswap mainnet addresses with a payable fallback that forwards ETH
# to the attacker and returns success. Measured 2,109 bytes each, one deployer.
DENYLIST = {
    "0xe592427a0aece92de3edee1f18e0157c05861564",  # mainnet SwapRouter
    "0x61ffe014ba17989e743c5f6cb21bf9697530b21e",  # mainnet QuoterV2
    "0x1f98431c8ad98523631ae4a59f267346ea31f984",  # mainnet V3Factory
}

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "string"}]},
]


class ChainError(Exception):
    """Base for both kinds of chain failure."""


class RevertError(ChainError):
    """The chain executed the call and it reverted."""


class RpcUnavailable(ChainError):
    """Every configured endpoint failed to answer."""


# ---------------- endpoints ----------------

_clients: Dict[str, AsyncWeb3] = {}
_latency: Dict[str, float] = {}
_ranked_at = 0.0
_bg_tasks: Set["asyncio.Task[Any]"] = set()   # keep re-rank tasks referenced until done
RANK_TTL = 300.0


def _client(url: str) -> AsyncWeb3:
    w3 = _clients.get(url)
    if w3 is None:
        w3 = _clients[url] = AsyncWeb3(AsyncHTTPProvider(url, request_kwargs={"timeout": RHC_RPC_TIMEOUT}))
    return w3


async def _measure(url: str) -> None:
    t0 = time.monotonic()
    try:
        await _client(url).eth.block_number
        _latency[url] = time.monotonic() - t0
    except Exception:
        _latency[url] = float("inf")


async def _measure_all() -> None:
    await asyncio.gather(*(_measure(u) for u in RHC_RPC_URLS))


async def rank_endpoints(force: bool = False) -> List[Tuple[str, float]]:
    """Endpoints fastest first, re-measured when stale."""
    global _ranked_at
    if force or time.monotonic() - _ranked_at > RANK_TTL:
        await _measure_all()
        _ranked_at = time.monotonic()
    return sorted(((u, _latency.get(u, float("inf"))) for u in RHC_RPC_URLS), key=lambda x: x[1])


def _ordered() -> List[str]:
    """Endpoints fastest first. Kicks off a background re-rank when stale and
    never blocks a read on it."""
    global _ranked_at
    if time.monotonic() - _ranked_at > RANK_TTL:
        _ranked_at = time.monotonic()
        try:
            task = asyncio.get_running_loop().create_task(_measure_all())
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)
        except RuntimeError:
            pass
    return sorted(RHC_RPC_URLS, key=lambda u: _latency.get(u, float("inf")))


def _is_revert(e: BaseException) -> bool:
    name = type(e).__name__
    if name in ("ContractLogicError", "ContractCustomError", "ContractPanicError"):
        return True
    msg = str(e).lower()
    return "execution reverted" in msg or "revert" in msg


async def with_client(fn: Callable[[AsyncWeb3], Awaitable[T]]) -> T:
    """Run a read against each endpoint in turn; only a unanimous failure fails.

    A revert is not a transport failure: it is the chain's answer, the same on
    every endpoint, so it is raised immediately as RevertError instead of being
    retried against the next RPC.
    """
    last: Optional[BaseException] = None
    for url in _ordered():
        try:
            return await fn(_client(url))
        except asyncio.CancelledError:
            raise
        except ChainError:
            raise
        except Exception as e:  # noqa: BLE001
            if _is_revert(e):
                raise RevertError(str(e) or "execution reverted") from e
            last = e
            log.debug("RPC %s failed: %s: %s", url, type(e).__name__, e)
    raise RpcUnavailable(f"All Robinhood Chain RPCs failed: {type(last).__name__}: {last}")


# ---------------- reads ----------------

def to_checksum(addr: str) -> str:
    return AsyncWeb3.to_checksum_address(addr)


def is_address(s: str) -> bool:
    try:
        return AsyncWeb3.is_address((s or "").strip())
    except Exception:
        return False


def is_native(addr: str) -> bool:
    return (addr or "").lower() == NATIVE.lower()


async def chain_id() -> int:
    return await with_client(lambda w3: w3.eth.chain_id)


async def native_balance(addr: str) -> int:
    cs = to_checksum(addr)
    return await with_client(lambda w3: w3.eth.get_balance(cs))


async def erc20_balance(token: str, owner: str) -> int:
    t, o = to_checksum(token), to_checksum(owner)
    return await with_client(lambda w3: w3.eth.contract(address=t, abi=ERC20_ABI).functions.balanceOf(o).call())


async def erc20_meta(token: str) -> Tuple[str, int]:
    """(symbol, decimals) from the contract itself; an indexer can lag a new listing.

    Only a revert is swallowed (some tokens have no symbol()); a transport error
    propagates so the next endpoint gets its turn.
    """
    t = to_checksum(token)

    async def read(w3: AsyncWeb3):
        c = w3.eth.contract(address=t, abi=ERC20_ABI)
        try:
            sym = await c.functions.symbol().call()
        except Exception as e:  # noqa: BLE001
            if not _is_revert(e):
                raise
            sym = "?"
        try:
            dec = int(await c.functions.decimals().call())
        except Exception as e:  # noqa: BLE001
            if not _is_revert(e):
                raise
            dec = 18
        return (str(sym) or "?")[:16], dec

    return await with_client(read)


async def allowance(token: str, owner: str, spender: str) -> int:
    t, o, s = to_checksum(token), to_checksum(owner), to_checksum(spender)
    return await with_client(lambda w3: w3.eth.contract(address=t, abi=ERC20_ABI).functions.allowance(o, s).call())


def approve_calldata(spender: str, amount: int) -> str:
    """ABI-encoded ``approve(spender, amount)``; no provider needed to encode."""
    w3 = AsyncWeb3()
    c = w3.eth.contract(abi=ERC20_ABI)
    return c.encode_abi("approve", args=[to_checksum(spender), int(amount)])


async def code_size(addr: str) -> int:
    cs = to_checksum(addr)
    code = await with_client(lambda w3: w3.eth.get_code(cs))
    return len(bytes(code))


async def gas_price() -> int:
    return await with_client(lambda w3: w3.eth.gas_price)


async def call(tx: Dict[str, Any], overrides: Optional[Dict[str, Dict[str, Any]]] = None) -> bytes:
    """``eth_call`` with optional state overrides. RevertError on revert."""
    async def run(w3: AsyncWeb3):
        if overrides:
            return await w3.eth.call(tx, "latest", overrides)
        return await w3.eth.call(tx, "latest")

    return bytes(await with_client(run))


async def estimate_gas(tx: Dict[str, Any]) -> int:
    return int(await with_client(lambda w3: w3.eth.estimate_gas(tx, "latest")))


async def nonce(addr: str) -> int:
    cs = to_checksum(addr)
    return int(await with_client(lambda w3: w3.eth.get_transaction_count(cs, "pending")))


# Node replies that mean "this transaction was NOT accepted, nothing is out there".
_DEFINITE_REJECTIONS = (
    "nonce too low", "insufficient funds", "intrinsic gas", "underpriced", "gas limit",
    "exceeds block gas", "max fee per gas less than", "invalid sender", "invalid signature",
    "invalid transaction", "execution reverted", "exceeds", "oversized",
)
# Replies that mean "the node has it already": the broadcast succeeded.
_ALREADY_BROADCAST = ("already known", "known transaction", "already imported", "already exists")


def local_tx_hash(raw: bytes) -> str:
    return AsyncWeb3.keccak(bytes(raw)).to_0x_hex()


async def send_raw(raw: bytes) -> str:
    """Broadcast a signed transaction and return its hash.

    Sent to ONE endpoint, deliberately. A signed transaction retried across
    endpoints after a lost response would either fail on the nonce or be the
    same transaction twice; neither tells us anything. So: a definite rejection
    raises ChainError (nothing is out there). Anything else, including a timed
    out response, returns the hash computed locally and lets the receipt wait
    settle it. Reporting an uncertain broadcast as "failed" is what turns a slow
    node into a double spend.
    """
    raw = bytes(raw)
    h_local = local_tx_hash(raw)
    urls = _ordered()
    # Never broadcast through an endpoint that was unreachable when measured.
    reachable = [u for u in urls if _latency.get(u, float("inf")) != float("inf")]
    url = (reachable or urls)[0]
    try:
        h = await _client(url).eth.send_raw_transaction(raw)
        got = h.to_0x_hex() if hasattr(h, "to_0x_hex") else "0x" + bytes(h).hex()
        if got.lower() != h_local.lower():
            log.warning("Node returned hash %s for a transaction hashing to %s", got, h_local)
        return got
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if any(k in msg for k in _ALREADY_BROADCAST):
            return h_local
        if any(k in msg for k in _DEFINITE_REJECTIONS):
            raise ChainError(str(e)) from e
        if _never_reached_the_node(e):
            raise RpcUnavailable(
                f"The broadcast endpoint {url} did not accept the request ({type(e).__name__}); nothing was sent."
            ) from e
        log.warning("Broadcast to %s got no clear answer (%s: %s); treating %s as possibly sent",
                    url, type(e).__name__, e, h_local)
        return h_local


def _never_reached_the_node(e: BaseException) -> bool:
    """A refused connection, DNS failure or HTTP-level rejection (429/403/5xx)
    means the node never took the transaction. Only a timeout AFTER sending is
    genuinely uncertain."""
    if isinstance(e, (aiohttp.ClientConnectorError, aiohttp.ClientResponseError)):
        return True
    msg = str(e).lower()
    return any(k in msg for k in ("connection refused", "cannot connect", "name resolution", "getaddrinfo",
                                  "429", "403", "502", "503", "504", "too many requests", "forbidden"))


async def receipt(tx_hash: str) -> Optional[Dict[str, Any]]:
    """One receipt lookup. ``None`` means not mined yet (or unknown), not failed."""
    async def one(w3: AsyncWeb3):
        try:
            return await w3.eth.get_transaction_receipt(tx_hash)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "TransactionNotFound" or "not found" in str(e).lower():
                return None
            raise

    rec = await with_client(one)
    return dict(rec) if rec is not None else None


async def wait_receipt(tx_hash: str, timeout: float) -> Optional[Dict[str, Any]]:
    """Poll for a receipt. ``None`` means unknown, NOT failed: this sequencer can
    still mine the transaction later, and retrying would spend twice. Transport
    blips during the wait are logged and polling continues until the deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rec = await receipt(tx_hash)
            if rec is not None:
                return rec
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("Receipt poll for %s failed: %s", tx_hash, e)
        await asyncio.sleep(1.0)
    return None


def explorer_tx(tx_hash: str) -> str:
    return f"{RHC_EXPLORER}/tx/{tx_hash}"


def explorer_address(addr: str) -> str:
    return f"{RHC_EXPLORER}/address/{addr}"


def fmt_units(amount: int, decimals: int, places: int = 6) -> str:
    """Human number from integer token units."""
    if decimals <= 0:
        return f"{amount:,}"
    whole, frac = divmod(int(amount), 10 ** decimals)
    frac_str = f"{frac:0{decimals}d}"[:places].rstrip("0")
    return f"{whole:,}" + (f".{frac_str}" if frac_str else "")


def to_units(text: str, decimals: int) -> int:
    """Parse a decimal string into integer token units, rounding down.

    Commas are rejected rather than stripped: "0,05" is 0.05 ETH to a European
    keyboard and 5 ETH to a comma-stripping parser, and that is a real loss.
    """
    s = (text or "").strip()
    if not s or s.startswith("-") or "," in s:
        raise ValueError("amount must be a positive number, written with a dot (0.05)")
    if "." in s:
        whole, frac = s.split(".", 1)
    else:
        whole, frac = s, ""
    if not (whole or frac) or not (whole + frac).isdigit():
        raise ValueError("amount must be a positive number, written with a dot (0.05)")
    frac = (frac + "0" * decimals)[:decimals]
    return int(whole or "0") * 10 ** decimals + int(frac or "0")
