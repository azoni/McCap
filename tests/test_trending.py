"""Robinhood's tradeable pair list (robinhood.get_trading_pairs).

Market movement for /rh_trending now comes from the Robinhood chain's DEX pools
(see test_rhchain.py); this covers the pair list the trading commands rely on.
"""

import asyncio

import pytest

from mccapbot import robinhood


# ---------------- the tradeable pair list ----------------


def test_without_credentials_the_fallback_is_flagged_unverified(monkeypatch):
    """The command must say the list is approximate rather than imply Robinhood
    confirmed it."""
    monkeypatch.setattr(robinhood, "RH_API_KEY", "")
    monkeypatch.setattr(robinhood, "RH_PRIVATE_KEY_B64", "")
    symbols, authoritative = asyncio.run(robinhood.get_trading_pairs())
    assert authoritative is False
    assert "BTC" in symbols and len(symbols) > 5


def test_live_pairs_are_marked_authoritative(monkeypatch):
    async def fake_request(method, path, body=None):
        return {"results": [
            {"symbol": "BTC-USD", "status": "tradable"},
            {"symbol": "ETH-USD", "status": "tradable"},
        ]}

    monkeypatch.setattr(robinhood, "RH_API_KEY", "k")
    monkeypatch.setattr(robinhood, "RH_PRIVATE_KEY_B64", "x")
    monkeypatch.setattr(robinhood, "_request", fake_request)
    symbols, authoritative = asyncio.run(robinhood.get_trading_pairs())
    assert authoritative is True
    assert symbols == ["BTC", "ETH"]


def test_untradeable_pairs_are_excluded(monkeypatch):
    """No point offering a mover you cannot actually buy."""
    async def fake_request(method, path, body=None):
        return {"results": [
            {"symbol": "BTC-USD", "status": "tradable"},
            {"symbol": "OLD-USD", "status": "delisted"},
        ]}

    monkeypatch.setattr(robinhood, "RH_API_KEY", "k")
    monkeypatch.setattr(robinhood, "RH_PRIVATE_KEY_B64", "x")
    monkeypatch.setattr(robinhood, "_request", fake_request)
    symbols, _ = asyncio.run(robinhood.get_trading_pairs())
    assert symbols == ["BTC"]


def test_a_failed_pairs_call_falls_back_rather_than_erroring(monkeypatch):
    async def boom(method, path, body=None):
        raise robinhood.RobinhoodError("upstream down")

    monkeypatch.setattr(robinhood, "RH_API_KEY", "k")
    monkeypatch.setattr(robinhood, "RH_PRIVATE_KEY_B64", "x")
    monkeypatch.setattr(robinhood, "_request", boom)
    symbols, authoritative = asyncio.run(robinhood.get_trading_pairs())
    assert authoritative is False and symbols
