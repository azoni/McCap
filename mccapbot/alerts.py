import asyncio
import time
from typing import Dict, Optional

import discord

from . import gecko, history
from .cache import TOKEN_CACHE_LOCK, token_cache
from .config import MAX_ALERT_EVENTS, POLL_TICK_SECONDS
from .dex import build_token_url, choose_consensus_pair, fetch_dex_token, get_image_url, resolve_mc_value
from .helpers import human_window, humanize, meets, username_from_id
from .logging_setup import log
from .models import AlertEvent, TokenSnapshot
from .scheduler import due_addresses, estimated_requests_per_minute, move_ready, move_triggered
from .storage import (
    alert_events,
    move_alerts,
    reminders,
    save_alerts,
    save_moves,
    save_reminders,
    watched_addresses,
)

# ca -> monotonic timestamp of last successful refresh
_last_checked: Dict[str, float] = {}


async def _refresh(ca: str) -> None:
    """Fetch one token, update the cache, and append to its history series."""
    data = await fetch_dex_token(ca)

    mc_val: Optional[float] = None
    src, link, dex, chain, quote, consensus, img = "none", None, "", "", "", 0.0, ""

    if isinstance(data, dict) and data.get("pairs"):
        best, consensus, _ = choose_consensus_pair(data["pairs"], ca)
        if best:
            mc_val, src = resolve_mc_value(best, ca)
            link = build_token_url(ca, best)
            dex = best.get("dexId", "")
            chain = best.get("chainId", "")
            quote = ((best.get("quoteToken") or {}).get("symbol") or "").upper()
            img = get_image_url(best, ca) or ""

    now = time.time()
    async with TOKEN_CACHE_LOCK:
        token_cache[ca] = TokenSnapshot(
            mc=mc_val,
            url=(link or build_token_url(ca, None)),
            updated_ts=now,
            source=src,
            dex=dex,
            chain=chain,
            quote=quote,
            consensus=consensus,
            delta=(abs((mc_val or 0) - consensus) if mc_val and consensus else None),
            image_url=img,
        )
    history.record(ca, mc_val, now)


def _record_event(rem, current_mc, kind: str, direction: str, target: float) -> None:
    alert_events.insert(
        0,
        AlertEvent(
            ts=time.time(),
            ca=rem.ca,
            name=rem.name,
            symbol=rem.symbol,
            direction=direction,
            target_mc=target,
            current_mc=current_mc,
            channel_id=rem.channel_id,
            guild_id=rem.guild_id,
            creator_id=rem.creator_id,
            kind=kind,
        ),
    )
    del alert_events[MAX_ALERT_EVENTS:]


async def _fire_level(client: discord.Client, rem, current_mc, snap) -> None:
    ch = await client.fetch_channel(rem.channel_id)
    color = 0x2ECC71 if rem.direction == "above" else 0xE74C3C
    desc = (
        f"{'rose above' if rem.direction == 'above' else 'fell below'} "
        f"**${humanize(rem.target_mc)} MC**\nCurrent: **${humanize(current_mc)}**"
    )
    if rem.spec:
        desc += f"\nTarget was `{rem.spec}` from ${humanize(rem.anchor_mc)}"
    if rem.note:
        desc += f"\n\n📝 {rem.note}"

    user_name = await username_from_id(client, rem.creator_id)
    embed = discord.Embed(
        title=f"{rem.name} ({rem.symbol})",
        description=desc,
        url=(snap.url if snap else build_token_url(rem.ca, None)),
        color=color,
    )
    if snap and snap.image_url:
        embed.set_thumbnail(url=snap.image_url)
    embed.set_footer(text=f"Set by {user_name} • alert {rem.id}")

    await ch.send(
        content=f"<@{rem.creator_id}>",
        embed=embed,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False, replied_user=False),
    )
    _record_event(rem, current_mc, "level", rem.direction, rem.target_mc)
    log.info(
        "Alert fired | %s (%s) | dir=%s target=%s curr=%s id=%s",
        rem.name, rem.symbol, rem.direction, humanize(rem.target_mc), humanize(current_mc), rem.id,
    )


async def _fire_move(client: discord.Client, mv, change: float, current_mc, snap) -> None:
    ch = await client.fetch_channel(mv.channel_id)
    up = change >= 0
    arrow = "📈" if up else "📉"
    desc = (
        f"{arrow} **{change:+.1f}%** in the last {human_window(mv.window_sec)}\n"
        f"Current: **${humanize(current_mc)}**"
    )
    if mv.note:
        desc += f"\n\n📝 {mv.note}"

    user_name = await username_from_id(client, mv.creator_id)
    embed = discord.Embed(
        title=f"{mv.name} ({mv.symbol})",
        description=desc,
        url=(snap.url if snap else build_token_url(mv.ca, None)),
        color=0x2ECC71 if up else 0xE74C3C,
    )
    if snap and snap.image_url:
        embed.set_thumbnail(url=snap.image_url)
    embed.set_footer(
        text=f"Set by {user_name} • move {mv.id} • rearms in {human_window(mv.cooldown_sec)}"
    )

    await ch.send(
        content=f"<@{mv.creator_id}>",
        embed=embed,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False, replied_user=False),
    )
    _record_event(mv, current_mc, "move", "up" if up else "down", mv.pct)
    log.info(
        "Move fired | %s (%s) | %+.1f%% over %s id=%s",
        mv.name, mv.symbol, change, human_window(mv.window_sec), mv.id,
    )


async def _check_levels(client: discord.Client, snap_by_ca) -> bool:
    fired = []
    for rem in list(reminders):
        snap = snap_by_ca.get(rem.ca)
        current = snap.mc if snap else None
        if not meets(rem.direction, current, rem.target_mc):
            continue
        try:
            await _fire_level(client, rem, current, snap)
        except Exception:
            log.exception("Failed to send alert for %s (%s)", rem.name, rem.id)
        fired.append(rem)

    if not fired:
        return False
    # Remove by identity; list positions shift as alerts fire.
    fired_ids = {r.id for r in fired}
    reminders[:] = [r for r in reminders if r.id not in fired_ids]
    await save_reminders()
    return True


async def _check_moves(client: discord.Client, snap_by_ca) -> bool:
    now = time.time()
    changed = False
    for mv in list(move_alerts):
        if not move_ready(mv, now):
            continue
        change = history.pct_change(mv.ca, mv.window_sec, now)
        if not move_triggered(mv.direction, mv.pct, change):
            continue

        snap = snap_by_ca.get(mv.ca)
        try:
            await _fire_move(client, mv, change, snap.mc if snap else None, snap)
        except Exception:
            log.exception("Failed to send move alert for %s (%s)", mv.name, mv.id)
        mv.last_fired_ts = now
        changed = True

    if changed:
        await save_moves()
        await save_alerts()
    return changed


async def backfill_move_history() -> None:
    """Seed history for every momentum alert so a restart isn't blind.

    Without this a restarted bot ignores its momentum alerts for roughly half
    their window, because pct_change correctly refuses to answer until it has
    enough span. One GeckoTerminal call per token closes that gap.
    """
    if not move_alerts:
        return

    # Longest window per token — it covers every shorter one for free.
    want: Dict[str, int] = {}
    for m in move_alerts:
        if m.window_sec > want.get(m.ca, 0):
            want[m.ca] = m.window_sec

    # Needs a live market cap to scale candle prices against.
    await asyncio.gather(*(_refresh(ca) for ca in want), return_exceptions=True)

    async def one(ca: str, window: int):
        snap = token_cache.get(ca)
        if not snap or not snap.mc:
            return 0
        try:
            return await gecko.backfill(ca, window, snap.mc)
        except Exception:
            log.debug("Backfill failed for %s", ca, exc_info=True)
            return 0

    results = await asyncio.gather(*(one(ca, w) for ca, w in want.items()), return_exceptions=True)
    seeded = sum(r for r in results if isinstance(r, int))
    ready = sum(
        1 for m in move_alerts
        if history.pct_change(m.ca, m.window_sec, time.time()) is not None
    )
    log.info(
        "Seeded %d historical sample(s) across %d token(s); %d/%d momentum alert(s) armed immediately",
        seeded, len(want), ready, len(move_alerts),
    )


async def watcher(client: discord.Client) -> None:
    await client.wait_until_ready()
    last_rate_log = 0.0

    try:
        await backfill_move_history()
    except Exception:
        log.exception("Momentum history backfill failed; alerts will warm up normally")

    while not client.is_closed():
        try:
            addresses = watched_addresses()
            if addresses:
                async with TOKEN_CACHE_LOCK:
                    mc_by_ca = {ca: (token_cache[ca].mc if ca in token_cache else None) for ca in addresses}

                mono = time.monotonic()
                due = due_addresses(reminders, move_alerts, mc_by_ca, _last_checked, mono)

                if due:
                    results = await asyncio.gather(*(_refresh(ca) for ca in due), return_exceptions=True)
                    for ca, res in zip(due, results):
                        if isinstance(res, Exception):
                            log.debug("Refresh failed for %s: %s", ca, res)
                        # Stamp regardless so a persistently failing token does
                        # not get retried on every single tick.
                        _last_checked[ca] = mono

                async with TOKEN_CACHE_LOCK:
                    snap_by_ca = {ca: token_cache.get(ca) for ca in addresses}

                level_fired = await _check_levels(client, snap_by_ca)
                await _check_moves(client, snap_by_ca)

                if level_fired:
                    await save_alerts()
                    live = set(watched_addresses())
                    for ca in [c for c in _last_checked if c not in live]:
                        _last_checked.pop(ca, None)
                    history.forget(live)

                if mono - last_rate_log > 900:
                    rate = estimated_requests_per_minute(reminders, move_alerts, mc_by_ca)
                    log.info(
                        "Watching %d level + %d move alert(s) across %d token(s) — ~%.0f req/min",
                        len(reminders), len(move_alerts), len(addresses), rate,
                    )
                    last_rate_log = mono
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watcher loop error")

        await asyncio.sleep(POLL_TICK_SECONDS)
