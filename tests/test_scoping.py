"""Scoping when the app is invoked from a user installation.

User-installed commands run with no guild. Falling back to `guild_id or 0`
would put every user's private alerts in one bucket and show them to each
other, so these tests guard that boundary.
"""

from types import SimpleNamespace

from mccapbot.cogs.alerts import AlertsCog
from mccapbot.cogs.watch import _owns
from mccapbot.models import MoveAlert, Reminder, WatchItem
from mccapbot.storage import move_alerts, reminders

ALICE, BOB = 111, 222
GUILD = 999


def inter(guild_id, user_id):
    return SimpleNamespace(guild_id=guild_id, user=SimpleNamespace(id=user_id))


def rem(creator, guild, name="t"):
    return Reminder(
        ca=f"CA-{name}", target_mc=1.0, direction="above", channel_id=1,
        creator_id=creator, guild_id=guild, name=name, symbol=name,
    )


def setup_function():
    reminders.clear()
    move_alerts.clear()


def teardown_function():
    reminders.clear()
    move_alerts.clear()


# ---------------- alerts ----------------


def test_guild_context_shows_the_guild():
    reminders.extend([rem(ALICE, GUILD, "a"), rem(BOB, GUILD, "b"), rem(ALICE, 555, "c")])
    got = AlertsCog._scoped(inter(GUILD, ALICE))
    assert {r.name for r in got} == {"a", "b"}


def test_user_context_shows_only_the_callers_alerts():
    """Alice in a DM must not see Bob's alerts."""
    reminders.extend([rem(ALICE, GUILD, "a"), rem(BOB, GUILD, "b"), rem(BOB, 0, "b-dm")])
    got = AlertsCog._scoped(inter(None, ALICE))
    assert {r.name for r in got} == {"a"}


def test_user_context_spans_every_guild_the_caller_has_alerts_in():
    reminders.extend([rem(ALICE, 111, "x"), rem(ALICE, 222, "y"), rem(BOB, 333, "z")])
    got = AlertsCog._scoped(inter(None, ALICE))
    assert {r.name for r in got} == {"x", "y"}


def test_zero_guild_alerts_do_not_pool_across_users():
    """The bug this guards: two users' guildless alerts sharing guild_id 0."""
    reminders.extend([rem(ALICE, 0, "alice"), rem(BOB, 0, "bob")])
    assert {r.name for r in AlertsCog._scoped(inter(None, ALICE))} == {"alice"}
    assert {r.name for r in AlertsCog._scoped(inter(None, BOB))} == {"bob"}


def test_move_alert_scoping_matches():
    move_alerts.extend([
        MoveAlert(ca="A", pct=1, window_sec=3600, direction="both", channel_id=1,
                  creator_id=ALICE, guild_id=0, name="a", symbol="a"),
        MoveAlert(ca="B", pct=1, window_sec=3600, direction="both", channel_id=1,
                  creator_id=BOB, guild_id=0, name="b", symbol="b"),
    ])
    assert {m.name for m in AlertsCog._scoped_moves(inter(None, ALICE))} == {"a"}


# ---------------- watchlists ----------------


def watch(owner, guild, name="w"):
    return WatchItem(ca=f"CA-{name}", guild_id=guild, added_by=owner, name=name, symbol=name)


def test_watch_in_guild_is_shared():
    """A server watchlist is shared, so Bob sees what Alice added."""
    assert _owns(watch(ALICE, GUILD), guild_id=GUILD, user_id=BOB) is True


def test_watch_outside_a_guild_is_personal():
    assert _owns(watch(ALICE, 0), guild_id=None, user_id=ALICE) is True
    assert _owns(watch(ALICE, 0), guild_id=None, user_id=BOB) is False


def test_guild_entries_do_not_leak_into_a_dm():
    assert _owns(watch(ALICE, GUILD), guild_id=None, user_id=ALICE) is False
