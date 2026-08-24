"""The status line under the bot's name: SOL balance plus what it tracks."""

import asyncio

import pytest

from mccapbot.bot import Bot
from mccapbot.models import MoveAlert, Reminder
from mccapbot.storage import move_alerts, reminders

DISCORD_ACTIVITY_LIMIT = 128


def rem(ca="CA1"):
    return Reminder(
        ca=ca, target_mc=1_000_000, direction="above", channel_id=1,
        creator_id=1, guild_id=1, name="Tok", symbol="TOK",
    )


@pytest.fixture(autouse=True)
def clean():
    reminders.clear()
    move_alerts.clear()
    yield
    reminders.clear()
    move_alerts.clear()


def test_shows_balance_and_tracking_together():
    reminders.extend(rem(f"CA{i}") for i in range(40))
    line = Bot._presence_text(0.623001)
    assert "💰 0.62 SOL" in line
    assert "40 alert(s)" in line
    assert "40 token(s)" in line


def test_balance_is_rounded_to_two_places():
    assert "💰 12.35 SOL" in Bot._presence_text(12.3456)
    assert "💰 1,234.00 SOL" in Bot._presence_text(1234.0)


def test_unknown_balance_is_omitted_not_shown_as_zero():
    """A failed RPC call must never render as an empty wallet."""
    reminders.append(rem())
    line = Bot._presence_text(None)
    assert "SOL" not in line
    assert "0.00" not in line
    assert "1 alert(s)" in line


def test_a_genuinely_empty_wallet_still_displays():
    """Zero is real information; only None means 'unknown'."""
    assert "💰 0.00 SOL" in Bot._presence_text(0.0)


def test_balance_alone_when_nothing_is_tracked():
    assert Bot._presence_text(0.5) == "💰 0.50 SOL"


def test_falls_back_when_nothing_is_known():
    assert Bot._presence_text(None) == "for /mc alerts"


def test_move_alerts_count_toward_the_total():
    reminders.append(rem("CA1"))
    move_alerts.append(MoveAlert(
        ca="CA2", pct=30, window_sec=3600, direction="both", channel_id=1,
        creator_id=1, guild_id=1, name="M", symbol="M",
    ))
    line = Bot._presence_text(1.0)
    assert "2 alert(s)" in line
    assert "2 token(s)" in line


def test_shared_token_counted_once():
    reminders.extend([rem("SAME"), rem("SAME")])
    line = Bot._presence_text(1.0)
    assert "2 alert(s)" in line and "1 token(s)" in line


def test_stays_within_the_activity_limit():
    """Discord truncates or rejects an over-long activity name."""
    reminders.extend(rem(f"CA{i}") for i in range(5000))
    assert len(Bot._presence_text(123_456.789)) <= DISCORD_ACTIVITY_LIMIT


# ---------------- the balance call itself ----------------


def test_invalid_address_returns_none_without_calling_out():
    from mccapbot.solana import get_balance

    async def go():
        return await get_balance("not-a-real-address")

    assert asyncio.run(go()) is None


def test_empty_wallet_config_is_handled():
    from mccapbot.solana import get_balance

    async def go():
        return await get_balance("")

    assert asyncio.run(go()) is None


def test_lamports_conversion():
    from mccapbot.solana import LAMPORTS_PER_SOL

    assert LAMPORTS_PER_SOL == 1_000_000_000
    assert 623_001_000 / LAMPORTS_PER_SOL == pytest.approx(0.623001)
