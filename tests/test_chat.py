"""Chat: memory scoping, history windows, the tool loop, and spend guards.

No test here talks to the real API; ``respond`` takes a fake client.
"""

import json
import time
from types import SimpleNamespace

import anthropic
import pytest

from mccapbot import chat, storage
from mccapbot.models import MoveAlert, Reminder

try:  # anthropic 1.x is built on httpx2; older builds on httpx
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx


@pytest.fixture(autouse=True)
def _clean_state():
    storage.memory_notes.clear()
    storage.chat_turns.clear()
    storage.reminders.clear()
    storage.move_alerts.clear()
    yield
    storage.memory_notes.clear()
    storage.chat_turns.clear()
    storage.reminders.clear()
    storage.move_alerts.clear()


def ctx(guild_id=1, channel_id=10, user_id=100, user_name="charlton"):
    return chat.ChatContext(
        channel_id=channel_id, user_id=user_id, user_name=user_name,
        guild_id=guild_id, guild_name="mega-desk" if guild_id else "",
    )


def text_response(text, stop="end_turn"):
    return SimpleNamespace(
        stop_reason=stop,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def tool_response(name, inputs, tool_id="toolu_1"):
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", id=tool_id, name=name, input=inputs)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class FakeAPI:
    """Replays scripted responses and records every request."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._responses[0], Exception):
            raise self._responses[0]
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return item


# ---------------- scoping ----------------


def test_scope_is_per_server_and_private_per_dm():
    assert chat.scope_key(1315, 100) == "g1315"
    assert chat.scope_key(None, 100) == "u100"
    assert chat.scope_key(0, 100) == "u100"


@pytest.mark.asyncio
async def test_notes_are_scoped_and_persisted(_isolate_storage):
    note = await chat.remember("g1", "ship the RSTR fix", 100, "charlton")
    assert chat.notes_for("g1") == [note]
    assert chat.notes_for("g2") == []
    # Another server cannot delete it.
    assert await chat.forget("g2", note.id) is None
    assert await chat.forget("g1", note.id) is note
    assert chat.notes_for("g1") == []

    on_disk = json.load(open(storage.CHAT_MEMORY_FILE, encoding="utf-8"))
    assert on_disk == []


@pytest.mark.asyncio
async def test_note_cap_drops_the_oldest(monkeypatch):
    monkeypatch.setattr(chat, "CHAT_MAX_NOTES", 3)
    ids = []
    for i in range(5):
        n = await chat.remember("g1", f"note {i}", 100, "c")
        n.created_ts = 1000 + i  # deterministic ordering
        ids.append(n.id)
    kept = [n.text for n in chat.notes_for("g1")]
    assert kept == ["note 2", "note 3", "note 4"]


def test_describe_notes_shows_id_author_and_text():
    storage.memory_notes.append(
        chat.MemoryNote(scope="g1", text="use haiku", author_id=1, author_name="charlton", id="abc123", created_ts=0)
    )
    out = chat.describe_notes("g1")
    assert "[abc123]" in out and "charlton" in out and "use haiku" in out
    assert chat.describe_notes("g9") == "(nothing saved yet)"


# ---------------- history ----------------


def test_build_messages_names_speakers_and_merges_same_role():
    storage.chat_turns.extend([
        chat.ChatTurn(channel_id=10, role="assistant", content="stale leading reply"),
        chat.ChatTurn(channel_id=10, role="user", content="hey", author_name="alice"),
        chat.ChatTurn(channel_id=10, role="user", content="you there?", author_name="bob"),
        chat.ChatTurn(channel_id=10, role="assistant", content="yep"),
        chat.ChatTurn(channel_id=11, role="user", content="other channel", author_name="eve"),
    ])
    msgs = chat.build_messages(10, "charlton", "what's up")
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"] == "alice: hey\nbob: you there?"
    assert msgs[-1]["content"] == "charlton: what's up"
    assert "other channel" not in json.dumps(msgs)


@pytest.mark.asyncio
async def test_history_window_is_per_channel(monkeypatch):
    monkeypatch.setattr(chat, "CHAT_HISTORY_TURNS", 4)
    for i in range(5):
        await chat.record_exchange(10, "c", f"q{i}", f"a{i}")
    await chat.record_exchange(11, "c", "other", "reply")
    mine = chat.history_for(10)
    assert [t.content for t in mine] == ["q3", "a3", "q4", "a4"]
    assert len(chat.history_for(11)) == 2


# ---------------- the tool loop ----------------


@pytest.mark.asyncio
async def test_respond_runs_a_tool_then_replies():
    api = FakeAPI(
        tool_response("remember", {"text": "Charlton wants a /mc_purge command"}),
        text_response("Saved: you want a /mc_purge command."),
    )
    reply = await chat.respond(ctx(), "remember that I want a /mc_purge command", api=api)

    assert reply == "Saved: you want a /mc_purge command."
    assert [n.text for n in chat.notes_for("g1")] == ["Charlton wants a /mc_purge command"]
    assert len(api.calls) == 2
    # The second request carried the tool result back under the right id.
    second = api.calls[1]["messages"]
    assert second[-2]["role"] == "assistant"
    result = second[-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_1"
    assert result["content"].startswith("Saved as [")
    # Both sides of the exchange were recorded, without the tool internals.
    assert [t.role for t in chat.history_for(10)] == ["user", "assistant"]
    assert api.calls[0]["tools"] is chat.TOOLS
    assert "Notes for this server" in api.calls[0]["system"]


@pytest.mark.asyncio
async def test_respond_gives_up_after_the_tool_round_cap(monkeypatch):
    monkeypatch.setattr(chat, "CHAT_MAX_TOOL_ROUNDS", 2)
    api = FakeAPI(tool_response("list_alerts", {}))  # never stops asking
    reply = await chat.respond(ctx(), "loop forever", api=api)
    assert len(api.calls) == 3
    assert "loop" in reply.lower()


@pytest.mark.asyncio
async def test_respond_turns_api_errors_into_text():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.RateLimitError(
        "slow down", response=httpx.Response(429, request=request), body=None
    )
    reply = await chat.respond(ctx(), "hi", api=FakeAPI(err))
    assert "rate-limit" in reply
    assert chat.history_for(10) == []  # a failed call is not part of the conversation


@pytest.mark.asyncio
async def test_refusal_is_a_plain_sentence():
    reply = await chat.respond(ctx(), "x", api=FakeAPI(text_response("", stop="refusal")))
    assert reply == "I can't help with that one."


# ---------------- tools ----------------


@pytest.mark.asyncio
async def test_list_alerts_uses_mc_list_scoping():
    storage.reminders.extend([
        Reminder(ca="A" * 44, target_mc=1e6, direction="above", channel_id=1, creator_id=100,
                 guild_id=1, name="Alpha", symbol="ALPHA", id="aaa111"),
        Reminder(ca="B" * 44, target_mc=5e5, direction="below", channel_id=1, creator_id=200,
                 guild_id=2, name="Beta", symbol="BETA", id="bbb222"),
    ])
    storage.move_alerts.append(
        MoveAlert(ca="C" * 44, pct=30, window_sec=3600, direction="up", channel_id=1,
                  creator_id=100, guild_id=1, name="Gamma", symbol="GAMMA", id="ccc333")
    )
    in_guild = await chat.run_tool("list_alerts", {}, ctx(guild_id=1))
    assert "aaa111" in in_guild and "ccc333" in in_guild and "bbb222" not in in_guild
    assert "no market data yet" in in_guild

    in_dm = await chat.run_tool("list_alerts", {}, ctx(guild_id=0, user_id=200))
    assert "bbb222" in in_dm and "aaa111" not in in_dm
    assert await chat.run_tool("list_alerts", {}, ctx(guild_id=3)) == "No active alerts here."


@pytest.mark.asyncio
async def test_token_lookup_by_address_reads_the_summary(monkeypatch):
    async def fake_summary(ca):
        return {"ca": ca, "name": "Robinhood Hat Strategy", "symbol": "RSTR", "mc": 2_599_891,
                "liq": 254_000, "vol24": 1_200_000, "change24": -12.3, "pools": 23}
    monkeypatch.setattr(chat, "token_summary", fake_summary)
    out = await chat.run_tool("token_lookup", {"query": "0x78b96280c3347e0f58a7147b73eb0ec5ffff025d"}, ctx())
    assert "RSTR" in out and "$2.60M" in out and "-12.3%" in out


@pytest.mark.asyncio
async def test_token_lookup_by_name_dedupes_tokens_deepest_first(monkeypatch):
    async def fake_get_json(url, limiter=None):
        assert "search?q=rstr" in url
        return {"pairs": [
            {"chainId": "robinhood", "baseToken": {"address": "0xreal", "symbol": "RSTR", "name": "Real"},
             "marketCap": 2.6e6, "liquidity": {"usd": 150_000}},
            {"chainId": "robinhood", "baseToken": {"address": "0xreal", "symbol": "RSTR", "name": "Real"},
             "marketCap": 2.5e6, "liquidity": {"usd": 90_000}},
            {"chainId": "robinhood", "baseToken": {"address": "0xfake", "symbol": "RSTR", "name": "Copycat"},
             "marketCap": 7_000, "liquidity": {"usd": 7_000}},
        ]}
    monkeypatch.setattr(chat, "get_json", fake_get_json)
    out = await chat.run_tool("token_lookup", {"query": "rstr"}, ctx())
    lines = out.splitlines()[1:]
    assert len(lines) == 2
    assert "0xreal" in lines[0] and "0xfake" in lines[1]


@pytest.mark.asyncio
async def test_tool_failures_become_text_not_exceptions(monkeypatch):
    async def boom(ca):
        raise RuntimeError("dexscreener down")
    monkeypatch.setattr(chat, "token_summary", boom)
    out = await chat.run_tool("token_lookup", {"query": "0x" + "1" * 40}, ctx())
    assert "failed" in out
    assert await chat.run_tool("nope", {}, ctx()) == "Unknown tool nope."


# ---------------- spend guard ----------------


def test_spend_guard_cooldown_and_daily_cap():
    g = chat.SpendGuard(daily_cap=2, cooldown=3)
    t0 = 1_700_000_000.0
    assert g.check(1, t0) is None
    g.note_call(1, t0)
    assert g.check(1, t0 + 1) is not None       # too soon for the same user
    assert g.check(2, t0 + 1) is None           # someone else is fine
    g.note_call(2, t0 + 1)
    assert "budget" in g.check(3, t0 + 10)      # cap reached
    assert g.check(3, t0 + 86_400) is None      # new UTC day resets it
    assert g.calls_today == 0


# ---------------- Discord plumbing ----------------


def test_is_addressed_by_dm_or_mention():
    bot = SimpleNamespace(id=42)
    dm = SimpleNamespace(guild=None, mentions=[])
    pinged = SimpleNamespace(guild=object(), mentions=[bot])
    ignored = SimpleNamespace(guild=object(), mentions=[SimpleNamespace(id=7)])
    everyone = SimpleNamespace(guild=object(), mentions=[])
    assert chat.is_addressed(dm, 42)
    assert chat.is_addressed(pinged, 42)
    assert not chat.is_addressed(ignored, 42)
    assert not chat.is_addressed(everyone, 42)


def test_strip_mention_removes_both_mention_forms():
    assert chat.strip_mention("<@42> what's  RSTR at", 42) == "what's RSTR at"
    assert chat.strip_mention("hey <@!42>", 42) == "hey"
    assert chat.strip_mention("<@42>", 42) == ""


def test_chunk_message_respects_discord_limit():
    assert chat.chunk_message("") == []
    assert chat.chunk_message("short") == ["short"]
    long = "\n".join(f"line {i} " + "x" * 90 for i in range(60))
    pieces = chat.chunk_message(long, limit=2000)
    assert all(len(p) <= 2000 for p in pieces)
    assert "".join(p.replace("\n", "") for p in pieces) == long.replace("\n", "")
    assert all(not p.startswith("\n") and not p.endswith("\n") for p in pieces)
