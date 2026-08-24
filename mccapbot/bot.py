import asyncio

import discord
from discord.ext import commands

from .alerts import watcher as alerts_watcher
from .config import DEX_BLACKLIST, LOG_LEVEL, PRESENCE_REFRESH_SECONDS, SCAN_WATCH_ENABLE
from .http import close_session
from .logging_setup import log
from .storage import (
    load_alerts,
    load_moves,
    load_reminders,
    load_scans,
    load_watchlist,
    move_alerts,
    reminders,
    watched_addresses,
)

# Cogs loaded at startup. Each module exposes `async def setup(bot)`.
EXTENSIONS = (
    "mccapbot.cogs.alerts",
    "mccapbot.cogs.watch",
    "mccapbot.cogs.lp",
    "mccapbot.cogs.scans",
)


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # Slash commands need no message content. It is required only to read
        # OTHER bots' scan embeds (see cogs/scans.py) and is a privileged
        # intent, so it stays opt-in: the code requesting it is not enough,
        # the Developer Portal toggle has to be on too or the gateway refuses
        # the connection.
        intents.message_content = SCAN_WATCH_ENABLE
        super().__init__(command_prefix="!", intents=intents)
        self._bg_tasks: list[asyncio.Task] = []

    # ---------------- background loops ----------------

    async def _presence_loop(self):
        """Show what the bot is actually tracking.

        This used to display a donation wallet's SOL balance, which told nobody
        anything useful now that payments are gone.
        """
        await self.wait_until_ready()
        while not self.is_closed():
            total = len(reminders) + len(move_alerts)
            tokens = len(watched_addresses())
            name = f"{total} alert(s) · {tokens} token(s)" if total else "for /mc alerts"
            try:
                await self.change_presence(
                    status=discord.Status.online,
                    activity=discord.Activity(type=discord.ActivityType.watching, name=name),
                )
            except Exception:
                log.exception("Failed to update presence")
            await asyncio.sleep(PRESENCE_REFRESH_SECONDS)

    # ---------------- lifecycle ----------------

    async def setup_hook(self):
        await load_reminders()
        await load_moves()
        await load_watchlist()
        await load_alerts()
        await load_scans()

        for ext in EXTENSIONS:
            try:
                await self.load_extension(ext)
                log.info("Loaded extension %s", ext)
            except Exception:
                # One broken cog shouldn't take the whole bot down.
                log.exception("Failed to load extension %s", ext)

        self._bg_tasks = [
            asyncio.create_task(self._presence_loop(), name="presence"),
            asyncio.create_task(alerts_watcher(self), name="alerts-watcher"),
        ]

        synced = await self.tree.sync()
        log.info("Synced %d global slash command(s)", len(synced))

    async def close(self):
        for t in self._bg_tasks:
            t.cancel()
        await close_session()
        await super().close()

    async def on_ready(self):
        guilds = ", ".join(f"{g.name}({g.id})" for g in self.guilds) or "none"
        log.info(
            "Logged in as %s (ID %s) | Guilds: [%s] | LOG_LEVEL=%s | DEX_BLACKLIST=%s",
            self.user, self.user.id, guilds, LOG_LEVEL, sorted(DEX_BLACKLIST),
        )

    async def on_guild_join(self, guild: discord.Guild):
        log.info("Joined guild %s (%s); using global application commands only.", guild.name, guild.id)

    # ---------------- owner-only text helpers ----------------

    @commands.command(name="sync")
    @commands.is_owner()
    async def sync_global(self, ctx: commands.Context):
        synced = await self.tree.sync()
        await ctx.send(f"🔄 Synced {len(synced)} global slash commands.")
