"""The execution path, against a fake chain. Signing is real (offline); nothing
is broadcast: ``send_raw`` is replaced and records what it was given."""

import asyncio
import os
import time

import pytest
import pytest_asyncio

from mccapbot.config import RHC_KYBER_ROUTER
from mccapbot.rhc import chain, guard, kyber, ledger, swap, wallets

PONS = "0x39dbed3a2bd333467115de45665cc57f813c4571"
USER = 7


class FakeChain:
    """Mutable chain state the swap module reads and writes through."""

    def __init__(self):
        self.chain_id = 4663
        self.native = {}
        self.tokens = {}          # (token, owner) -> balance
        self.allowances = {}      # (token, owner, spender) -> amount
        self.code = {RHC_KYBER_ROUTER.lower(): 13724}
        self.call_result = (36 * 10**18).to_bytes(32, "big") + (5).to_bytes(32, "big")
        self.call_error = None
        self.sent = []            # raw signed transactions
        self.estimated = []       # tx dicts passed to estimate_gas
        self.receipt = {"status": 1, "gasUsed": 300_000, "effectiveGasPrice": 300_000_000}
        self.receipts = {}        # hash -> receipt for the one-shot lookup
        self.on_send = None       # hook: mutate balances when a tx "lands"
        self.nonce = 3

    def install(self, monkeypatch):
        fc = self

        async def chain_id():
            return fc.chain_id

        async def native_balance(addr):
            return fc.native.get(addr.lower(), 0)

        async def erc20_balance(token, owner):
            return fc.tokens.get((token.lower(), owner.lower()), 0)

        async def allowance(token, owner, spender):
            return fc.allowances.get((token.lower(), owner.lower(), spender.lower()), 0)

        async def code_size(addr):
            return fc.code.get(addr.lower(), 0)

        async def call(tx, overrides=None):
            if fc.call_error:
                raise fc.call_error
            return fc.call_result

        async def estimate_gas(tx):
            fc.estimated.append(tx)
            return 400_000

        async def gas_price():
            return 300_000_000

        async def nonce(addr):
            fc.nonce += 1
            return fc.nonce

        async def send_raw(raw):
            fc.sent.append(bytes(raw))
            if fc.on_send:
                fc.on_send(len(fc.sent))
            return "0x" + ("ab" * 31) + f"{len(fc.sent):02x}"

        async def wait_receipt(tx_hash, timeout):
            return fc.receipt

        async def receipt(tx_hash):
            return fc.receipts.get(tx_hash)

        for name, fn in locals().items():
            if name not in ("fc", "self", "monkeypatch") and callable(fn):
                monkeypatch.setattr(chain, name, fn)


@pytest.fixture
def fc(monkeypatch):
    f = FakeChain()
    f.install(monkeypatch)
    guard.reset_router_verification()
    swap._pending.clear()
    yield f
    guard.reset_router_verification()
    swap._pending.clear()


@pytest_asyncio.fixture
async def wallet():
    wallets.wallets.clear()
    wallets._loaded = True
    for p in (wallets.RHC_WALLETS_FILE, ledger.RHC_JOURNAL_FILE, ledger.RHC_LEDGER_FILE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    w = await wallets.create(USER, "tester")
    yield w
    wallets.wallets.clear()
    for p in (wallets.RHC_WALLETS_FILE, ledger.RHC_JOURNAL_FILE, ledger.RHC_LEDGER_FILE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def built(is_buy=True, amount_out=36 * 10**18, slippage_bps=200, amount_in=10**16):
    tin, tout = (chain.NATIVE, PONS) if is_buy else (PONS, chain.NATIVE)
    rt = kyber.Route(token_in=tin, token_out=tout, amount_in=amount_in, amount_out=amount_out, amount_in_usd=24.8,
                     amount_out_usd=24.7, gas=1, gas_usd=0.5, router=RHC_KYBER_ROUTER, summary={}, hops=["uniswap-v4"])
    return kyber.BuiltSwap(router=RHC_KYBER_ROUTER, data="0xe21fd0e9" + "00" * 8, value=amount_in if is_buy else 0,
                           amount_in=amount_in, amount_out=amount_out, amount_in_usd=24.8, amount_out_usd=24.7,
                           gas=1, gas_usd=0.5, slippage_bps=slippage_bps,
                           min_out=kyber.min_out(amount_out, slippage_bps), route=rt)


# ---------------- refusals, nothing signed ----------------


@pytest.mark.asyncio
async def test_wrong_chain_refuses(fc, wallet):
    fc.chain_id = 42161
    fc.native[wallet.address.lower()] = 10**18
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert not res.ok and "expected 4663" in res.error and fc.sent == []


@pytest.mark.asyncio
async def test_router_stub_refuses(fc, wallet):
    fc.code[RHC_KYBER_ROUTER.lower()] = 2109
    fc.native[wallet.address.lower()] = 10**18
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert not res.ok and "bytes of code" in res.error and fc.sent == []


@pytest.mark.asyncio
async def test_no_gas_and_not_enough_eth_refuse(fc, wallet):
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert not res.ok and "no ETH" in res.error
    fc.native[wallet.address.lower()] = 10**16  # exactly the trade size, nothing for gas
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert not res.ok and "gas" in res.error
    assert fc.sent == []


@pytest.mark.asyncio
async def test_simulation_failures_refuse_before_signing(fc, wallet):
    fc.native[wallet.address.lower()] = 10**18
    fc.call_result = b""  # drainer tell
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert not res.ok and "drainer" in res.error and fc.sent == []

    fc.call_result = (30 * 10**18).to_bytes(32, "big") + (5).to_bytes(32, "big")  # below floor
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert not res.ok and "slippage floor" in res.error and fc.sent == []

    fc.call_error = chain.RevertError("execution reverted")
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert not res.ok and "revert" in res.error and fc.sent == []

    fc.call_error = chain.RpcUnavailable("all down")
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert not res.ok and "blind" in res.error and fc.sent == []


@pytest.mark.asyncio
async def test_no_wallet_refuses(fc):
    wallets.wallets.clear()
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert not res.ok and "no wallet" in res.error


@pytest.mark.asyncio
async def test_one_transaction_at_a_time(fc, wallet):
    fc.native[wallet.address.lower()] = 10**18
    lk = swap._lock(USER)
    await lk.acquire()
    try:
        res = await swap.execute(USER, built(), PONS, "PONS")
    finally:
        lk.release()
    assert not res.ok and "in flight" in res.error and fc.sent == []


@pytest.mark.asyncio
async def test_unresolved_pending_transaction_blocks_new_ones_until_found(fc, wallet):
    fc.native[wallet.address.lower()] = 10**18
    swap._pending[USER] = ("0xpending", time.time())
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert not res.ok and "0xpending" in res.error and "explorer" in res.error and fc.sent == []
    # Once the chain shows the receipt, trading resumes.
    fc.receipts["0xpending"] = {"status": 1}
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert res.ok and USER not in swap._pending
    # A very old pending marker is given up on rather than blocking forever.
    swap._pending[USER] = ("0xancient", time.time() - 10**6)
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert res.ok


# ---------------- the happy paths ----------------


@pytest.mark.asyncio
async def test_buy_signs_once_and_reports_what_arrived(fc, wallet):
    owner = wallet.address.lower()
    fc.native[owner] = 10**18

    def land(n):
        fc.tokens[(PONS, owner)] = 36 * 10**18  # tokens arrive when the swap lands
    fc.on_send = land

    res = await swap.execute(USER, built(), PONS, "PONS")
    assert res.ok and res.tx and not res.pending
    assert len(fc.sent) == 1, "a buy needs no approval"
    assert res.amount_out == 36 * 10**18
    assert res.gas_cost_wei == 300_000 * 300_000_000
    assert fc.estimated[-1]["to"] == RHC_KYBER_ROUTER and fc.estimated[-1]["value"] == 10**16
    entries = ledger.entries_for(USER)
    assert [e["status"] for e in entries] == ["submitted", "confirmed"] and entries[-1]["kind"] == "buy"
    assert entries[-1]["actual_out_estimate"] == str(36 * 10**18)


@pytest.mark.asyncio
async def test_sell_approves_exactly_the_amount_then_swaps(fc, wallet):
    owner = wallet.address.lower()
    fc.native[owner] = 10**17
    fc.tokens[(PONS, owner)] = 50 * 10**18
    sell = built(is_buy=False, amount_in=36 * 10**18, amount_out=10**16)
    fc.call_result = (10**16).to_bytes(32, "big") + (5).to_bytes(32, "big")

    def land(n):
        if n == 1:  # approval landed
            fc.allowances[(PONS, owner, RHC_KYBER_ROUTER.lower())] = 36 * 10**18
        else:       # swap landed: ETH arrives
            fc.native[owner] = 10**17 + 10**16 - 300_000 * 300_000_000
    fc.on_send = land

    res = await swap.execute(USER, sell, PONS, "PONS")
    assert res.ok, res.error
    assert len(fc.sent) == 2, "approve, then swap"
    approve_tx = fc.estimated[0]
    assert approve_tx["to"] == chain.to_checksum(PONS) and approve_tx["value"] == 0
    assert approve_tx["data"].startswith("0x095ea7b3")
    assert approve_tx["data"].lower().endswith(hex(36 * 10**18)[2:].rjust(64, "0")), "exact amount, never unlimited"
    assert res.amount_out == 10**16, "gas is added back so the delta is the swap's real output"


@pytest.mark.asyncio
async def test_sell_resets_a_nonzero_allowance_first(fc, wallet):
    """USDT-style tokens refuse approve(x) while the allowance is non-zero."""
    owner = wallet.address.lower()
    fc.native[owner] = 10**17
    fc.tokens[(PONS, owner)] = 50 * 10**18
    fc.allowances[(PONS, owner, RHC_KYBER_ROUTER.lower())] = 5 * 10**18   # too small, non-zero
    fc.call_result = (10**16).to_bytes(32, "big") + (5).to_bytes(32, "big")

    def land(n):
        if n == 1:
            fc.allowances[(PONS, owner, RHC_KYBER_ROUTER.lower())] = 0
        elif n == 2:
            fc.allowances[(PONS, owner, RHC_KYBER_ROUTER.lower())] = 36 * 10**18
    fc.on_send = land
    res = await swap.execute(USER, built(is_buy=False, amount_in=36 * 10**18, amount_out=10**16), PONS, "PONS")
    assert res.ok, res.error
    assert len(fc.sent) == 3, "approve(0), approve(amount), swap"
    assert int(fc.estimated[0]["data"][74:138], 16) == 0
    assert int(fc.estimated[1]["data"][74:138], 16) == 36 * 10**18


@pytest.mark.asyncio
async def test_sell_skips_approval_when_allowance_exists(fc, wallet):
    owner = wallet.address.lower()
    fc.native[owner] = 10**17
    fc.tokens[(PONS, owner)] = 50 * 10**18
    fc.allowances[(PONS, owner, RHC_KYBER_ROUTER.lower())] = 10**24
    fc.call_result = (10**16).to_bytes(32, "big") + (5).to_bytes(32, "big")
    res = await swap.execute(USER, built(is_buy=False, amount_in=36 * 10**18, amount_out=10**16), PONS, "PONS")
    assert res.ok and len(fc.sent) == 1


@pytest.mark.asyncio
async def test_sell_more_than_held_refuses(fc, wallet):
    owner = wallet.address.lower()
    fc.native[owner] = 10**17
    fc.tokens[(PONS, owner)] = 10**18
    res = await swap.execute(USER, built(is_buy=False, amount_in=36 * 10**18, amount_out=10**16), PONS, "PONS")
    assert not res.ok and "less of that token" in res.error and fc.sent == []


@pytest.mark.asyncio
async def test_receipt_timeout_is_pending_and_remembered(fc, wallet):
    fc.native[wallet.address.lower()] = 10**18
    fc.receipt = None
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert not res.ok and res.pending and res.tx
    assert "unconfirmed" in res.error and "explorer" in res.error.lower()
    assert ledger.entries_for(USER)[0]["status"] == "submitted"
    assert [tx for _uid, tx, _ts in ledger.unresolved()] == [res.tx.lower()]
    assert swap._pending[USER][0] == res.tx
    # The next attempt is blocked until that hash resolves.
    again = await swap.execute(USER, built(), PONS, "PONS")
    assert not again.ok and res.tx in again.error and len(fc.sent) == 1


@pytest.mark.asyncio
async def test_on_chain_revert_is_reported_with_the_hash(fc, wallet):
    fc.native[wallet.address.lower()] = 10**18
    fc.receipt = {"status": 0, "gasUsed": 100_000, "effectiveGasPrice": 300_000_000}
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert not res.ok and not res.pending and res.tx and "reverted" in res.error
    assert res.gas_cost_wei == 100_000 * 300_000_000
    assert [e["status"] for e in ledger.entries_for(USER)] == ["submitted", "reverted"]


@pytest.mark.asyncio
async def test_failed_approval_stops_the_sell(fc, wallet):
    owner = wallet.address.lower()
    fc.native[owner] = 10**17
    fc.tokens[(PONS, owner)] = 50 * 10**18
    fc.receipt = {"status": 0, "gasUsed": 50_000, "effectiveGasPrice": 300_000_000}
    res = await swap.execute(USER, built(is_buy=False, amount_in=36 * 10**18, amount_out=10**16), PONS, "PONS")
    assert not res.ok and "approval" in res.error.lower()
    assert len(fc.sent) == 1, "the swap must not be sent after a failed approval"


# ---------------- withdraw ----------------


@pytest.mark.asyncio
async def test_send_native_checks_address_denylist_zero_and_balance(fc, wallet):
    owner = wallet.address.lower()
    res = await swap.send_native(USER, "not-an-address", 10**16)
    assert not res.ok and "valid address" in res.error
    res = await swap.send_native(USER, "0xE592427A0AEce92De3Edee1F18E0157C05861564", 10**16)
    assert not res.ok and "drainer" in res.error
    res = await swap.send_native(USER, chain.ZERO, 10**16)
    assert not res.ok and "zero address" in res.error
    fc.native[owner] = 10**16
    res = await swap.send_native(USER, "0x000000000000000000000000000000000000dEaD", 10**16)
    assert not res.ok and "Not enough ETH" in res.error and fc.sent == []
    fc.native[owner] = 10**18
    res = await swap.send_native(USER, "0x000000000000000000000000000000000000dEaD", 10**16)
    assert res.ok and len(fc.sent) == 1 and res.amount_out == 10**16


@pytest.mark.asyncio
async def test_send_native_timeout_is_pending_and_blocks_the_next(fc, wallet):
    fc.native[wallet.address.lower()] = 10**18
    fc.receipt = None
    res = await swap.send_native(USER, "0x000000000000000000000000000000000000dEaD", 10**16)
    assert not res.ok and res.pending and res.tx
    again = await swap.send_native(USER, "0x000000000000000000000000000000000000dEaD", 10**16)
    assert not again.ok and res.tx in again.error and len(fc.sent) == 1


@pytest.mark.asyncio
async def test_signed_transaction_carries_the_right_chain_fees_and_target(fc, wallet):
    from eth_account import Account
    from eth_account.typed_transactions import TypedTransaction
    from hexbytes import HexBytes

    fc.native[wallet.address.lower()] = 10**18
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert res.ok
    raw = fc.sent[0]
    assert Account.recover_transaction(raw).lower() == wallet.address.lower(), "signed by the user's own key"
    fields = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
    to = fields["to"]
    to = to if isinstance(to, str) else "0x" + bytes(to).hex()
    assert fields["chainId"] == 4663
    assert fields["maxPriorityFeePerGas"] == 0, "FCFS sequencer: a tip buys nothing"
    assert fields["maxFeePerGas"] == 300_000_000 * swap.FEE_MULTIPLIER
    assert to.lower() == RHC_KYBER_ROUTER.lower()
    assert fields["value"] == 10**16
    assert fields["nonce"] == 4
    assert fields["gas"] == 400_000 + 400_000 * swap.GAS_BUFFER_PCT // 100


@pytest.mark.asyncio
async def test_broadcast_is_journaled_before_the_receipt_arrives(fc, wallet):
    fc.native[wallet.address.lower()] = 10**18
    res = await swap.execute(USER, built(), PONS, "PONS")
    statuses = [e["status"] for e in ledger.entries_for(USER) if e.get("tx") == res.tx]
    assert statuses == ["submitted", "confirmed"]


def test_restore_pending_rebuilds_the_guard_from_the_journal(fc):
    ledger.journal({"ts": 10.0, "user_id": USER, "kind": "buy", "token": PONS, "tx": "0xlive", "status": "submitted"})
    ledger.journal({"ts": 11.0, "user_id": 8, "kind": "buy", "token": PONS, "tx": "0xdone", "status": "submitted"})
    ledger.journal({"ts": 12.0, "user_id": 8, "kind": "resolution", "tx": "0xdone", "status": "confirmed"})
    assert swap.restore_pending() == 1
    assert swap._pending[USER] == ("0xlive", 10.0) and 8 not in swap._pending


@pytest.mark.asyncio
async def test_dropped_pending_buy_is_journaled_and_refunded(fc, wallet):
    fc.native[wallet.address.lower()] = 10**18
    ledger.record(USER, 24.8)
    ledger.journal({"ts": time.time() - 10**6, "user_id": USER, "kind": "buy", "token": PONS, "tx": "0xold",
                    "status": "submitted", "usd_in": 24.8})
    swap._pending[USER] = ("0xold", time.time() - 10**6)
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert res.ok, "an expired pending marker no longer blocks"
    # The dropped buy's $24.80 reservation came back; swap.execute itself never
    # writes the ledger (the cog does), so nothing else is charged here.
    assert ledger.spent_today(USER) == 0.0
    statuses = {e["tx"]: e["status"] for e in ledger.entries_for(USER) if e.get("kind") == "resolution"}
    assert statuses.get("0xold") == "dropped"


@pytest.mark.asyncio
async def test_resolved_pending_transaction_is_journaled_and_unblocks(fc, wallet):
    fc.native[wallet.address.lower()] = 10**18
    swap._pending[USER] = ("0xseen", time.time())
    fc.receipts["0xseen"] = {"status": 0, "gasUsed": 1, "effectiveGasPrice": 1}
    res = await swap.execute(USER, built(), PONS, "PONS")
    assert res.ok
    assert any(e.get("tx") == "0xseen" and e["status"] == "reverted" for e in ledger.entries_for(USER))


def test_describe_error_maps_the_cases_a_trader_needs():
    assert "gas" in swap.describe_error(Exception("insufficient funds for gas * price + value"))
    assert "slippage" in swap.describe_error(Exception("execution reverted: Return amount is not enough"))
    assert "Nonce" in swap.describe_error(Exception("nonce too low"))
    assert "reverted" in swap.describe_error(Exception("execution reverted"))
    assert "reach" in swap.describe_error(chain.RpcUnavailable("all failed"))


# ---------------- in-flight helpers for the auto-order engine ----------------


def test_has_inflight_and_busy_errors(fc):
    assert not swap.has_inflight(USER)
    swap._pending[USER] = ("0xabc", time.time())
    assert swap.has_inflight(USER), "an unresolved broadcast counts"
    swap._pending.clear()
    lk = swap._lock(USER)

    async def hold():
        async with lk:
            return swap.has_inflight(USER)
    assert asyncio.run(hold()) is True and not swap.has_inflight(USER)
    assert swap.is_busy_error(swap.IN_FLIGHT_TEXT)
    assert swap.is_busy_error("Your earlier transaction 0xabc is still unconfirmed. Check it ...")
    assert not swap.is_busy_error("The transaction reverted.") and not swap.is_busy_error(None)


def test_poll_pending_reports_the_final_word(fc):
    now = time.time()
    swap._pending[USER] = ("0xaaa", now)
    assert asyncio.run(swap.poll_pending(USER, "0xaaa")) == "pending"
    fc.receipts["0xaaa"] = {"status": 1, "gasUsed": 21_000, "effectiveGasPrice": 300_000_000}
    assert asyncio.run(swap.poll_pending(USER, "0xaaa")) == "confirmed"
    assert USER not in swap._pending
    assert asyncio.run(swap.poll_pending(USER, "0xaaa")) == "confirmed", "answered from the journal afterwards"

    swap._pending[USER] = ("0xbbb", now)
    fc.receipts["0xbbb"] = {"status": 0, "gasUsed": 21_000, "effectiveGasPrice": 300_000_000}
    assert asyncio.run(swap.poll_pending(USER, "0xbbb")) == "reverted"

    swap._pending[USER] = ("0xccc", now - swap.RHC_PENDING_BLOCK_SECONDS - 1)
    assert asyncio.run(swap.poll_pending(USER, "0xccc")) == "dropped"
    assert asyncio.run(swap.poll_pending(USER, "0xnever")) is None
