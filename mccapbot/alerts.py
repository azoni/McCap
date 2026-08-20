import asyncio
import time
from typing import Dict, Optional

import discord

from .cache import TOKEN_CACHE_LOCK, token_cache
from .config import MAX_ALERT_EVENTS, POLL_TICK_SECONDS
from .dex import build_token_url, choose_consensus_pair, fetch_dex_token, get_image_url, resolve_mc_value
from .helpers import humanize, meets, username_from_id
from .logging_setup import log
from .models import AlertEvent, TokenSnapshot
from .scheduler import due_addresses, estimated_requests_per_minute
from .storage import alert_events, reminders, save_alerts, save_reminders

# ca -> monotonic timestamp of last successful refresh
_last_checked: Dict[str, float] = {}


async def _refresh(ca: str) -> None:
    """Fetch one token and write the result into the shared cache."""
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

    async with TOKEN_CACHE_LOCK:
        token_cache[ca] = TokenSnapshot(
            mc=mc_val,
            url=(link or build_token_url(ca, None)),
            updated_ts=time.time(),
            source=src,
            dex=dex,
            chain=chain,
            quote=quote,
            consensus=consensus,
            delta=(abs((mc_val or 0) - consensus) if mc_val and consensus else None),
            image_url=img,
        )


async def _fire(client: discord.Client, rem, current_mc: Optional[float], snap: Optional[TokenSnapshot]) -> None:
    """Post the alert embed and record the event."""
    ch = await client.fetch_channel(rem.channel_id)
    url = snap.url if snap else build_token_url(rem.ca, None)
    color = 0x2ECC71 if rem.direction == "above" else 0xE74C3C
    title = f"{rem.name} ({rem.symbol})"
    desc = (
        f"{'rose above' if rem.direction == 'above' else 'fell below'} "
        f"**${humanize(rem.target_mc)} MC**\nCurrent: **${humanize(current_mc)}**"
    )
    if rem.note:
        desc += f"\n\n📝 {rem.note}"

    user_name = await username_from_id(client, rem.creator_id)
    embed = discord.Embed(title=title, description=desc, url=url, color=color)
    if snap and snap.image_url:
        embed.set_thumbnail(url=snap.image_url)
    embed.set_footer(text=f"Set by {user_name} • alert {rem.id}")

    await ch.send(
        content=f"<@{rem.creator_id}>",
        embed=embed,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False, replied_user=False),
    )

    alert_events.insert(
        0,
        AlertEvent(
            ts=time.time(),
            ca=rem.ca,
            name=rem.name,
            symbol=rem.symbol,
            direction=rem.direction,
            target_mc=rem.target_mc,
            current_mc=current_mc,
            channel_id=rem.channel_id,
            guild_id=rem.guild_id,
            creator_id=rem.creator_id,
        ),
    )
    del alert_events[MAX_ALERT_EVENTS:]
    await save_alerts()
    log.info(
        "Alert fired | %s | dir=%s target=%s curr=%s id=%s",
        title,
        rem.direction,
        humanize(rem.target_mc),
        humanize(current_mc),
        rem.id,
    )


async def watcher(client: discord.Client) -> None:
    await client.wait_until_ready()
    last_rate_log = 0.0

    while not client.is_closed():
        try:
            if reminders:
                async with TOKEN_CACHE_LOCK:
                    mc_by_ca = {r.ca: (token_cache[r.ca].mc if r.ca in token_cache else None) for r in reminders}

                now = time.monotonic()
                due = due_addresses(reminders, mc_by_ca, _last_checked, now)

                if due:
                    results = await asyncio.gather(*(_refresh(ca) for ca in due), return_exceptions=True)
                    for ca, res in zip(due, results):
                        if isinstance(res, Exception):
                            log.debug("Refresh failed for %s: %s", ca, res)
                        # Stamp regardless so a persistently failing token does not
                        # get retried on every single tick.
                        _last_checked[ca] = now

                # Re-read the cache after refreshing so we compare against fresh data.
                async with TOKEN_CACHE_LOCK:
                    snap_by_ca = {r.ca: token_cache.get(r.ca) for r in reminders}

                fired = []
                for rem in list(reminders):
                    snap = snap_by_ca.get(rem.ca)
                    current = snap.mc if snap else None
                    if not meets(rem.direction, current, rem.target_mc):
                        continue
                    try:
                        await _fire(client, rem, current, snap)
                    except Exception:
                        log.exception("Failed to send alert for %s (%s)", rem.name, rem.id)
                    fired.append(rem)

                if fired:
                    # Remove by identity; list positions shift as alerts fire.
                    fired_ids = {r.id for r in fired}
                    reminders[:] = [r for r in reminders if r.id not in fired_ids]
                    await save_reminders()
                    # Drop schedule entries for tokens nobody watches anymore.
                    live = {r.ca for r in reminders}
                    for ca in [c for c in _last_checked if c not in live]:
                        _last_checked.pop(ca, None)

                if now - last_rate_log > 900:
                    rate = estimated_requests_per_minute(reminders, mc_by_ca)
                    log.info(
                        "Watching %d alert(s) across %d token(s) — ~%.0f req/min",
                        len(reminders),
                        len({r.ca for r in reminders}),
                        rate,
                    )
                    last_rate_log = now
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watcher loop error")

        await asyncio.sleep(POLL_TICK_SECONDS)
