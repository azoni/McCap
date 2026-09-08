"""Talk to McCap.

@mention the bot, or DM it, and it answers through the Claude API with two
kinds of memory, both stored on the data volume next to the alerts:

* a rolling per-channel conversation (``CHAT_HISTORY_TURNS`` turns), so a
  redeploy does not lose the thread of a discussion; and
* long-term notes per server: things people told it to remember, ideas to
  implement, decisions. It reads them on every request and edits them through
  the ``remember`` / ``forget`` tools.

It can also read the live alert list and look tokens up, so "what are we
tracking" and "what's X at" get real numbers. It does not create alerts: an
alert fires later into a channel, and that path is built around the slash
command interaction that armed it, so the bot points people at ``/mc`` instead.
"""

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import anthropic

from .cache import TOKEN_CACHE_LOCK, token_cache
from .config import (
    ANTHROPIC_API_KEY,
    CHAT_DAILY_CAP,
    CHAT_HISTORY_TURNS,
    CHAT_MAX_NOTES,
    CHAT_MAX_TOKENS,
    CHAT_MAX_TOOL_ROUNDS,
    CHAT_MODEL,
    CHAT_TIMEOUT,
    CHAT_USER_COOLDOWN_SECONDS,
    DEX_SEARCH_URL,
)
from .dex import token_summary
from .helpers import humanize, is_solana_address, pct, plural, short_ca
from .http import dex_limiter, get_json
from .logging_setup import log
from .models import ChatTurn, MemoryNote
from .storage import (
    chat_turns,
    memory_notes,
    move_alerts,
    reminders,
    save_chat_history,
    save_memory,
)

DISCORD_MESSAGE_LIMIT = 2000


@dataclass
class ChatContext:
    """Who is talking, and where."""

    channel_id: int
    user_id: int
    user_name: str
    guild_id: int = 0          # 0 in a DM
    guild_name: str = ""

    @property
    def scope(self) -> str:
        return scope_key(self.guild_id, self.user_id)


def scope_key(guild_id: Optional[int], user_id: int) -> str:
    """Notes are shared per server and private per DM."""
    return f"g{guild_id}" if guild_id else f"u{user_id}"


# ---------------- memory ----------------

def notes_for(scope: str) -> List[MemoryNote]:
    return sorted((n for n in memory_notes if n.scope == scope), key=lambda n: n.created_ts)


async def remember(scope: str, text: str, author_id: int, author_name: str) -> MemoryNote:
    """Save a note; when the scope is full the oldest one goes."""
    note = MemoryNote(scope=scope, text=text.strip(), author_id=author_id, author_name=author_name)
    memory_notes.append(note)
    mine = notes_for(scope)
    if len(mine) > CHAT_MAX_NOTES:
        drop = {n.id for n in mine[: len(mine) - CHAT_MAX_NOTES]}
        memory_notes[:] = [n for n in memory_notes if n.id not in drop]
    await save_memory()
    return note


async def forget(scope: str, note_id: str) -> Optional[MemoryNote]:
    """Delete a note by id, only if it belongs to this scope."""
    for n in memory_notes:
        if n.id == note_id and n.scope == scope:
            memory_notes.remove(n)
            await save_memory()
            return n
    return None


def describe_notes(scope: str) -> str:
    notes = notes_for(scope)
    if not notes:
        return "(nothing saved yet)"
    return "\n".join(
        f"[{n.id}] {n.author_name}, {time.strftime('%Y-%m-%d', time.gmtime(n.created_ts))}: {n.text}"
        for n in notes
    )


# ---------------- conversation history ----------------

def history_for(channel_id: int) -> List[ChatTurn]:
    return [t for t in chat_turns if t.channel_id == channel_id]


def _trim_history(channel_id: int) -> None:
    mine = history_for(channel_id)
    if len(mine) <= CHAT_HISTORY_TURNS:
        return
    drop = {id(t) for t in mine[: len(mine) - CHAT_HISTORY_TURNS]}
    chat_turns[:] = [t for t in chat_turns if id(t) not in drop]


async def record_exchange(channel_id: int, user_name: str, user_text: str, reply: str) -> None:
    """Persist one question/answer pair, keeping the channel's window bounded."""
    chat_turns.append(ChatTurn(channel_id=channel_id, role="user", content=user_text, author_name=user_name))
    chat_turns.append(ChatTurn(channel_id=channel_id, role="assistant", content=reply))
    _trim_history(channel_id)
    await save_chat_history()


def build_messages(channel_id: int, user_name: str, user_text: str) -> List[Dict[str, Any]]:
    """History plus the new message, as alternating API turns.

    Several people can talk in one channel, so user turns carry the speaker's
    name. Consecutive same-role turns are merged and a leading assistant turn
    is dropped: the API wants a user message first.
    """
    turns = history_for(channel_id) + [
        ChatTurn(channel_id=channel_id, role="user", content=user_text, author_name=user_name)
    ]
    messages: List[Dict[str, Any]] = []
    for t in turns:
        text = f"{t.author_name}: {t.content}" if t.role == "user" and t.author_name else t.content
        if messages and messages[-1]["role"] == t.role:
            messages[-1]["content"] += "\n" + text
        else:
            messages.append({"role": t.role, "content": text})
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


# ---------------- prompt ----------------

def build_system(ctx: ChatContext) -> str:
    where = f'the Discord server "{ctx.guild_name}"' if ctx.guild_id else f"a DM with {ctx.user_name}"
    unit = "server" if ctx.guild_id else "DM"
    return f"""You are McCap, a Discord bot that watches memecoin market caps (Solana and EVM chains) and posts alerts when they cross targets. You are chatting in {where}. People reach you by @mentioning you or in a DM.

How to behave:
- Reply like a teammate in chat: short and plain, Discord markdown, no headers. A sentence or two is usually right; go longer only when asked for detail.
- You have persistent memory. When someone tells you to remember something, states a fact worth keeping, describes something they want built or changed, or makes a decision, save it with the `remember` tool: one note per item, concrete, with the names and numbers they gave. Confirm in a few words what you saved. Use `forget` for a note that is wrong or done; when a note is superseded, forget the old one and remember the new one.
- When asked what you remember, what is planned, or what is on the to-do list, answer from the notes below. Do not invent items.
- For a live price or market cap use `token_lookup`; for what is being tracked use `list_alerts`. Never guess a market cap.
- You cannot create or remove alerts. Point people to the slash commands: /mc <address> <target> for a level alert (targets like 5M, or relative like 2x or +50%), /mc_move for a momentum alert, /mc_list, /mc_remove, /mc_recent, /mc_check for holder and risk context, /mc_lp for pool venues, /watch for watchlists.
- Note ids look like `a1b2c3`. Write amounts like $1.2M or $850K.

Notes for this {unit} (id, who, when, note):
{describe_notes(ctx.scope)}

Today is {time.strftime('%Y-%m-%d', time.gmtime())} (UTC)."""


# ---------------- tools ----------------

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "remember",
        "description": (
            "Save a note to long-term memory for this server (or this DM). Use for facts, "
            "preferences, decisions, and things people want built or changed. One note per item."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The note, concrete and self-contained."}},
            "required": ["text"],
        },
    },
    {
        "name": "forget",
        "description": "Delete a note from long-term memory by its id (shown in brackets in the notes list).",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "list_alerts",
        "description": "The active market-cap alerts visible here, with each token's latest market cap.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "token_lookup",
        "description": (
            "Live market data for a token from DexScreener: name, market cap, 24h change, liquidity, "
            "volume. Pass a contract address / mint for an exact answer, or a symbol or name to search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Contract address, mint, symbol, or name."}},
            "required": ["query"],
        },
    },
]


def _visible_alerts(ctx: ChatContext):
    """Same scoping rule as /mc_list: the server's alerts, or the user's own in a DM."""
    if ctx.guild_id:
        return (
            [r for r in reminders if r.guild_id == ctx.guild_id],
            [m for m in move_alerts if m.guild_id == ctx.guild_id],
        )
    return (
        [r for r in reminders if r.creator_id == ctx.user_id],
        [m for m in move_alerts if m.creator_id == ctx.user_id],
    )


async def _tool_list_alerts(ctx: ChatContext) -> str:
    levels, moves = _visible_alerts(ctx)
    if not levels and not moves:
        return "No active alerts here."
    async with TOKEN_CACHE_LOCK:
        snaps = {x.ca: token_cache.get(x.ca) for x in (*levels, *moves)}

    def now_str(ca: str) -> str:
        snap = snaps.get(ca)
        return f"now ${humanize(snap.mc)}" if snap and snap.mc is not None else "no market data yet"

    lines = []
    for r in levels:
        sign = ">=" if r.direction == "above" else "<="
        note = f" ({r.note})" if r.note else ""
        lines.append(
            f"[{r.id}] {r.symbol or r.name} {sign} ${humanize(r.target_mc)}, {now_str(r.ca)}, "
            f"{short_ca(r.ca)}{note}"
        )
    for m in moves:
        lines.append(
            f"[{m.id}] {m.symbol or m.name} moves {pct(m.pct, signed=False)} {m.direction} within {m.window_sec // 60}m, "
            f"{now_str(m.ca)}, {short_ca(m.ca)}"
        )
    return "\n".join(lines)


def _looks_like_address(q: str) -> bool:
    q = q.strip()
    return is_solana_address(q) or (q.startswith("0x") and len(q) == 42)


def _describe_summary(s: Dict[str, Any]) -> str:
    return (
        f"{s['name']} ({s['symbol']}): MC ${humanize(s['mc'])}, 24h {pct(s['change24'])}, "
        f"liquidity ${humanize(s['liq'])}, 24h volume ${humanize(s['vol24'])}, {plural(int(s['pools'] or 0), 'pool')}. "
        f"Address {s['ca']}."
    )


async def _tool_token_lookup(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "Empty query."
    if _looks_like_address(q):
        summary = await token_summary(q)
        return _describe_summary(summary) if summary else f"DexScreener has no pools for {short_ca(q)}."

    data = await get_json(DEX_SEARCH_URL.format(query=quote(q)), limiter=dex_limiter)
    pairs = (data or {}).get("pairs") or []
    if not pairs:
        return f"No DexScreener results for {q!r}."
    # One line per distinct token, deepest pool first, so a symbol shared by
    # several tokens shows the real one at the top instead of a copycat.
    seen = set()
    out = []
    for p in sorted(pairs, key=lambda p: -float((p.get("liquidity") or {}).get("usd") or 0)):
        base = p.get("baseToken") or {}
        key = (p.get("chainId"), base.get("address"))
        if key in seen:
            continue
        seen.add(key)
        mc = p.get("marketCap") or p.get("fdv")
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        out.append(
            f"{base.get('name')} ({base.get('symbol')}) on {p.get('chainId')}: "
            f"MC ${humanize(float(mc)) if mc else '-'}, liquidity ${humanize(liq)}, address {base.get('address')}"
        )
        if len(out) >= 5:
            break
    return "Matches (deepest pool first; pass an address for exact figures):\n" + "\n".join(out)


async def run_tool(name: str, inputs: Dict[str, Any], ctx: ChatContext) -> str:
    """Execute one tool call. Always returns text; errors become text too."""
    try:
        if name == "remember":
            text = str(inputs.get("text") or "").strip()
            if not text:
                return "Nothing to save."
            note = await remember(ctx.scope, text, ctx.user_id, ctx.user_name)
            return f"Saved as [{note.id}]."
        if name == "forget":
            note = await forget(ctx.scope, str(inputs.get("id") or "").strip())
            return f"Forgot [{note.id}]: {note.text}" if note else "No note with that id here."
        if name == "list_alerts":
            return await _tool_list_alerts(ctx)
        if name == "token_lookup":
            return await _tool_token_lookup(str(inputs.get("query") or ""))
        return f"Unknown tool {name}."
    except Exception:
        log.exception("Chat tool %s failed", name)
        return f"The {name} tool failed; say so briefly."


# ---------------- spend guard ----------------

class SpendGuard:
    """Per-user cooldown plus a daily ceiling on paid calls.

    In-memory on purpose: a redeploy resetting the count is fine, a spam loop
    quietly running all night is not.
    """

    def __init__(self, daily_cap: int = CHAT_DAILY_CAP, cooldown: float = CHAT_USER_COOLDOWN_SECONDS):
        self.daily_cap = daily_cap
        self.cooldown = cooldown
        self._day = ""
        self._count = 0
        self._last_by_user: Dict[int, float] = {}

    def _roll(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != self._day:
            self._day, self._count = day, 0

    def check(self, user_id: int, now: float) -> Optional[str]:
        """Reason the call should not happen, or None."""
        self._roll(now)
        if self._count >= self.daily_cap:
            return "I've hit my daily chat budget; back tomorrow (UTC)."
        last = self._last_by_user.get(user_id, 0.0)
        if self.cooldown > 0 and now - last < self.cooldown:
            return "One at a time, give me a second."
        return None

    def note_call(self, user_id: int, now: float) -> None:
        self._roll(now)
        self._count += 1
        self._last_by_user[user_id] = now

    @property
    def calls_today(self) -> int:
        return self._count


# ---------------- the model call ----------------

_client: Optional[anthropic.AsyncAnthropic] = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, timeout=CHAT_TIMEOUT, max_retries=2)
    return _client


def _text_of(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text").strip()


async def respond(ctx: ChatContext, user_text: str, api=None) -> str:
    """Answer one message, running tool calls as needed, and record the exchange.

    ``api`` is the Anthropic client to use; tests pass a fake. Returns the reply
    text. On an API failure that is a short human explanation, never an
    exception.
    """
    api = api or client()
    system = build_system(ctx)
    messages = build_messages(ctx.channel_id, ctx.user_name, user_text)

    try:
        response = None
        for _round in range(CHAT_MAX_TOOL_ROUNDS + 1):
            response = await api.messages.create(
                model=CHAT_MODEL,
                max_tokens=CHAT_MAX_TOKENS,
                system=system,
                tools=TOOLS,
                messages=messages,
            )
            if response.stop_reason != "tool_use":
                break
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    out = await run_tool(block.name, dict(block.input or {}), ctx)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
            messages.append({"role": "user", "content": results})
    except anthropic.AuthenticationError:
        log.error("Anthropic rejected the API key; chat is effectively off.")
        return "My API key was rejected. Someone needs to check ANTHROPIC_API_KEY."
    except anthropic.RateLimitError:
        return "Claude is rate-limiting me right now. Try again in a minute."
    except anthropic.APIStatusError as e:
        log.warning("Chat API error %s: %s", e.status_code, e.message)
        return "The model call failed on Anthropic's side. Try again shortly."
    except anthropic.APIConnectionError:
        log.warning("Chat could not reach the Anthropic API", exc_info=True)
        return "I couldn't reach the model API. Try again shortly."

    if response is None:
        return "Something went wrong before I could answer."
    if response.stop_reason == "refusal":
        reply = "I can't help with that one."
    elif response.stop_reason == "tool_use":
        # Ran out of tool rounds while the model still wanted more lookups.
        reply = _text_of(response) or "I got stuck in a loop of lookups; ask again more specifically."
    else:
        reply = _text_of(response) or "(I ran out of things to say; try rephrasing.)"
        if response.stop_reason == "max_tokens":
            reply += " ..."

    usage = getattr(response, "usage", None)
    if usage is not None:
        log.info(
            "Chat reply in %s for %s: %s in / %s out tokens (%s)",
            ctx.scope, ctx.user_name, usage.input_tokens, usage.output_tokens, CHAT_MODEL,
        )
    await record_exchange(ctx.channel_id, ctx.user_name, user_text, reply)
    return reply


# ---------------- Discord plumbing ----------------

def is_addressed(message, bot_user_id: int) -> bool:
    """A DM, or an @mention of the bot (reply pings count; @everyone does not)."""
    if message.guild is None:
        return True
    return any(getattr(u, "id", None) == bot_user_id for u in (message.mentions or []))


def strip_mention(content: str, bot_user_id: int) -> str:
    """Remove the bot's own mention tokens from the message text."""
    text = (content or "").replace(f"<@{bot_user_id}>", "").replace(f"<@!{bot_user_id}>", "")
    return " ".join(text.split())


def chunk_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> List[str]:
    """Split a reply so every piece fits a Discord message, preferring line breaks."""
    text = text.strip()
    if not text:
        return []
    chunks: List[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    chunks.append(text)
    return chunks
