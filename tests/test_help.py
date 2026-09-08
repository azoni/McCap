"""/help lists every registered command, built from the tree so it cannot drift."""

import pytest
import pytest_asyncio

from mccapbot.bot import Bot, EXTENSIONS
from mccapbot.cogs.help import build_help


@pytest_asyncio.fixture
async def tree():
    # Built inside the test loop. The bot is deliberately NOT closed: closing
    # unloads every extension, and discord.py purges unloaded cog modules from
    # sys.modules, which leaves other test modules holding stale module objects
    # (their monkeypatches then miss). Nothing was connected, so there is
    # nothing to release.
    bot = Bot()
    for ext in EXTENSIONS:
        await bot.load_extension(ext)
    yield bot.tree


@pytest.mark.asyncio
async def test_help_lists_every_leaf_command_with_its_description(tree):
    embed = build_help(tree)
    text = "\n".join(f"{f.name}\n{f.value}" for f in embed.fields)
    for leaf in ("/rh wallet create`", "/rh buy`", "/rh sell`", "/rh trending`", "/rh new`", "/rh holdings`",
                 "/rh history`", "/rh pnl`", "/rh stats`", "/rh tutorial`", "/rh auto sell`", "/rh auto buy`",
                 "/rh auto list`", "/rh auto cancel`", "`/mc`", "/mc_move`", "/mc_clear`", "/watch add`"):
        assert leaf in text, f"{leaf} missing from /help"
    assert "/help" not in text, "help does not list itself"
    assert "Robinhood Chain wallets & trading" in [f.name.split(" (cont.)")[0] for f in embed.fields]
    assert all(len(f.value) <= 1024 for f in embed.fields)
    assert "/rh tutorial" in (embed.footer.text or ""), "the help card points newcomers at the tutorial"


@pytest.mark.asyncio
async def test_rh_command_options_follow_the_visibility_rules(tree):
    """Personal views default private and expose ``public``; the rest expose ``private``."""
    rh = next(c for c in tree.get_commands() if c.name == "rh")
    leaves = {}

    def walk(cmd, prefix=""):
        name = f"{prefix}{cmd.name}"
        if hasattr(cmd, "commands"):
            for sub in cmd.commands:
                walk(sub, name + " ")
        else:
            leaves[name] = [p.name for p in cmd.parameters]
    walk(rh)
    for name in ("rh holdings", "rh history", "rh pnl", "rh tutorial", "rh auto list"):
        assert "public" in leaves[name] and "private" not in leaves[name], name
    for name in ("rh buy", "rh sell", "rh stats", "rh trending", "rh new", "rh auto sell", "rh auto buy"):
        assert "private" in leaves[name], name
    assert leaves["rh auto buy"][:4] == ["token", "usd", "condition", "value"]


@pytest.mark.asyncio
async def test_help_has_no_stale_commands(tree):
    text = "\n".join(f.value for f in build_help(tree).fields)
    for gone in ("/rh_buy", "/rh_trending", "/rh quote", "/pay", "/graduated_report"):
        assert gone not in text
