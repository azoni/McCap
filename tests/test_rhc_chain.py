"""rhc.chain: amounts, addresses, revert detection, encoding, receipts, broadcasting.
No real RPC; the web3 client is replaced with fakes where a call is exercised."""

import pytest

from mccapbot.config import RHC_KYBER_ROUTER
from mccapbot.rhc import chain


def test_to_units_parses_decimals_and_rounds_down():
    assert chain.to_units("1", 18) == 10**18
    assert chain.to_units("0.01", 18) == 10**16
    assert chain.to_units("0.000000000000000001", 18) == 1
    assert chain.to_units(".5", 18) == 5 * 10**17
    assert chain.to_units("1.23456789", 6) == 1_234_567          # extra precision is dropped, not rounded up
    assert chain.to_units("36", 0) == 36


@pytest.mark.parametrize("bad", ["", "-1", "abc", "1e5", "1.2.3", "0x10", " ", "1 000", "0,05", "1,234.5"])
def test_to_units_rejects_junk_including_commas(bad):
    """'0,05' is 0.05 ETH on a European keyboard and 5 ETH to a comma-stripping parser."""
    with pytest.raises(ValueError):
        chain.to_units(bad, 18)


def test_fmt_units_is_readable():
    assert chain.fmt_units(10**18, 18) == "1"
    assert chain.fmt_units(10**16, 18) == "0.01"
    assert chain.fmt_units(36_168_677_000_000_000_000, 18) == "36.168677"
    assert chain.fmt_units(1_234_500_000, 6) == "1,234.5"
    assert chain.fmt_units(0, 18) == "0"
    assert chain.fmt_units(5, 0) == "5"
    assert chain.fmt_units(123_456_789, 18, places=3) == "0"


def test_address_helpers():
    assert chain.is_address(RHC_KYBER_ROUTER)
    assert chain.is_address(RHC_KYBER_ROUTER.lower())
    assert not chain.is_address("0x123")
    assert not chain.is_address("PONS")
    assert not chain.is_address("")
    assert chain.is_native(chain.NATIVE) and chain.is_native(chain.NATIVE.lower())
    assert not chain.is_native(chain.WETH)
    assert chain.to_checksum(RHC_KYBER_ROUTER.lower()) == RHC_KYBER_ROUTER
    assert chain.ZERO == "0x" + "0" * 40


def test_denylist_holds_the_three_squatted_addresses_lowercased():
    assert "0xe592427a0aece92de3edee1f18e0157c05861564" in chain.DENYLIST
    assert all(a == a.lower() for a in chain.DENYLIST)
    assert RHC_KYBER_ROUTER.lower() not in chain.DENYLIST


def test_revert_detection_separates_chain_answers_from_transport_failures():
    class ContractLogicError(Exception):
        pass
    assert chain._is_revert(ContractLogicError("execution reverted: Return amount is not enough"))
    assert chain._is_revert(Exception("execution reverted"))
    assert not chain._is_revert(TimeoutError("timed out"))
    assert not chain._is_revert(ConnectionError("connection refused"))


def test_approve_calldata_encodes_spender_and_exact_amount():
    data = chain.approve_calldata(RHC_KYBER_ROUTER, 36 * 10**18)
    assert data.startswith("0x095ea7b3")
    assert data[10:74].lower().endswith(RHC_KYBER_ROUTER.lower()[2:])
    assert int(data[74:138], 16) == 36 * 10**18


def test_explorer_links():
    assert chain.explorer_tx("0xabc").endswith("/tx/0xabc")
    assert chain.explorer_address("0xdef").endswith("/address/0xdef")


# ---------------- with_client / receipts / broadcast, against fake web3 clients ----------------


class FakeEth:
    def __init__(self, behaviours):
        self.b = behaviours          # name -> value or exception or callable

    def _do(self, name, *args):
        v = self.b[name]
        if callable(v) and not isinstance(v, type):
            v = v(*args)
        if isinstance(v, BaseException):
            raise v
        return v

    async def get_transaction_receipt(self, h):
        return self._do("receipt", h)

    async def send_raw_transaction(self, raw):
        return self._do("send", raw)

    @property
    async def block_number(self):
        return 1


class FakeW3:
    def __init__(self, behaviours):
        self.eth = FakeEth(behaviours)


class TransactionNotFound(Exception):
    pass


def install(monkeypatch, *clients):
    """Replace endpoints with fake clients in the given order."""
    urls = [f"http://fake{i}" for i in range(len(clients))]
    monkeypatch.setattr(chain, "RHC_RPC_URLS", urls)
    monkeypatch.setattr(chain, "_clients", dict(zip(urls, clients)))
    monkeypatch.setattr(chain, "_latency", {u: i for i, u in enumerate(urls)})
    monkeypatch.setattr(chain, "_ranked_at", float("inf"))   # never re-rank during the test


@pytest.mark.asyncio
async def test_with_client_falls_back_on_transport_but_not_on_revert(monkeypatch):
    class ContractLogicError(Exception):
        pass
    install(monkeypatch,
            FakeW3({"receipt": ConnectionError("down")}),
            FakeW3({"receipt": {"status": 1}}))
    assert await chain.receipt("0x1") == {"status": 1}

    install(monkeypatch,
            FakeW3({"receipt": ContractLogicError("execution reverted")}),
            FakeW3({"receipt": {"status": 1}}))
    with pytest.raises(chain.RevertError):
        await chain.receipt("0x1")

    install(monkeypatch, FakeW3({"receipt": ConnectionError("a")}), FakeW3({"receipt": TimeoutError("b")}))
    with pytest.raises(chain.RpcUnavailable):
        await chain.receipt("0x1")


@pytest.mark.asyncio
async def test_not_mined_yet_is_none_and_wait_keeps_polling(monkeypatch):
    """The bug this guards: 'not found' used to be a hard error, so every swap
    reported pending and approvals crashed on the first poll."""
    calls = {"n": 0}

    def receipt(h):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransactionNotFound(f"Transaction with hash {h} not found.")
        return {"status": 1, "gasUsed": 1}
    install(monkeypatch, FakeW3({"receipt": receipt}))
    assert await chain.receipt("0x1") is None
    assert await chain.receipt("0x1") is None
    rec = await chain.wait_receipt("0x1", timeout=5)
    assert rec == {"status": 1, "gasUsed": 1} and calls["n"] == 3


@pytest.mark.asyncio
async def test_wait_receipt_survives_transport_blips_and_times_out_to_none(monkeypatch):
    install(monkeypatch, FakeW3({"receipt": ConnectionError("blip")}))
    assert await chain.wait_receipt("0x1", timeout=1.5) is None


@pytest.mark.asyncio
async def test_send_raw_returns_node_hash_on_success(monkeypatch):
    raw = b"\x02" + b"\x11" * 40
    local = chain.local_tx_hash(raw)

    class H(bytes):
        def to_0x_hex(self):
            return "0x" + self.hex()
    install(monkeypatch, FakeW3({"send": H(bytes.fromhex(local[2:]))}))
    assert await chain.send_raw(raw) == local


@pytest.mark.asyncio
async def test_send_raw_never_retries_on_another_endpoint(monkeypatch):
    """A lost response is 'possibly sent', never 'failed' (that invites a double
    spend) and never re-sent elsewhere."""
    raw = b"\x02" + b"\x22" * 40
    second = {"called": False}

    def second_send(r):
        second["called"] = True
        return b"\x00" * 32
    install(monkeypatch, FakeW3({"send": TimeoutError("read timed out")}), FakeW3({"send": second_send}))
    assert await chain.send_raw(raw) == chain.local_tx_hash(raw)
    assert not second["called"]


@pytest.mark.asyncio
async def test_send_raw_distinguishes_definite_rejection_from_already_known(monkeypatch):
    raw = b"\x02" + b"\x33" * 40
    install(monkeypatch, FakeW3({"send": ValueError("{'code': -32000, 'message': 'nonce too low'}")}))
    with pytest.raises(chain.ChainError, match="nonce too low"):
        await chain.send_raw(raw)
    install(monkeypatch, FakeW3({"send": ValueError("insufficient funds for gas * price + value")}))
    with pytest.raises(chain.ChainError):
        await chain.send_raw(raw)
    install(monkeypatch, FakeW3({"send": ValueError("already known")}))
    assert await chain.send_raw(raw) == chain.local_tx_hash(raw)
