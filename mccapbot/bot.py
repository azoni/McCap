import asyncio
from typing import Optional

import discord
from discord.ext import commands

from .alerts import watcher as alerts_watcher
from .config import (
    DEX_BLACKLIST,
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
    load_reminders,
    load_scans,
    load_watchlist,
)

# Cogs loaded at startup. Each module exposes `async def setup(bot)`.
EXTENSIONS = (
    "mccapbot.cogs.alerts",
    "mccapbot.cogs.watch",
    "mccapbot.cogs.lp",
    "mccapbot.cogs.check",
    "mccapbot.cogs.trade",
    "mccapbot.cogs.scans",
    "mccapbot.cogs.chat",
    "mccapbot.cogs.rhc",
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
        # Cancel and await the loops BEFORE closing the shared aiohttp session.
        # Closing it first left in-flight requests running against a dead
        # session, and the scan tracker (owned by its cog, cancelled later by
        # cog_unload) outlived it entirely.
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
