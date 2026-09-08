"""Combined holdings for the status line and About Me. Aggregate only, best effort."""

import pytest

from mccapbot.bot import Bot
from mccapbot.rhc import chain, ledger, portfolio, wallets

PONS = "0x39dbed3a2bd333467115de45665cc57f813c4571"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    wallets.wallets.clear()
    wallets._loaded = True
    portfolio._cache = None
    yield
    wallets.wallets.clear()
    portfolio._cache = None


def fake_wallet(user_id, address):
    return wallets.Wallet(user_id=user_id, address=address, salt="", nonce="", ciphertext="", ops=1, mem=1)


@pytest.mark.asyncio
async def test_empty_vault_summary_and_texts():
    s = await portfolio.summary()
    assert s.wallets == 0 and s.eth == 0
    assert portfolio.presence_fragment(s) == ""
    assert portfolio.presence_fragment(None) == ""
    assert "No Robinhood Chain wallets yet" in portfolio.about_me(s)
    assert Bot._presence_text(0.5, s) == "💰 0.50 SOL"


@pytest.mark.asyncio
async def test_summary_adds_eth_and_tokens_across_wallets(monkeypatch):
    wallets.wallets.extend([fake_wallet(1, "0x" + "a1" * 20), fake_wallet(2, "0x" + "b2" * 20)])

    async def native_balance(addr):
        return {"0x" + "a1" * 20: 2 * 10**16, "0x" + "b2" * 20: 3 * 10**16}[addr]

    async def erc20_balance(token, owner):
        return 36 * 10**18 if owner == "0x" + "a1" * 20 else 0

    async def erc20_meta(token):
        return ("PONS", 18)

    async def token_summary(addr):
        return {"price": 2500.0} if addr.lower() == chain.WETH.lower() else {"price": 0.7}

    monkeypatch.setattr(chain, "native_balance", native_balance)
    monkeypatch.setattr(chain, "erc20_balance", erc20_balance)
    monkeypatch.setattr(chain, "erc20_meta", erc20_meta)
    monkeypatch.setattr(portfolio, "token_summary", token_summary)
    monkeypatch.setattr(ledger, "tokens_touched", lambda uid: [PONS] if uid == 1 else [])

    s = await portfolio.summary()
    assert s.wallets == 2 and s.readable == 2
    assert s.eth == pytest.approx(0.05)
    assert s.eth_value_usd == pytest.approx(125.0)
    assert s.positions == [("PONS", 36.0, pytest.approx(25.2))]
    assert s.total_usd == pytest.approx(150.2)

    frag = portfolio.presence_fragment(s)
    assert frag == "RH $150.20"
    line = Bot._presence_text(1.0, s)
    assert line == "💰 1.00 SOL · RH $150.20"

    about = portfolio.about_me(s)
    assert "2 wallets hold $150.20: 0.05 ETH ($125.00) + 36 PONS ($25.20)" in about, about
    assert about.endswith("/rh holdings · /rh pnl · /rh stats") and "Updated" not in about
    assert len(about) <= 400


def test_about_me_drops_positions_before_cutting_a_sentence():
    positions = [(f"TOKEN{i:02d}", 1234.5, 12.0) for i in range(40)]
    s = portfolio.Summary(wallets=3, readable=3, eth_wei=10**18, eth_usd=2500.0,
                          tokens_usd=480.0, positions=positions)
    about = portfolio.about_me(s)
    assert len(about) <= 400
    assert about.endswith("/rh holdings · /rh pnl · /rh stats"), "the tail survives; positions are what get dropped"
    assert "3 wallets hold $2.98K: 1 ETH ($2.5K)" in about
    assert "TOKEN39" not in about and "TOKEN00" in about


@pytest.mark.asyncio
async def test_unreadable_wallet_is_skipped_not_zeroed(monkeypatch):
    wallets.wallets.extend([fake_wallet(1, "0x" + "a1" * 20), fake_wallet(2, "0x" + "b2" * 20)])

    async def native_balance(addr):
        if addr == "0x" + "b2" * 20:
            raise chain.RpcUnavailable("down")
        return 10**18

    async def token_summary(addr):
        return None
    monkeypatch.setattr(chain, "native_balance", native_balance)
    monkeypatch.setattr(portfolio, "token_summary", token_summary)
    monkeypatch.setattr(ledger, "tokens_touched", lambda uid: [])
    s = await portfolio.summary()
    assert s.readable == 1 and s.eth == 1.0 and s.eth_usd is None and s.total_usd is None
    assert portfolio.presence_fragment(s) == "RH 1 ETH"


@pytest.mark.asyncio
async def test_summary_is_cached(monkeypatch):
    wallets.wallets.append(fake_wallet(1, "0x" + "a1" * 20))
    calls = []

    async def native_balance(addr):
        calls.append(addr)
        return 10**18

    async def token_summary(addr):
        return {"price": 2500.0}
    monkeypatch.setattr(chain, "native_balance", native_balance)
    monkeypatch.setattr(portfolio, "token_summary", token_summary)
    monkeypatch.setattr(ledger, "tokens_touched", lambda uid: [])
    await portfolio.summary()
    await portfolio.summary()
    assert len(calls) == 1
    await portfolio.summary(force=True)
    assert len(calls) == 2
