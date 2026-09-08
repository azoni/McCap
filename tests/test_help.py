"""/help lists every registered command, built from the tree so it cannot drift."""

import asyncio

import pytest

from mccapbot.bot import Bot, EXTENSIONS
from mccapbot.cogs.help import build_help


@pytest.fixture(scope="module")
def tree():
    async def load():
        bot = Bot()
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
        return bot
    bot = asyncio.run(load())
    yield bot.tree
    asyncio.run(bot.close())


def test_help_lists_every_leaf_command_with_its_description(tree):
    embed = build_help(tree)
    text = "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)
    for leaf in ("/rhc wallet create`", "/rhc buy`", "/rhc sell`", "/rhc trending`", "/rhc new`", "`/mc`", "/mc_move`", "/watch add`"):
        assert leaf in text, f"{leaf} missing from /help"
    assert "/help" not in text, "help does not list itself"
    assert "Robinhood Chain wallets & trading" in [f.name for f in embed.fields]
    assert all(len(f.value) <= 1024 for f in embed.fields)


def test_help_has_no_stale_commands(tree):
    text = "\n".join(f.value for f in build_help(tree).fields)
    for gone in ("/rh_buy", "/rh_trending", "/rhc quote", "/pay", "/graduated_report"):
        assert gone not in text
