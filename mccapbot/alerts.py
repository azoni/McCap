import asyncio
import time
from typing import Dict, Optional

import discord

from . import gecko, history, jupiter
from .cache import TOKEN_CACHE_LOCK, token_cache
from .config import (
    JUPITER_ENABLE,
    JUPITER_REFRESH_SECONDS,
    MAX_ALERT_EVENTS,
    POLL_TICK_SECONDS,
    TOP_HOLDER_WARN_PCT,
)
from .dex import build_token_url, choose_consensus_pair, fetch_dex_token, get_image_url, resolve_mc_value
from .helpers import human_window, humanize, meets, username_from_id
from .logging_setup import log
from .models import AlertEvent, TokenSnapshot
from .scheduler import (
    describe_tiers,
    due_addresses,
    estimated_requests_per_minute,
    move_ready,
    move_triggered,
)
from .storage import (
    alert_events,
    move_alerts,
    reminders,
    save_alerts,
    save_moves,
    save_reminders,
    watched_addresses,
)

# ca -> monotonic timestamp of last refresh attempt
_last_checked: Dict[str, float] = {}

# ca -> consecutive refreshes that returned no market cap. Dead memecoins never
# resolve, and POLL_UNKNOWN_SECONDS (120s) is *shorter* than POLL_COLD_SECONDS
# (300s), so without a backoff a token that can never fire was polled more often
# than a live one. The streak backs the interval off geometrically.
_no_data: Dict[str, int] = {}
NO_DATA_GIVE_UP = 5


# ca -> JupToken. Populated by a single batched sweep, not per-token, so the whole
# watchlist costs one request. Used for embed context and as a market-cap fallback
# for tokens DexScreener has stopped returning pairs for.
jup_cache: Dict[str, "jupiter.JupToken"] = {}


async def jupiter_sweep() -> int:
    """Refresh Jupiter data for every watched token in one batched request."""
    if not JUPITER_ENABLE:
        return 0
    addresses = watched_addresses()
    if not addresses:
        return 0
    try:
        fetched = await jupiter.fetch_many(addresses)
    except Exception:
        # Decoration must never break the watcher.
        log.debug("Jupiter sweep failed", exc_info=True)
        return 0
    jup_cache.update(fetched)
    live = set(addresses)
    for ca in [c for c in jup_cache if c not in live]:
        jup_cache.pop(ca, None)
    return len(fetched)


def jupiter_mc(ca: str) -> Optional[float]:
    """Jupiter's market cap for a token, if it has one."""
    tok = jup_cache.get(ca)
    return tok.mcap if tok and tok.mcap else None


def _note_data_state(ca: str, refresh_result) -> None:
    """Track whether a token is still returning usable data."""
    if isinstance(refresh_result, Exception) or refresh_result is False:
        return  # request failed; not evidence about the token itself
    snap = token_cache.get(ca)
    if snap is not None and snap.mc is not None:
        if _no_data.pop(ca, 0):
            log.info("Token %s is reporting a market cap again", ca)
    else:
        _no_data[ca] = _no_data.get(ca, 0) + 1
        if _no_data[ca] == NO_DATA_GIVE_UP:
            log.warning(
                "Token %s has returned no market data %d times running — backing off. "
                "Alerts on it cannot fire; remove them with /mc_remove.",
                ca, NO_DATA_GIVE_UP,
            )


async def expire_auto_alerts() -> bool:
    """Retire auto-armed momentum alerts past their TTL.

    This used to live only in the scan cog's tracker task, which never starts
    when SCAN_WATCH_ENABLE is off — so alerts armed while it was on became
    permanent, kept consuming request budget, and kept firing long past their
    TTL. It belongs on the always-running watcher instead.
    """
    now = time.time()
    expired = [m for m in move_alerts if m.auto_expires_ts and m.auto_expires_ts <= now]
    if not expired:
        return False
    ids = {m.id for m in expired}
    move_alerts[:] = [m for m in move_alerts if m.id not in ids]
    await save_moves()
    log.info("Expired %d auto-armed scan alert(s)", len(expired))
    return True


async def _collect(live: set) -> None:
    """Drop per-token state for addresses nobody watches any more."""
    for ca in [c for c in _last_checked if c not in live]:
        _last_checked.pop(ca, None)
    for ca in [c for c in _no_data if c not in live]:
        _no_data.pop(ca, None)
    # Keep anything the fired-alert history still displays, or /mc_recent's
    # Current column goes blank the moment an alert fires.
    keep = live | {e.ca for e in alert_events}
    async with TOKEN_CACHE_LOCK:
        for ca in [c for c in token_cache if c not in keep]:
            token_cache.pop(ca, None)
    history.forget(live)


async def _refresh(ca: str) -> bool:
    """Fetch one token and update the cache. Returns False if the request failed.

    A failed request must NOT be written to the cache. ``get_json`` flattens a
    429, a timeout, and any non-200 into ``None``, which is indistinguishable
    from "this token has no market data" — and overwriting a good snapshot with
    ``mc=None`` made /mc_list show "—" for a perfectly live token and demoted it
    to the slowest polling tier, precisely when it might have been about to fire.
    On failure the previous snapshot is left alone.
    """
    data = await fetch_dex_token(ca)
    if data is None:
        return False

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

    # DexScreener drops tokens whose pools thin out, which made a third of this
    # bot's alerts permanently unfireable. Jupiter still has a market cap for
    # most of them, so fall back rather than reporting "no data".
    if mc_val is None:
        fallback = jupiter_mc(ca)
        if fallback:
            mc_val, src = fallback, "jupiter"
            link = link or build_token_url(ca, None)

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
    return True


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


def _mention(user_id: int) -> Optional[str]:
    """Ping the owner, or nobody.

    An auto-armed alert may have no resolvable human behind it (creator_id 0).
    Formatting that anyway posts a literal, broken "<@0>" mention.
    """
    return f"<@{user_id}>" if user_id else None


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

    risk = jupiter.risk_line(jup_cache.get(rem.ca), TOP_HOLDER_WARN_PCT)
    if risk:
        desc += f"\n\n🔎 {risk}"

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
        content=_mention(rem.creator_id),
        embed=embed,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False, replied_user=False),
    )
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

    risk = jupiter.risk_line(jup_cache.get(mv.ca), TOP_HOLDER_WARN_PCT)
    if risk:
        desc += f"\n\n🔎 {risk}"

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
        content=_mention(mv.creator_id),
        embed=embed,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False, replied_user=False),
    )
    log.info(
        "Move fired | %s (%s) | %+.1f%% over %s id=%s",
        mv.name, mv.symbol, change, human_window(mv.window_sec), mv.id,
    )


async def _check_levels(client: discord.Client, snap_by_ca) -> bool:
    """Fire level alerts whose target has been reached.

    An alert is only consumed once it has actually been delivered, or once
    delivery is known to be impossible. Previously ``fired.append`` sat outside
    the try, so a deleted channel or a missing Send Messages permission threw,
    got swallowed by the log, and the alert was still deleted and persisted —
    while ``_record_event`` (which lived after the send) never ran. The alert
    vanished from both the active list and /mc_recent with no trace anywhere.
    """
    fired = []
    for rem in list(reminders):
        # /mc_remove can delete an alert while this loop awaits. Re-check
        # membership before sending, or a user who just removed one still gets
        # pinged and it still lands in /mc_recent.
        if not any(r.id == rem.id for r in reminders):
            continue
        snap = snap_by_ca.get(rem.ca)
        current = snap.mc if snap else None
        if not meets(rem.direction, current, rem.target_mc):
            continue
        try:
            await _fire_level(client, rem, current, snap)
        except (discord.NotFound, discord.Forbidden) as e:
            # The channel is gone or we can't post there — retrying every tick
            # would spin forever, so retire it, but keep the record.
            log.warning(
                "Alert %s (%s) hit its target but is undeliverable to channel %s (%s); "
                "retiring it and recording the fire.",
                rem.id, rem.name, rem.channel_id, e.__class__.__name__,
            )
        except Exception:
            # Transient (rate limit, gateway blip). Leave it armed and retry.
            log.exception("Deferring alert %s (%s): send failed", rem.id, rem.name)
            continue
        _record_event(rem, current, "level", rem.direction, rem.target_mc)
        fired.append(rem)

    if not fired:
        return False
    # Remove by identity; list positions shift as alerts fire.
    fired_ids = {r.id for r in fired}
    # History first: if the second save fails, the fire is still recorded rather
    # than the alert being gone with no trace of why.
    await save_alerts()
    reminders[:] = [r for r in reminders if r.id not in fired_ids]
    await save_reminders()
    return True


async def _check_moves(client: discord.Client, snap_by_ca) -> bool:
    now = time.time()
    changed = False
    for mv in list(move_alerts):
        if not any(m.id == mv.id for m in move_alerts):
            continue  # removed mid-tick
        if not move_ready(mv, now):
            continue
        change = history.pct_change(mv.ca, mv.window_sec, now)
        if not move_triggered(mv.direction, mv.pct, change):
            continue

        snap = snap_by_ca.get(mv.ca)
        try:
            await _fire_move(client, mv, change, snap.mc if snap else None, snap)
        except (discord.NotFound, discord.Forbidden) as e:
            # Undeliverable for good. Start the cooldown anyway so this doesn't
            # re-trigger on every tick, but log it loudly.
            log.warning(
                "Move alert %s (%s) triggered but is undeliverable to channel %s (%s).",
                mv.id, mv.name, mv.channel_id, e.__class__.__name__,
            )
        except Exception:
            # Transient: leave last_fired_ts alone so the trigger isn't
            # swallowed by a cooldown that never earned its notification.
            log.exception("Deferring move alert %s (%s): send failed", mv.id, mv.name)
            continue
        _record_event(mv, snap.mc if snap else None, "move", "up" if change >= 0 else "down", mv.pct)
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
    # Seeded from the clock, not 0: time.monotonic() is host uptime on Linux, so
    # a zero start made the first tick always overdue and logged the empty-cache
    # request rate — which understated real load roughly twofold.
    last_rate_log = time.monotonic()
    last_gc = last_rate_log
    last_jup = 0.0

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

                # One batched call covers every token, so this is cheap enough to
                # run on its own cadence independent of the per-token polling.
                if JUPITER_ENABLE and (mono - last_jup) > JUPITER_REFRESH_SECONDS:
                    got = await jupiter_sweep()
                    last_jup = mono
                    log.debug("Jupiter sweep resolved %d token(s)", got)

                due = due_addresses(reminders, move_alerts, mc_by_ca, _last_checked, mono, _no_data)

                if due:
                    results = await asyncio.gather(*(_refresh(ca) for ca in due), return_exceptions=True)
                    for ca, res in zip(due, results):
                        if isinstance(res, Exception):
                            log.debug("Refresh failed for %s: %s", ca, res)
                        # Stamp regardless so a persistently failing token does
                        # not get retried on every single tick.
                        _last_checked[ca] = mono
                        _note_data_state(ca, res)

                async with TOKEN_CACHE_LOCK:
                    snap_by_ca = {ca: token_cache.get(ca) for ca in addresses}

                level_fired = await _check_levels(client, snap_by_ca)
                await _check_moves(client, snap_by_ca)

                # Housekeeping runs on its own schedule, not only when a level
                # alert fires: /mc_remove and expiring auto-armed scan alerts
                # also stop a token being watched, and those paths never fired.
                if level_fired or (mono - last_gc) > 300:
                    await expire_auto_alerts()
                    await _collect(set(watched_addresses()))
                    last_gc = mono

                if mono - last_rate_log > 900:
                    async with TOKEN_CACHE_LOCK:
                        warm = {ca: (token_cache[ca].mc if ca in token_cache else None) for ca in addresses}
                    rate = estimated_requests_per_minute(reminders, move_alerts, warm, _no_data)
                    tiers = describe_tiers(reminders, warm)
                    # Report tokens currently returning nothing, not just those
                    # past the give-up threshold. The backoff slows how fast a
                    # streak accumulates, so thresholding here read as
                    # "0 with no market data" while 16 of 33 had none.
                    no_data_now = sum(1 for ca in addresses if _no_data.get(ca, 0) > 0)
                    backed_off = sum(1 for ca in addresses if _no_data.get(ca, 0) >= NO_DATA_GIVE_UP)
                    log.info(
                        "Watching %d level + %d move alert(s) across %d token(s) — ~%.0f req/min "
                        "| tiers %s | %d token(s) reporting no market cap (%d fully backed off)",
                        len(reminders), len(move_alerts), len(addresses), rate,
                        tiers, no_data_now, backed_off,
                    )
                    last_rate_log = mono
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watcher loop error")

        await asyncio.sleep(POLL_TICK_SECONDS)
