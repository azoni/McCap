"""The status line under the bot's name: SOL balance plus the Robinhood Chain total."""

from mccapbot.bot import Bot
from mccapbot.rhc import portfolio

DISCORD_ACTIVITY_LIMIT = 128


def test_balance_is_rounded_to_two_places():
    assert "💰 12.35 SOL" in Bot._presence_text(12.3456)
    assert "💰 1,234.00 SOL" in Bot._presence_text(1234.0)


def test_unknown_balance_is_omitted_not_shown_as_zero():
    """A failed RPC call must never render as an empty wallet."""
    line = Bot._presence_text(None)
    assert "SOL" not in line
    assert "0.00" not in line


def test_a_genuinely_empty_wallet_still_displays():
    """Zero is real information; only None means 'unknown'."""
    assert "💰 0.00 SOL" in Bot._presence_text(0.0)


def test_balance_alone_when_nothing_else_is_known():
    assert Bot._presence_text(0.5) == "💰 0.50 SOL"


def test_falls_back_when_nothing_is_known():
    assert Bot._presence_text(None) == "for /mc alerts"


def test_alert_and_token_counts_are_not_in_the_status_line():
    """They used to be; they belong in /mc_status."""
    line = Bot._presence_text(1.0)
    assert "alert" not in line and "token" not in line


def test_robinhood_chain_total_sits_next_to_the_sol_balance():
    s = portfolio.Summary(wallets=2, readable=2, eth_wei=5 * 10**16, eth_usd=2500.0,
                          tokens_usd=25.2, positions=[("PONS", 36.0, 25.2)])
    line = Bot._presence_text(1.0, s)
    assert line == "💰 1.00 SOL · RH 0.050 ETH + 1 token ($150)"
    assert len(line) <= DISCORD_ACTIVITY_LIMIT
    assert Bot._presence_text(None, s) == "RH 0.050 ETH + 1 token ($150)"
