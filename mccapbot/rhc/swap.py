"""Executing a Robinhood Chain swap for a user's custodial wallet.

Order of operations matters; each step can strand funds if skipped:

  1. the RPC must report chain 4663           signing for another chain is fatal
  2. the router must pass the guard           this chain hosts drainers
  3. the wallet must hold ETH for gas          the #1 silent way a swap fails
     (and, for a buy, more than the amount being spent)
  4. selling needs an allowance               approve exactly this amount, wait
  5. simulate the exact calldata              drainer tell + slippage pre-check
  6. estimate gas fresh, sign, broadcast      priority fee 0: FCFS sequencer
  7. journal the broadcast, then wait         a timeout is PENDING, not failed

One transaction at a time per wallet: two concurrent trades would race the
nonce. A wallet with an unresolved pending transaction refuses new ones until
that transaction is found or a grace period passes, because "it timed out, try
again" is how a slow node becomes a double spend. That memory is rebuilt from
the journal on startup, so a redeploy mid-trade does not forget it.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from eth_account import Account

from ..config import RHC_KYBER_ROUTER, RHC_PENDING_BLOCK_SECONDS, RHC_TX_TIMEOUT
from ..logging_setup import log
from . import chain, guard, ledger, wallets
from .kyber import BuiltSwap

GAS_BUFFER_PCT = 25
FEE_MULTIPLIER = 2  # maxFeePerGas headroom over the current base fee


@dataclass
class SwapResult:
    ok: bool
    tx: Optional[str] = None
    error: str = ""
    pending: bool = False          # broadcast, outcome unknown: do NOT retry blindly
    amount_out: Optional[int] = None
    gas_cost_wei: int = 0

    @property
    def explorer(self) -> str:
        return chain.explorer_tx(self.tx) if self.tx else ""


_locks: Dict[int, asyncio.Lock] = {}
_pending: Dict[int, Tuple[str, float]] = {}   # user_id -> (tx hash, when it was broadcast)


def _lock(user_id: int) -> asyncio.Lock:
    lk = _locks.get(user_id)
    if lk is None:
        lk = _locks[user_id] = asyncio.Lock()
    return lk


def restore_pending() -> int:
    """Rebuild the pending map from the journal after a restart."""
    _pending.clear()
    for user_id, tx_hash, ts in ledger.unresolved():
        _pending[user_id] = (tx_hash, ts)
    if _pending:
        log.warning("Restored %d unresolved Robinhood Chain transaction(s) from the journal", len(_pending))
    return len(_pending)


def describe_error(e: BaseException) -> str:
    """The failures a trader needs to tell apart."""
    msg = str(e) or type(e).__name__
    low = msg.lower()
    if "insufficient funds" in low:
        return "Insufficient ETH for gas on Robinhood Chain."
    if "slippage" in low or "return amount is not enough" in low or "too little" in low:
        return "Price moved past your slippage limit. Nothing was swapped. Re-quote and try again."
    if "nonce" in low:
        return "Nonce conflict: another transaction from this wallet is in flight. Wait and retry."
    if "revert" in low:
        return "The transaction reverted."
    if isinstance(e, chain.RpcUnavailable):
        return "Could not reach Robinhood Chain. Try again shortly."
    return msg[:200]


async def _pending_block(user_id: int) -> Optional[str]:
    """Reason to refuse because an earlier transaction is still unresolved, or None.

    When the receipt finally shows up the journal gets the resolution. When the
    grace period passes with no receipt, the transaction is treated as dropped
    by the sequencer: journaled as such and, for a buy, its budget reservation
    is returned.
    """
    entry = _pending.get(user_id)
    if not entry:
        return None
    tx_hash, since = entry
    try:
        rec = await chain.receipt(tx_hash)
    except chain.ChainError:
        rec = None
    if rec is not None:
        _pending.pop(user_id, None)
        status = "confirmed" if int(rec.get("status", 0)) == 1 else "reverted"
        ledger.journal({"ts": time.time(), "user_id": user_id, "tx": tx_hash, "status": status,
                        "kind": "resolution", "gas_cost_wei": str(int(rec.get("gasUsed", 0)) * int(rec.get("effectiveGasPrice", 0) or 0))})
        return None
    if time.time() - since > RHC_PENDING_BLOCK_SECONDS:
        log.warning("Giving up on pending tx %s for user %s; treating it as dropped", tx_hash, user_id)
        _pending.pop(user_id, None)
        original = ledger.entry_for_tx(user_id, tx_hash)
        ledger.journal({"ts": time.time(), "user_id": user_id, "tx": tx_hash, "status": "dropped", "kind": "resolution"})
        if original and original.get("kind") == "buy" and original.get("usd_in"):
            try:
                ledger.refund(user_id, float(original["usd_in"]))
            except Exception:
                log.exception("Could not refund the reservation for dropped tx %s", tx_hash)
        return None
    return (f"Your earlier transaction {tx_hash} is still unconfirmed. Check it on the explorer "
            f"({chain.explorer_tx(tx_hash)}) before trading again.")


async def _sign_and_send(user_id: int, tx: Dict[str, Any]) -> str:
    """Fill fees and nonce, sign with the user's key, broadcast. Returns the hash.

    The key is decrypted (off the event loop; scrypt is CPU-bound) and dropped as
    soon as the signature exists.
    """
    sender = tx["from"]
    gas_price = await chain.gas_price()
    tx = dict(tx)
    tx.setdefault("chainId", chain.CHAIN_ID)
    if tx["chainId"] != chain.CHAIN_ID:
        raise guard.GuardError("Refusing to sign for a chain other than 4663.")
    tx["nonce"] = await chain.nonce(sender)
    tx["maxFeePerGas"] = int(gas_price) * FEE_MULTIPLIER
    tx["maxPriorityFeePerGas"] = 0
    tx["type"] = 2
    key = await asyncio.to_thread(wallets.private_key, user_id)
    try:
        signed = Account.from_key(key).sign_transaction(tx)
    finally:
        del key
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    return await chain.send_raw(bytes(raw))


async def _wait(tx_hash: str) -> Optional[Dict[str, Any]]:
    try:
        return await chain.wait_receipt(tx_hash, RHC_TX_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("Receipt wait failed for %s: %s", tx_hash, e)
        return None


def _mark_pending(user_id: int, tx_hash: str) -> None:
    _pending[user_id] = (tx_hash, time.time())


async def _approve(user_id: int, token: str, owner: str, amount: int) -> SwapResult:
    tx = {
        "from": chain.to_checksum(owner),
        "to": chain.to_checksum(token),
        "value": 0,
        "data": chain.approve_calldata(RHC_KYBER_ROUTER, amount),
    }
    try:
        gas = await chain.estimate_gas(tx)
        tx["gas"] = gas + gas * GAS_BUFFER_PCT // 100
        h = await _sign_and_send(user_id, tx)
    except Exception as e:  # noqa: BLE001
        return SwapResult(ok=False, error=f"Approval failed: {describe_error(e)}")
    ledger.journal({"ts": time.time(), "user_id": user_id, "kind": "approve", "token": token.lower(),
                    "amount": str(amount), "tx": h, "status": "submitted"})
    rec = await _wait(h)
    if rec is None:
        _mark_pending(user_id, h)
        return SwapResult(ok=False, tx=h, pending=True,
                          error="Approval submitted but unconfirmed. Check the explorer before retrying.")
    status = "confirmed" if int(rec.get("status", 0)) == 1 else "reverted"
    ledger.journal({"ts": time.time(), "user_id": user_id, "kind": "resolution", "tx": h, "status": status})
    if status != "confirmed":
        return SwapResult(ok=False, tx=h, error="Token approval reverted. Nothing was swapped.")
    return SwapResult(ok=True, tx=h)


async def _ensure_allowance(user_id: int, token: str, owner: str, amount: int) -> SwapResult:
    current = await chain.allowance(token, owner, RHC_KYBER_ROUTER)
    if current >= amount:
        return SwapResult(ok=True)
    # Exactly this trade's amount, never unlimited: an infinite approval to a
    # router is a standing risk long after the trade is over. Some tokens
    # (USDT-style) refuse to change a non-zero allowance directly, so reset to
    # zero first when one exists.
    if current > 0:
        reset = await _approve(user_id, token, owner, 0)
        if not reset.ok:
            return reset
    return await _approve(user_id, token, owner, amount)


async def execute(user_id: int, built: BuiltSwap, token: str, symbol: str,
                  extra: Optional[Dict[str, Any]] = None) -> SwapResult:
    """Run the full path for a built swap. ``token`` is the non-ETH side.

    ``extra`` is journaled with the trade: market cap and price at the time,
    token decimals, entry figures for a sell. It is what /rhc holdings and
    /rhc pnl are computed from later.
    """
    lk = _lock(user_id)
    if lk.locked():
        return SwapResult(ok=False, error="You already have a transaction in flight. Wait for it to finish.")
    async with lk:
        blocked = await _pending_block(user_id)
        if blocked:
            return SwapResult(ok=False, error=blocked)
        return await _execute_locked(user_id, built, token, symbol, extra or {})


async def _execute_locked(user_id: int, built: BuiltSwap, token: str, symbol: str,
                          extra: Dict[str, Any]) -> SwapResult:
    w = wallets.get(user_id)
    if w is None:
        return SwapResult(ok=False, error="You have no wallet yet. Use /rhc wallet create.")
    owner = w.address
    is_buy = built.is_buy

    # 1. Never sign against an unexpected chain.
    try:
        cid = await chain.chain_id()
    except chain.ChainError:
        return SwapResult(ok=False, error="Could not reach Robinhood Chain to verify the chain id.")
    if cid != chain.CHAIN_ID:
        return SwapResult(ok=False, error=f"RPC reported chain {cid}, expected {chain.CHAIN_ID}. Refusing to sign.")

    # 2. The router must be the real one.
    try:
        await guard.verify_router()
    except guard.GuardError as e:
        return SwapResult(ok=False, error=str(e))
    except chain.ChainError as e:
        return SwapResult(ok=False, error=f"Could not verify the router: {describe_error(e)}")

    # 3. Gas, and for a buy the ETH being spent, must be there.
    try:
        bal = await chain.native_balance(owner)
    except chain.ChainError:
        return SwapResult(ok=False, error="Could not read your ETH balance on Robinhood Chain.")
    if bal == 0:
        return SwapResult(ok=False, error="Your wallet has no ETH for gas. Fund it first (/rhc wallet show).")
    # The aggregator's gas estimate, buffered, at the fee we will actually sign.
    try:
        price = await chain.gas_price()
    except chain.ChainError:
        return SwapResult(ok=False, error="Could not read the gas price on Robinhood Chain.")
    need = int(built.value) + (int(built.gas) * (100 + GAS_BUFFER_PCT) // 100) * int(price) * FEE_MULTIPLIER
    if bal < need:
        return SwapResult(ok=False, error=(
            f"Not enough ETH: you have {chain.fmt_units(bal, 18)} ETH and this needs about "
            f"{chain.fmt_units(need, 18)} ETH including gas."
        ))

    # 4. Selling needs an allowance for the router.
    if not is_buy:
        try:
            have = await chain.erc20_balance(token, owner)
        except chain.ChainError:
            return SwapResult(ok=False, error="Could not read your token balance.")
        if have < built.amount_in:
            return SwapResult(ok=False, error="You hold less of that token than the amount to sell. Re-quote.")
        try:
            approved = await _ensure_allowance(user_id, token, owner, built.amount_in)
        except chain.ChainError as e:
            return SwapResult(ok=False, error=f"Could not check the token allowance: {describe_error(e)}")
        if not approved.ok:
            return approved

    # 5. The exact calldata must simulate to at least the slippage floor.
    try:
        await guard.simulate(built, owner)
    except guard.GuardError as e:
        return SwapResult(ok=False, error=str(e))

    # Balances before, so the receipt can be turned into what actually arrived.
    try:
        before = await (chain.erc20_balance(token, owner) if is_buy else chain.native_balance(owner))
    except chain.ChainError:
        before = None

    tx = {
        "from": chain.to_checksum(owner),
        "to": chain.to_checksum(built.router),
        "value": int(built.value),
        "data": built.data,
    }
    # 6. Estimate immediately before signing, never from a cache: this Orbit
    # chain's fee has an L1 data component that drifts with Ethereum.
    tx_hash: Optional[str] = None
    try:
        gas = await chain.estimate_gas(tx)
        tx["gas"] = gas + gas * GAS_BUFFER_PCT // 100
        tx_hash = await _sign_and_send(user_id, tx)
    except Exception as e:  # noqa: BLE001
        log.warning("Swap for user %s failed before broadcast: %s", user_id, e)
        return SwapResult(ok=False, error=describe_error(e))

    # 7. From here the transaction exists on the network whether or not we see
    # its receipt. Journal it NOW, before waiting, so a restart cannot forget
    # it; never report a broadcast transaction as a plain failure.
    log.info("Swap broadcast for user %s: %s", user_id, tx_hash)
    _journal(user_id, built, token, symbol, tx_hash, "submitted", None, 0, extra)
    rec = await _wait(tx_hash)
    if rec is None:
        _mark_pending(user_id, tx_hash)
        return SwapResult(ok=False, tx=tx_hash, pending=True,
                          error="Submitted but unconfirmed. It may still mine; check the explorer before retrying.")
    gas_used = int(rec.get("gasUsed", 0))
    eff = int(rec.get("effectiveGasPrice", 0) or 0)
    gas_cost = gas_used * eff
    if int(rec.get("status", 0)) != 1:
        _journal(user_id, built, token, symbol, tx_hash, "reverted", None, gas_cost, extra)
        return SwapResult(ok=False, tx=tx_hash, error="Swap reverted on-chain. Nothing was swapped (gas was spent).",
                          gas_cost_wei=gas_cost)

    # What arrived, by balance delta. A deposit landing in the same seconds
    # would inflate this, so it is labelled as an estimate in the journal.
    amount_out: Optional[int] = None
    try:
        after = await (chain.erc20_balance(token, owner) if is_buy else chain.native_balance(owner))
        if before is not None:
            amount_out = after - before if is_buy else after - before + gas_cost
            if amount_out < 0:
                amount_out = None
    except chain.ChainError:
        pass
    _journal(user_id, built, token, symbol, tx_hash, "confirmed", amount_out, gas_cost, extra)
    return SwapResult(ok=True, tx=tx_hash, amount_out=amount_out, gas_cost_wei=gas_cost)


def _journal(user_id: int, built: BuiltSwap, token: str, symbol: str, tx_hash: str,
             status: str, amount_out: Optional[int], gas_cost: int,
             extra: Optional[Dict[str, Any]] = None) -> None:
    ledger.journal({
        **(extra or {}),
        "ts": time.time(),
        "user_id": user_id,
        "kind": "buy" if built.is_buy else "sell",
        "token": token.lower(),
        "symbol": symbol,
        "amount_in": str(built.amount_in),
        "quoted_out": str(built.amount_out),
        "actual_out_estimate": (str(amount_out) if amount_out is not None else None),
        "usd_in": built.amount_in_usd,
        "usd_out": built.amount_out_usd,
        "slippage_bps": built.slippage_bps,
        "gas_cost_wei": str(gas_cost),
        "tx": tx_hash,
        "status": status,
        "route": built.route.hops,
    })


async def send_native(user_id: int, to: str, amount_wei: int) -> SwapResult:
    """Withdraw ETH from a user's wallet to an address they gave."""
    lk = _lock(user_id)
    if lk.locked():
        return SwapResult(ok=False, error="You already have a transaction in flight. Wait for it to finish.")
    async with lk:
        blocked = await _pending_block(user_id)
        if blocked:
            return SwapResult(ok=False, error=blocked)
        w = wallets.get(user_id)
        if w is None:
            return SwapResult(ok=False, error="You have no wallet yet.")
        if not chain.is_address(to):
            return SwapResult(ok=False, error="That is not a valid address.")
        if to.lower() in chain.DENYLIST:
            return SwapResult(ok=False, error="REFUSING: that address is a known drainer.")
        if to.lower() == chain.ZERO:
            return SwapResult(ok=False, error="REFUSING: that is the zero address; ETH sent there is burned.")
        if amount_wei <= 0:
            return SwapResult(ok=False, error="Amount must be greater than zero.")
        try:
            if await chain.chain_id() != chain.CHAIN_ID:
                return SwapResult(ok=False, error="RPC reported the wrong chain. Refusing to sign.")
            bal = await chain.native_balance(w.address)
        except chain.ChainError as e:
            return SwapResult(ok=False, error=f"Could not reach Robinhood Chain: {describe_error(e)}")
        tx = {"from": chain.to_checksum(w.address), "to": chain.to_checksum(to), "value": int(amount_wei), "data": "0x"}
        try:
            gas = await chain.estimate_gas(tx)
            price = await chain.gas_price()
            fee = gas * price * FEE_MULTIPLIER
            if bal < amount_wei + fee:
                return SwapResult(ok=False, error=f"Not enough ETH: balance {chain.fmt_units(bal, 18)} ETH, "
                                                  f"need {chain.fmt_units(amount_wei + fee, 18)} with gas.")
            tx["gas"] = gas + gas * GAS_BUFFER_PCT // 100
            h = await _sign_and_send(user_id, tx)
        except Exception as e:  # noqa: BLE001
            return SwapResult(ok=False, error=describe_error(e))
        ledger.journal({"ts": time.time(), "user_id": user_id, "kind": "withdraw", "to": to.lower(),
                        "amount": str(amount_wei), "tx": h, "status": "submitted"})
        rec = await _wait(h)
        if rec is None:
            _mark_pending(user_id, h)
            return SwapResult(ok=False, tx=h, pending=True, error="Submitted but unconfirmed; check the explorer.")
        status = "confirmed" if int(rec.get("status", 0)) == 1 else "reverted"
        ledger.journal({"ts": time.time(), "user_id": user_id, "kind": "resolution", "tx": h, "status": status})
        if status != "confirmed":
            return SwapResult(ok=False, tx=h, error="The transfer reverted.")
        return SwapResult(ok=True, tx=h, amount_out=amount_wei,
                          gas_cost_wei=int(rec.get("gasUsed", 0)) * int(rec.get("effectiveGasPrice", 0) or 0))
