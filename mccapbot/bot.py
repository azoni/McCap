import asyncio

import discord
from discord.ext import commands

from .alerts import watcher as alerts_watcher
from .config import BALANCE_POLL_SECONDS, DEX_BLACKLIST, DONATION_WALLET, LOG_LEVEL
from .http import close_session
from .logging_setup import log
from .payments import payments_watcher
from .solana import get_solana_balance
from .storage import load_alerts, load_invoices, load_reminders

# Cogs loaded at startup. Each module exposes `async def setup(bot)`.
EXTENSIONS = (
    "mccapbot.cogs.alerts",
    "mccapbot.cogs.lp",
    "mccapbot.cogs.payments",
    "mccapbot.cogs.graduated",
)


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # Slash commands don't need message content; leaving it off avoids
        # requiring a privileged intent in the developer portal.
        intents.message_content = False
        super().__init__(command_prefix="!", intents=intents)
        self._bg_tasks: list[asyncio.Task] = []

    # ---------------- background loops ----------------

    async def _wallet_presence_loop(self):
        await self.wait_until_ready()
        if not DONATION_WALLET:
            log.info("No valid DONATION_WALLET set; presence loop disabled.")
            return
        while not self.is_closed():
            bal = await get_solana_balance(DONATION_WALLET)
            activity = discord.Activity(
                type=discord.ActivityType.watching,
                name=(f"💰 {bal:,.2f} SOL" if bal is not None else "💰 fetching SOL…"),
            )
            try:
                await self.change_presence(status=discord.Status.online, activity=activity)
            except Exception:
                log.exception("Failed to update presence")
            await asyncio.sleep(BALANCE_POLL_SECONDS)

    # ---------------- lifecycle ----------------

    async def setup_hook(self):
        await load_reminders()
        await load_invoices()
        await load_alerts()

        for ext in EXTENSIONS:
            try:
                await self.load_extension(ext)
                log.info("Loaded extension %s", ext)
            except Exception:
                # One broken cog shouldn't take the whole bot down.
                log.exception("Failed to load extension %s", ext)

        self._bg_tasks = [
            asyncio.create_task(self._wallet_presence_loop(), name="wallet-presence"),
            asyncio.create_task(alerts_watcher(self), name="alerts-watcher"),
            asyncio.create_task(payments_watcher(self), name="payments-watcher"),
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
            self.user,
            self.user.id,
            guilds,
            LOG_LEVEL,
            sorted(DEX_BLACKLIST),
        )

    async def on_guild_join(self, guild: discord.Guild):
        log.info("Joined guild %s (%s); using global application commands only.", guild.name, guild.id)

    # ---------------- owner-only text helpers ----------------

    @commands.command(name="sync")
    @commands.is_owner()
    async def sync_global(self, ctx: commands.Context):
        synced = await self.tree.sync()
        await ctx.send(f"🔄 Synced {len(synced)} global slash commands.")

    @commands.command(name="sync_here")
    @commands.is_owner()
    async def sync_guild(self, ctx: commands.Context):
        synced = await self.tree.sync(guild=ctx.guild)
        await ctx.send(f"🔄 Synced {len(synced)} commands to this guild.")
