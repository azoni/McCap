"""Cost basis, multiples and profit from the trade journal."""

import os

import pytest

from mccapbot.rhc import chain, ledger, pnl, wallets

PONS = "0x39dbed3a2bd333467115de45665cc57f813c4571"
LAPTOP = "0x76ed1e2a8fc3873fcb5c514688ca2fe8a3600b7f"
USER = 7
ADDR = "0x" + "a1" * 20


@pytest.fixture(autouse=True)
def clean():
    for p in (ledger.RHC_JOURNAL_FILE, ledger.RHC_LEDGER_FILE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    wallets.wallets.clear()
    wallets._loaded = True
    yield
    for p in (ledger.RHC_JOURNAL_FILE, ledger.RHC_LEDGER_FILE):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    wallets.wallets.clear()


def buy(tx, token, symbol, eth_wei, tokens_raw, usd, mc, gas=10**14, status="confirmed", ts=1.0, user=USER):
    base = {"ts": ts, "user_id": user, "kind": "buy", "token": token, "symbol": symbol, "decimals": 18,
            "amount_in": str(eth_wei), "quoted_out": str(tokens_raw), "actual_out_estimate": str(tokens_raw),
            "usd_in": usd, "usd_out": usd, "mc_usd": mc, "tx": tx}
    ledger.journal({**base, "status": "submitted", "gas_cost_wei": "0"})
    ledger.journal({**base, "status": status, "gas_cost_wei": str(gas)})


def sell(tx, token, symbol, tokens_raw, eth_wei, usd, mc, multiple=None, gas=10**14, ts=2.0, user=USER):
    base = {"ts": ts, "user_id": user, "kind": "sell", "token": token, "symbol": symbol, "decimals": 18,
            "amount_in": str(tokens_raw), "quoted_out": str(eth_wei), "actual_out_estimate": str(eth_wei),
            "usd_in": usd, "usd_out": usd, "mc_usd": mc, "multiple": multiple, "tx": tx}
    ledger.journal({**base, "status": "submitted", "gas_cost_wei": "0"})
    ledger.journal({**base, "status": "confirmed", "gas_cost_wei": str(gas)})


# ---------------- journal -> positions ----------------


def test_history_collapses_the_submitted_and_confirmed_records():
    buy("0x1", PONS, "PONS", 10**16, 36 * 10**18, 25.0, 500_000.0)
    buy("0x2", LAPTOP, "LAPTOP", 10**15, 2000 * 10**18, 2.5, 400_000.0, status="reverted", ts=1.5)
    h = ledger.history(USER)
    assert [(e["tx"], e["final_status"]) for e in h] == [("0x1", "confirmed"), ("0x2", "reverted")]
    assert [e["tx"] for e in ledger.trades(USER)] == ["0x1"], "reverted trades carry no cost"


def test_aggregate_cost_entry_mc_and_multiple():
    buy("0x1", PONS, "PONS", 10**16, 40 * 10**18, 20.0, 50_000.0)        # 40 PONS for $20 at $50K MC
    buy("0x2", PONS, "PONS", 10**16, 20 * 10**18, 20.0, 100_000.0, ts=1.5)  # 20 more for $20 at $100K MC
    pos = pnl.aggregate(ledger.trades(USER))[PONS]
    assert pos.bought == 60 and pos.cost_usd == 40.0 and pos.buys == 2
    assert pos.avg_cost == pytest.approx(40 / 60)
    assert pos.entry_mc == pytest.approx(75_000.0), "cost-weighted entry market cap"
    assert pos.gas_wei == 2 * 10**14
    pos.balance_raw, pos.mc_now, pos.price_now = 60 * 10**18, 300_000.0, 4.0
    assert pos.multiple_now == pytest.approx(4.0)                    # 75K -> 300K
    assert pos.worth_usd == pytest.approx(240.0)
    assert pos.unrealized_usd == pytest.approx(200.0)
    assert pos.realized_usd == 0.0
    assert pos.pnl_pct == pytest.approx(500.0)


def test_realized_profit_uses_average_cost():
    buy("0x1", PONS, "PONS", 10**16, 40 * 10**18, 20.0, 50_000.0)
    sell("0x2", PONS, "PONS", 20 * 10**18, 4 * 10**16, 100.0, 200_000.0, multiple=4.0)   # half of it at 4x
    pos = pnl.aggregate(ledger.trades(USER))[PONS]
    assert pos.sold == 20 and pos.proceeds_usd == 100.0
    assert pos.realized_usd == pytest.approx(100.0 - 20 * 0.5)      # avg cost $0.50 per token
    assert pos.last_sell_mc == 200_000.0
    pos.balance_raw, pos.price_now = 20 * 10**18, 5.0
    assert pos.unrealized_usd == pytest.approx(100.0 - 10.0)
    assert pos.pnl_usd == pytest.approx(90.0 + 90.0)


def test_entry_for_is_journal_only():
    buy("0x1", PONS, "PONS", 10**16, 40 * 10**18, 20.0, 50_000.0)
    assert pnl.entry_for(USER, PONS.upper()).entry_mc == 50_000.0
    assert pnl.entry_for(USER, LAPTOP) is None


# ---------------- live enrichment ----------------


@pytest.mark.asyncio
async def test_user_pnl_adds_balances_prices_and_gas(monkeypatch):
    buy("0x1", PONS, "PONS", 10**16, 40 * 10**18, 20.0, 50_000.0, gas=3 * 10**14)

    async def native_balance(addr):
        return 5 * 10**16

    async def erc20_balance(token, owner):
        return 40 * 10**18

    async def erc20_meta(token):
        return ("PONS", 18)

    async def token_summary(addr):
        if addr.lower() == chain.WETH.lower():
            return {"price": 2000.0}
        return {"price": 1.0, "mc": 100_000.0}
    monkeypatch.setattr(chain, "native_balance", native_balance)
    monkeypatch.setattr(chain, "erc20_balance", erc20_balance)
    monkeypatch.setattr(chain, "erc20_meta", erc20_meta)
    monkeypatch.setattr(pnl, "token_summary", token_summary)

    u = await pnl.user_pnl(USER, ADDR)
    assert u.eth == 0.05 and u.eth_value_usd == 100.0
    assert u.gas_wei == 3 * 10**14 and u.gas_usd == pytest.approx(0.6)
    p = u.open_positions[0]
    assert p.symbol == "PONS" and p.worth_usd == 40.0 and p.multiple_now == pytest.approx(2.0)
    assert u.tokens_worth_usd == 40.0 and u.total_usd == 140.0
    assert u.unrealized_usd == pytest.approx(20.0) and u.realized_usd == 0.0
    assert u.pnl_usd == pytest.approx(20.0 - 0.6), "gas comes off the net figure"


# ---------------- group stats ----------------


def test_group_stats_span_every_wallet():
    wallets.wallets.extend([
        wallets.Wallet(user_id=7, address=ADDR, salt="", nonce="", ciphertext="", ops=1, mem=1),
        wallets.Wallet(user_id=8, address="0x" + "b2" * 20, salt="", nonce="", ciphertext="", ops=1, mem=1),
        wallets.Wallet(user_id=9, address="0x" + "c3" * 20, salt="", nonce="", ciphertext="", ops=1, mem=1),
    ])
    buy("0x1", PONS, "PONS", 10**16, 40 * 10**18, 20.0, 50_000.0, user=7)
    sell("0x2", PONS, "PONS", 40 * 10**18, 8 * 10**16, 200.0, 500_000.0, multiple=10.0, user=7)
    buy("0x3", LAPTOP, "LAPTOP", 10**15, 2000 * 10**18, 2.5, 400_000.0, user=8)
    g = pnl.group_stats()
    assert g.wallets == 3 and g.traders == 2
    assert g.buys == 2 and g.sells == 1
    assert g.volume_usd == pytest.approx(222.5)
    assert g.gas_wei == 3 * 10**14
    assert g.realized_usd == pytest.approx(180.0)
    assert g.best_multiple == 10.0 and g.best_symbol == "PONS"


# ---------------- formatting and chart ----------------


def test_formatting_helpers():
    assert pnl.fmt_usd(1234.5) == "$1,234.50" and pnl.fmt_usd(None) == "—"
    assert pnl.fmt_usd(-3.2, signed=True) == "-$3.20" and pnl.fmt_usd(3.2, signed=True) == "+$3.20"
    assert pnl.fmt_x(4.0) == "4.00x" and pnl.fmt_x(12.34) == "12.3x" and pnl.fmt_x(None) == "—"
    assert pnl.fmt_amount(2062.09) == "2,062" and pnl.fmt_amount(3.33) == "3.33" and pnl.fmt_amount(0.00042) == "0.0004"


def test_chart_renders_a_png():
    u = pnl.UserPnl(user_id=USER, address=ADDR, eth_usd=2000.0, gas_wei=10**14)
    p = pnl.TokenPnl(token=PONS, symbol="PONS", bought_raw=40 * 10**18, cost_usd=20.0, buys=1,
                     balance_raw=40 * 10**18, price_now=1.0)
    u.positions.append(p)
    png = pnl.render_chart(u, "test")
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"
    assert pnl.render_chart(pnl.UserPnl(user_id=USER, address=ADDR), "empty") is None
