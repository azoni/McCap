import asyncio, time
from .storage import reminders, save_reminders, alert_events, save_alerts
from .cache import token_cache, TOKEN_CACHE_LOCK
from .dex import fetch_dex_token, choose_consensus_pair, resolve_mc_value, build_token_url, get_image_url
from .helpers import meets, humanize
from .logging_setup import log
import discord

from .config import POLL_SECONDS

async def watcher(client: discord.Client):
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            unique_cas = list({r.ca for r in reminders})
            if unique_cas:
                results = await asyncio.gather(*[fetch_dex_token(ca) for ca in unique_cas], return_exceptions=True)
                async with TOKEN_CACHE_LOCK:
                    now = asyncio.get_running_loop().time()
                    for ca, data in zip(unique_cas, results):
                        mc_val, src, link, dex, chain, quote, cons, img = None, "none", None, "", "", "", 0.0, ""
                        if isinstance(data, dict) and data.get("pairs"):
                            best, consensus, _ = choose_consensus_pair(data["pairs"], ca)
                            cons = consensus
                            if best:
                                mc_val, src = resolve_mc_value(best, ca)
                                link = build_token_url(ca, best)
                                dex = best.get("dexId",""); chain = best.get("chainId","")
                                quote = ((best.get("quoteToken") or {}).get("symbol") or "").upper()
                                img = get_image_url(best, ca) or ""
                        token_cache[ca] = token_cache.get(ca) or None  # keep type
                        token_cache[ca] = type(token_cache.get(ca) or object)("TokenSnapshot") if False else None  # no-op to keep mypy quiet
                        # assign directly (same as original behavior)
                        from .models import TokenSnapshot
                        token_cache[ca] = TokenSnapshot(
                            mc=mc_val, url=(link or build_token_url(ca, None)),
                            updated_ts=now, source=src, dex=dex, chain=chain, quote=quote,
                            consensus=cons, delta=(abs((mc_val or 0)-cons) if mc_val and cons else None),
                            image_url=img
                        )

            async with TOKEN_CACHE_LOCK:
                snap = {r.ca: token_cache.get(r.ca) for r in reminders}

            for rem in reminders[:]:
                s = snap.get(rem.ca); curr = s.mc if s else None
                if meets(rem.direction, curr, rem.target_mc):
                    try:
                        ch = await client.fetch_channel(rem.channel_id)
                        url = (s.url if s else build_token_url(rem.ca, None))
                        color = 0x2ecc71 if rem.direction == "above" else 0xe74c3c
                        title = f"{rem.name} ({rem.symbol})"
                        desc  = f"{'rose above' if rem.direction=='above' else 'fell below'} **${humanize(rem.target_mc)} MC**\nCurrent: **${humanize(curr)}**"
                        if rem.note:
                            desc += f"\n\n📝 {rem.note}"
                        from .helpers import username_from_id
                        user_name = await username_from_id(client, rem.creator_id)

                        embed = discord.Embed(title=title, description=desc, url=url, color=color)
                        if s and s.image_url: embed.set_thumbnail(url=s.image_url)
                        embed.set_footer(text=f"Set by {user_name}")
                        await ch.send(content=f"<@{rem.creator_id}>",
                                      embed=embed,
                                      allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False, replied_user=False))

                        # record alert event
                        alert_events.insert(0, __build_event(rem, curr))
                        if len(alert_events) > 1000: alert_events.pop()
                        await save_alerts()
                        log.info(f"Alert fired for {title} | dir={rem.direction} target={humanize(rem.target_mc)} curr={humanize(curr)}")
                    except Exception:
                        import logging; logging.exception("Failed to send alert message")
                    reminders.remove(rem); await save_reminders()
        except Exception:
            import logging; logging.exception("watcher loop error")
        await asyncio.sleep(POLL_SECONDS)

def __build_event(rem, curr_mc):
    from .models import AlertEvent
    return AlertEvent(
        ts=time.time(), ca=rem.ca, name=rem.name, symbol=rem.symbol,
        direction=rem.direction, target_mc=rem.target_mc, current_mc=curr_mc,
        channel_id=rem.channel_id, guild_id=rem.guild_id, creator_id=rem.creator_id
    )
