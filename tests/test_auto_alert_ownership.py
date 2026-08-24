"""Alerts armed automatically from a detected scan have no human owner.

They were created with the scanner bot's id, which pinged the *bot* when they
fired and made them removable only by someone with Manage Server.
"""

import pytest

import mccapbot.alerts as A
from mccapbot.cogs.alerts import AlertsCog
from mccapbot.models import MoveAlert, Reminder, ScanEvent


class User:
    def __init__(self, uid):
        self.id = uid


def move(owner):
    return MoveAlert(
        ca="CA1", pct=30, window_sec=3600, direction="both", channel_id=1,
        creator_id=owner, guild_id=3, name="Tok", symbol="TOK",
    )


# ---------------- mentions ----------------


def test_no_mention_without_an_owner():
    """Formatting creator_id 0 produced a literal broken '<@0>' ping."""
    assert A._mention(0) is None
    assert A._mention(None) is None


def test_owner_is_mentioned_normally():
    assert A._mention(1234567890) == "<@1234567890>"


# ---------------- removal permission ----------------


def test_owner_may_remove_their_own():
    assert AlertsCog._may_remove(User(7), owner_id=7, can_manage=False) is True


def test_stranger_may_not_remove_someone_elses():
    assert AlertsCog._may_remove(User(8), owner_id=7, can_manage=False) is False


def test_manage_server_may_remove_anything():
    assert AlertsCog._may_remove(User(8), owner_id=7, can_manage=True) is True


def test_anyone_may_remove_an_ownerless_alert():
    """Otherwise a scan-heavy server accumulates alerts only an admin can clear."""
    assert AlertsCog._may_remove(User(8), owner_id=0, can_manage=False) is True


# ---------------- attribution ----------------


def test_scan_event_keeps_scanner_and_requester_apart():
    """scanner_id identifies the bot that posted; requested_by the human who
    triggered it. Conflating them is what caused the ping-the-bot bug."""
    ev = ScanEvent(
        ca="CA1", guild_id=1, channel_id=2, scanner_id=999, name="T", symbol="T",
        mc_at_scan=1.0, requested_by=42,
    )
    assert ev.scanner_id != ev.requested_by
    assert ev.requested_by == 42


def test_unresolvable_requester_defaults_to_nobody():
    ev = ScanEvent(
        ca="CA1", guild_id=1, channel_id=2, scanner_id=999,
        name="T", symbol="T", mc_at_scan=1.0,
    )
    assert ev.requested_by == 0
    assert A._mention(ev.requested_by) is None
    assert AlertsCog._may_remove(User(123), ev.requested_by, can_manage=False) is True


@pytest.mark.asyncio
async def test_username_for_ownerless_reads_auto():
    from mccapbot.helpers import username_from_id

    class C:
        def get_user(self, _):
            raise AssertionError("must not look up id 0")

    assert await username_from_id(C(), 0) == "auto"
