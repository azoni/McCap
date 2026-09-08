"""What must be true before a Robinhood Chain transaction is signed.

The threat, measured live (mccap-web 2026-08-30, re-probed 2026-09-07): this
chain has drainers squatting Uniswap's canonical mainnet addresses. Each is a
2,109 byte stub whose payable fallback forwards your ETH to the attacker and
returns success, so a swap sent there gets a green receipt and the money is
simply gone. The trap is that it succeeds.

So nothing is signed until, independently:

  1. the target is not on the denylist of known squatters;
  2. the target IS the pinned canonical KyberSwap router (never an address
     that merely arrived in an API response);
  3. the target has real code (a floor well above the 2 KB stub); and
  4. the exact calldata we are about to send is SIMULATED with ``eth_call``
     and returns an ABI-encoded swap result whose amount is at least the
     slippage floor. A drainer stub returns empty bytes; a real router returns
     ``(returnAmount, gasUsed)``. Measured 2026-09-07: the real router came back
     within 0.03% of the quote, the stub came back with ``0x``.

Check 4 is the general one. It does not depend on knowing the attacker's
addresses, and it doubles as the slippage pre-check that stops a stale quote
from being sent at all. When the chain cannot be reached to run it, the answer
is "refuse", never "assume fine".
"""

from typing import Dict, Optional

from ..config import RHC_KYBER_ROUTER
from ..logging_setup import log
from . import chain
from .kyber import BuiltSwap

MIN_ROUTER_CODE_BYTES = 10_000
_router_verified = False


class GuardError(Exception):
    """Refusing to sign, with the reason."""


def assert_not_denied(target: str) -> None:
    if (target or "").lower() in chain.DENYLIST:
        raise GuardError(
            f"REFUSING TO SIGN: {target} is a known drainer on Robinhood Chain "
            "(a squatted Uniswap mainnet address with a payable fallback)."
        )


def assert_pinned_router(target: str) -> None:
    if (target or "").lower() != RHC_KYBER_ROUTER.lower():
        raise GuardError(f"REFUSING TO SIGN: target {target} is not the pinned KyberSwap router {RHC_KYBER_ROUTER}.")


async def verify_router() -> None:
    """Checks 1-3. A pass is cached (the chain is immutable); a failure is not,
    so a transient RPC problem does not permanently disable trading."""
    global _router_verified
    assert_not_denied(RHC_KYBER_ROUTER)
    if _router_verified:
        return
    size = await chain.code_size(RHC_KYBER_ROUTER)
    if size < MIN_ROUTER_CODE_BYTES:
        raise GuardError(
            f"REFUSING TO SIGN: the router at {RHC_KYBER_ROUTER} has {size} bytes of code on this chain; "
            f"a real aggregator router has far more (floor {MIN_ROUTER_CODE_BYTES})."
        )
    _router_verified = True
    log.info("Robinhood Chain router %s verified (%d bytes of code)", RHC_KYBER_ROUTER, size)


def reset_router_verification() -> None:
    global _router_verified
    _router_verified = False


def decode_swap_result(data: bytes) -> int:
    """First word of ``(uint256 returnAmount, uint256 gasUsed)``.

    Anything shorter than one word is not a swap result. That is exactly what a
    drainer's bare fallback produces, so it is a refusal, not a parse error.
    """
    if data is None or len(data) < 32:
        raise GuardError(
            "REFUSING TO SIGN: the router returned no swap result when simulated. "
            "That is drainer behaviour, not an aggregator."
        )
    return int.from_bytes(data[:32], "big")


async def simulate(built: BuiltSwap, sender: str, fund_sender: bool = False) -> int:
    """Check 4: run the exact transaction read-only and return the simulated output.

    ``fund_sender`` overrides the sender's ETH balance so a quote can be checked
    before the wallet is funded (never used on the signing path, where the real
    balance must carry the trade).
    """
    assert_not_denied(built.router)
    assert_pinned_router(built.router)
    tx: Dict[str, object] = {
        "from": chain.to_checksum(sender),
        "to": chain.to_checksum(built.router),
        "value": int(built.value),
        "data": built.data,
    }
    overrides: Optional[Dict[str, Dict[str, object]]] = None
    if fund_sender:
        overrides = {chain.to_checksum(sender): {"balance": int(built.value) + 10 ** 17}}
    try:
        out = await chain.call(tx, overrides)
    except chain.RevertError as e:
        raise GuardError(f"The swap would revert: {_short(str(e))}") from e
    except chain.RpcUnavailable as e:
        raise GuardError("Could not reach Robinhood Chain to simulate the swap. Refusing to sign blind; try again.") from e
    got = decode_swap_result(out)
    if got < built.min_out:
        raise GuardError(
            f"Simulated output {got} is below the slippage floor {built.min_out} "
            f"({built.slippage_bps / 100:.2f}%). The price moved; re-quote."
        )
    return got


def _short(msg: str, n: int = 160) -> str:
    msg = msg.replace("\n", " ")
    return msg if len(msg) <= n else msg[: n - 1] + "…"
