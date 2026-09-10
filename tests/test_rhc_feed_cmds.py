"""/rh feed on|off|status|mute and the richer boards, driven through the cog with fakes."""

import os
from types import SimpleNamespace

import pytest
from discord import app_commands

from mccapbot import discovery, rhchain
from mccapbot.cogs import rhc as cog
from tests.rhc_fakes import PONS, USER, FakeInteraction, arm_world, disarm_world


class Manager(FakeInteraction):
    def __init__(self, user_id=USER, guild_id=1, manager=True):
        super().__init__(user_id=user_id, guild_id=guild_id)
        self.user.guild_permissions = SimpleNamespace(manage_guild=manager, administrator=False)


@pytest.fixture
def feed_state(monkeypatch):
    w = arm_world(monkeypatch)
    discovery.configs.clear()
    try:
        os.remove(discovery.storage.FEED_FILE)
    except FileNotFoundError:
        pass
    yield w
    discovery.configs.clear()
    disarm_world()


async def feed_on(inter, **kw):
    await cog.RhcCog.feed_on.callback(cog.RhcCog(bot=None), inter, **kw)


@pytest.mark.asyncio
async def test_feed_on_is_manager_only_and_writes_the_config(feed_state):
    plain = Manager(manager=False)
    await feed_on(plain)
    assert plain.last.startswith("🔒") and discovery.config_for(1) is None

    dm = Manager(guild_id=None)
    await feed_on(dm)
    assert "server" in dm.last and discovery.config_for(1) is None

    m = Manager()
    await feed_on(m, min_liquidity=8000, movers=False, max_per_hour=5)
    cfg = discovery.config_for(1)
    assert cfg is not None and cfg.channel_id == 99 and cfg.enabled and cfg.min_liq == 8000.0 and not cfg.movers
    assert cfg.max_per_hour == 5 and cfg.new_pairs and cfg.spikes
    assert m.last.startswith("📡 Feed → <#99>") and "new pairs, spikes" in m.last and "liq ≥ $8K" in m.last
    assert os.path.exists(discovery.storage.FEED_FILE), "the setting is on disk"

    again = Manager()
    cfg.muted["0xabc"] = 9e9
    await feed_on(again, pace=4.0)
    cfg2 = discovery.config_for(1)
    assert cfg2.pace == 4.0 and cfg2.muted == {"0xabc": 9e9}, "re-running keeps the mutes"
    assert list(discovery.configs) == [1], "one config per server, replaced not appended"


@pytest.mark.asyncio
async def test_feed_off_and_status(feed_state):
    off = Manager()
    await cog.RhcCog.feed_off.callback(cog.RhcCog(bot=None), off)
    assert "not on" in off.last
    await feed_on(Manager())
    await cog.RhcCog.feed_off.callback(cog.RhcCog(bot=None), off)
    assert "off" in off.last and not discovery.config_for(1).enabled

    # The feed writes its own status text; the command just delivers it, and
    # still answers when the feed task never started (bot without one).
    st = Manager()
    await cog.RhcCog.feed_status.callback(cog.RhcCog(bot=None), st)
    text = st.last
    assert "Feed → <#99>" in text and "**off**" in text
    assert "Kinds: new pairs, spikes, movers" in text and "liq ≥ $5K" in text and "buyers ≥ 8" in text
    assert "Last hour: **0** posts (cap 10)" in text and "Last 24h: 0 posts" in text

    running = Manager()
    engine = discovery.Feed(None)
    engine.paused = True
    await cog.RhcCog.feed_status.callback(cog.RhcCog(bot=SimpleNamespace(feed=engine)), running)
    assert "paused (GeckoTerminal busy)" in running.last

    nobody = Manager(guild_id=2)
    await cog.RhcCog.feed_status.callback(cog.RhcCog(bot=None), nobody)
    assert "No feed in this server" in nobody.last


@pytest.mark.asyncio
async def test_feed_mute_resolves_the_token_and_stores_an_expiry(feed_state):
    await feed_on(Manager())
    m = Manager()
    await cog.RhcCog.feed_mute.callback(cog.RhcCog(bot=None), m, PONS, app_commands.Choice(name="6 hours", value="6h"))
    cfg = discovery.config_for(1)
    assert PONS.lower() in cfg.muted and "🔇" in m.last and "PONS" in m.last and "<t:" in m.last


# ---------------- boards ----------------


def pool(addr, sym, base, vol, mc, buyers=0, m5vol=0.0, quote="WETH", created=1.0):
    return rhchain.Pool(address=addr, name=f"{sym} / {quote}", dex="Uniswap V3", base_symbol=sym, base_name=sym,
                        base_address=base, quote_symbol=quote, price_usd=1, liq_usd=1e6, mc_usd=mc,
                        volume={"h24": vol, "h1": vol / 10, "m5": m5vol}, change={"h24": 1.0, "h1": 2.0, "m5": 3.0},
                        buys_h24=0, sells_h24=0, created_ts=created,
                        tx={"m5": {"buys": buyers, "sells": 1, "buyers": buyers, "sellers": 1},
                            "h24": {"buys": buyers * 3, "sells": 2, "buyers": buyers * 2, "sellers": 2}})


@pytest.mark.asyncio
async def test_board_merges_trending_ranks_active_and_tags_pace(feed_state, monkeypatch):
    other = "0x" + "2" * 40
    fresh = "0x" + "3" * 40

    async def tops():
        return [pool("0xa", "PONS", PONS, 100e6, 500e6, buyers=4, m5vol=100e6 / 10 / 12 * 3),  # 3x the hour's pace
                pool("0xb", "MEME", other, 30e6, 90e6, buyers=40)]

    async def trending(duration):
        assert duration == "5m"
        return [pool("0xb", "MEME", other, 30e6, 90e6, buyers=40),           # duplicate pool: not double-counted
                pool("0xc", "FRESH", fresh, 1e6, 2e6, buyers=9)]
    monkeypatch.setattr(rhchain, "top_pools", tops)
    monkeypatch.setattr(rhchain, "trending_pools", trending)
    monkeypatch.setattr(rhchain, "last_error", None, raising=False)

    c = cog.RhcCog(bot=None)
    embed, top, problem = await c._board("m5", "volume", 10, False)
    assert problem is None and [t.symbol for t in top][:3] == ["PONS", "MEME", "FRESH"]
    table = embed.fields[0].value
    assert "⚡3.0x" in table and "Buyers" in table and "FRESH" in table
    meme = next(t for t in top if t.symbol == "MEME")
    assert meme.volume("h24") == 30e6, "a pool present in both lists counts once"

    embed, top, _ = await c._board("m5", "active", 10, False)
    assert [t.symbol for t in top][:2] == ["MEME", "FRESH"] and "most buyers in 5m" in embed.title

    embed, top, _ = await c._board("h24", "gainers", 10, False)
    assert "buys/sells" in embed.description and "buyer" in embed.description


@pytest.mark.asyncio
async def test_new_board_filters_on_buyers_and_marks_non_major_quotes(feed_state, monkeypatch):
    async def news():
        return [pool("0xd", "DUST", "0x" + "4" * 40, 1e3, 5e3, buyers=1, created=100.0),
                pool("0xe", "HOT", "0x" + "5" * 40, 5e4, 2e5, buyers=12, quote="NVDA", created=200.0)]
    monkeypatch.setattr(rhchain, "new_pools", news)
    monkeypatch.setattr(rhchain, "last_error", None, raising=False)
    inter = FakeInteraction()
    await cog.RhcCog.new.callback(cog.RhcCog(bot=None), inter, count=10, min_liquidity=1000, min_buyers=5)
    embed = inter.last_kw["embed"]
    table = embed.fields[0].value
    assert "HOT (NVDA)" in table and "DUST" not in table and "Buyers 5m" in table
    assert "≥ 5 buyers" in embed.description
