import asyncio
from typing import Optional

import discord
from discord.ext import commands

from .alerts import watcher as alerts_watcher
from .config import (
    CHAT_ENABLE,
    DEX_BLACKLIST,
    FEED_ENABLE,
    LOG_LEVEL,
    PRESENCE_REFRESH_SECONDS,
    RHC_ABOUT_ME_ENABLE,
    SCAN_WATCH_ENABLE,
    SHOW_BALANCE,
    SOLANA_WALLET,
)
from .http import close_session
from .rhc import portfolio
from .solana import get_balance
from .logging_setup import log
from .storage import (
    load_alerts,
    load_chat_history,
    load_memory,
    load_moves,
    load_orders,
    load_reminders,
    load_scans,
    load_watchlist,
)

# Cogs loaded at startup. Each module exposes `async def setup(bot)`. Cogs whose
# feature is switched off are not loaded at all, so their slash commands are
# not registered: a command that can only say "this is disabled" is clutter.
EXTENSIONS = (
    "mccapbot.cogs.alerts",
    "mccapbot.cogs.watch",
    "mccapbot.cogs.lp",
    "mccapbot.cogs.check",
    "mccapbot.cogs.rhc",
    "mccapbot.cogs.help",
) + (("mccapbot.cogs.chat",) if CHAT_ENABLE else ()) + (("mccapbot.cogs.scans",) if SCAN_WATCH_ENABLE else ())


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
        self.auto_orders = None      # rhc.orders.Engine, started in setup_hook
        self.feed = None             # discovery.Feed, started in setup_hook
        self.tree.on_error = self._on_app_command_error

    async def _on_app_command_error(self, inter: discord.Interaction, error: Exception) -> None:
        """Never leave a slash command hanging. A stale client (Discord caches
        command definitions for up to an hour after a sync) sends option values
        the new code no longer accepts; say so instead of failing silently."""
        msg = "That command changed recently; try again in a moment or restart Discord."
        if not isinstance(error, discord.app_commands.TransformerError):
            log.exception("Slash command %s failed", getattr(inter.command, "qualified_name", "?"), exc_info=error)
            msg = "Something went wrong running that command; it's in the logs."
        try:
            if inter.response.is_done():
                await inter.followup.send(msg, ephemeral=True)
            else:
                await inter.response.send_message(msg, ephemeral=True)
        except Exception:
            log.debug("Could not report a slash command error", exc_info=True)

    # ---------------- background loops ----------------

    async def _presence_loop(self):
        """Status line: the bot's SOL balance, the Robinhood Chain wallets' total,
        and what it is tracking. The same summary goes into the bot's About Me
        so it shows when someone clicks McCap."""
        await self.wait_until_ready()
        last_balance: Optional[float] = None
        last_about: Optional[str] = None

        while not self.is_closed():
            if SHOW_BALANCE and SOLANA_WALLET:
                balance = await get_balance(SOLANA_WALLET)
                # None means the RPC call failed, which is not the same as an
                # empty wallet — keep the last known figure rather than
                # advertising 0.00 SOL because of a transient blip.
                if balance is not None:
                    last_balance = balance

            rh = None
            try:
                rh = await portfolio.summary()
            except Exception:
                log.exception("Could not summarise Robinhood Chain wallets")

            try:
                await self.change_presence(
                    status=discord.Status.online,
                    activity=discord.Activity(
                        type=discord.ActivityType.watching,
                        name=self._presence_text(last_balance, rh),
                    ),
                )
            except Exception:
                log.exception("Failed to update presence")

            if RHC_ABOUT_ME_ENABLE:
                text = portfolio.about_me(rh)
                if text != last_about and self.application is not None:
                    try:
                        await self.application.edit(description=text)
                        last_about = text
                    except Exception:
                        log.exception("Failed to update the About Me text")
            await asyncio.sleep(PRESENCE_REFRESH_SECONDS)

    @staticmethod
    def _presence_text(balance: Optional[float], rh=None) -> str:
        """Build the status line from whatever is actually known: the SOL
        balance and the Robinhood Chain wallets' total. Alert and token counts
        live in /mc_status, not here."""
        parts = []
        if balance is not None:
            parts.append(f"💰 {balance:,.2f} SOL")
        fragment = portfolio.presence_fragment(rh)
        if fragment:
            parts.append(fragment)
        return " · ".join(parts) if parts else "for /mc alerts"

    # ---------------- lifecycle ----------------

    async def setup_hook(self):
        await load_reminders()
        await load_moves()
        await load_watchlist()
        await load_alerts()
        await load_scans()
        await load_memory()
        await load_chat_history()
        await load_orders()

        if not CHAT_ENABLE:
            log.info("Chat is off (no ANTHROPIC_API_KEY); /memory not registered.")
        if not SCAN_WATCH_ENABLE:
            log.info("Scan watching is off (SCAN_WATCH_ENABLE=0); /scans not registered.")
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
        # The auto-order engine is owned here, not by the cog: test and CI
        # cog-load paths must never leave a task waiting on a gateway that
        # never connects, and an extension reload must not orphan a running
        # engine. Imported lazily for the same reason.
        from .rhc import orders
        self.auto_orders = orders.Engine(self)
        self._bg_tasks.append(asyncio.create_task(self.auto_orders.run(), name="auto-orders"))
        # The discovery feed and the tracker that grades its posts (and the
        # scanner's) live here for the same reasons.
        from . import discovery, tracker
        await discovery.load_feed()
        self.feed = discovery.Feed(self)
        if FEED_ENABLE:
            self._bg_tasks.append(asyncio.create_task(self.feed.run(), name="discovery-feed"))
        self._bg_tasks.append(asyncio.create_task(tracker.run(self), name="scan-tracker"))

        synced = await self.tree.sync()
        log.info("Synced %d global slash command(s)", len(synced))

    async def close(self):
        # Cancel and await the loops BEFORE closing the shared aiohttp session.
        # Closing it first left in-flight requests running against a dead
        # session, and the scan tracker (owned by its cog, cancelled later by
        # cog_unload) outlived it entirely. The engine goes first so an
        # in-flight auto-fill can finish journaling.
        if self.auto_orders is not None:
            try:
                await self.auto_orders.stop()
            except Exception:
                log.exception("Auto-order engine did not stop cleanly")
        for t in self._bg_tasks:
            t.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        await super().close()   # unloads cogs, so cog_unload cancels its tasks
        await close_session()

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
