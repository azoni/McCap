"""/mc_clear removes every alert in one server, for server managers, after a confirm."""

from types import SimpleNamespace

import pytest

from mccapbot.cogs import alerts as alerts_cog
from mccapbot.cogs.alerts import AlertsCog
from mccapbot.models import MoveAlert, Reminder
from mccapbot.storage import move_alerts, reminders


class FakeResponse:
    def __init__(self, sink):
        self.sink = sink

    async def send_message(self, content=None, **kw):
        self.sink.append((content, kw))


class FakeFollowup:
    def __init__(self, sink):
        self.sink = sink

    async def send(self, content=None, **kw):
        self.sink.append((content, kw))


class FakeInteraction:
    def __init__(self, guild_id=1, manage=True, user_id=5):
        self.sent = []
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id, guild_permissions=SimpleNamespace(manage_guild=manage, administrator=False))
        self.response = FakeResponse(self.sent)
        self.followup = FakeFollowup(self.sent)

    @property
    def last(self):
        return self.sent[-1][0]


class Confirmed:
    value = True

    def __init__(self, owner_id, timeout):
        pass

    async def wait(self):
        return


class Cancelled(Confirmed):
    value = False


def rem(guild_id, ca="CA1"):
    return Reminder(ca=ca, target_mc=1e6, direction="above", channel_id=1, creator_id=1, guild_id=guild_id,
                    name="Tok", symbol="TOK")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    reminders.clear()
    move_alerts.clear()
    # The real check requires a discord.Member; the fake carries the same permissions object.
    monkeypatch.setattr(AlertsCog, "_can_manage",
                        staticmethod(lambda user: bool(user.guild_permissions.manage_guild or user.guild_permissions.administrator)))
    yield
    reminders.clear()
    move_alerts.clear()


@pytest.mark.asyncio
async def test_clear_removes_only_this_servers_alerts(monkeypatch):
    monkeypatch.setattr(alerts_cog, "ConfirmOrder", Confirmed)
    reminders.extend([rem(1, "A"), rem(1, "B"), rem(2, "C")])
    move_alerts.append(MoveAlert(ca="D", pct=30, window_sec=3600, direction="both", channel_id=1, creator_id=1,
                                 guild_id=1, name="M", symbol="M"))
    inter = FakeInteraction(guild_id=1)
    await AlertsCog.mc_clear.callback(AlertsCog(bot=None), inter)
    assert "Cleared 2 level and 1 momentum" in inter.last
    assert [r.ca for r in reminders] == ["C"] and move_alerts == []


@pytest.mark.asyncio
async def test_clear_needs_manage_server_and_a_confirm(monkeypatch):
    reminders.append(rem(1))
    nope = FakeInteraction(guild_id=1, manage=False)
    await AlertsCog.mc_clear.callback(AlertsCog(bot=None), nope)
    assert nope.last.startswith("🔒") and len(reminders) == 1

    monkeypatch.setattr(alerts_cog, "ConfirmOrder", Cancelled)
    cancel = FakeInteraction(guild_id=1)
    await AlertsCog.mc_clear.callback(AlertsCog(bot=None), cancel)
    assert "Cancelled" in cancel.last and len(reminders) == 1

    empty = FakeInteraction(guild_id=9)
    await AlertsCog.mc_clear.callback(AlertsCog(bot=None), empty)
    assert "No active alerts" in empty.last
