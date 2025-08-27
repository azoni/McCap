# graduation_watcher.py
# Requires: discord.py (2.x), gql[websockets], python-dateutil, pytz
#
# Env:
# - DISCORD_TOKEN                (for your bot process, not used directly here)
# - GRADUATION_CHANNEL_ID        e.g. 1315787669980319757
# - BITQUERY_TOKEN               your Bitquery API token (Streaming enabled)
# - OPTIONAL FILTERING:
#     LB_ONLY_SUCCESS_TX=true    (default true) — ignores failed txs
#     LB_PROGRAM_ID=LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj (default)
#
# What it does:
# - Opens a GraphQL WebSocket subscription to Bitquery streaming
# - Watches LaunchLab methods: migrate_to_amm, migrate_to_cpswap
# - Extracts token mint and tx signature, posts to a Discord channel
# - Times are formatted in America/Los_Angeles with am/pm

import asyncio
import logging
import os
from typing import Optional, List

import discord
from discord.ext import commands

from gql import Client, gql
from gql.transport.websockets import WebsocketsTransport

from datetime import datetime
from dateutil import tz

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

LAUNCHLAB_PROGRAM_DEFAULT = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
GRAD_METHODS = ("migrate_to_amm", "migrate_to_cpswap")

SUBSCRIPTION = """
subscription LaunchLabGraduations($program: String!, $successOnly: Boolean!) {
  Solana {
    Instructions(
      where: {
        Instruction: {
          Program: {
            Address: { is: $program }
            Method: { in: ["migrate_to_amm","migrate_to_cpswap"] }
          }
        }
        Transaction: { Result: { Success: true } }
      }
    ) {
      Block { Time }
      Transaction { Signature }
      Instruction {
        Program { Method }
        Accounts {
          Address
          Token { Mint }
        }
      }
    }
  }
}
"""

PT = tz.gettz("America/Los_Angeles")

def _first_mint(accounts: List[dict]) -> Optional[str]:
    # Find first account entry that includes a token mint
    for a in accounts or []:
        t = a.get("Token")
        if t and t.get("Mint"):
            return t["Mint"]
    return None

def _fmt_pt(ts_iso: str) -> str:
    # Example ts_iso: "2025-08-26T18:01:23Z"
    dt_utc = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    dt_pt = dt_utc.astimezone(PT)
    return dt_pt.strftime("%b %d, %Y • %I:%M:%S %p %Z")  # with am/pm

class GraduationWatcher(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task = None
        self._stop = asyncio.Event()

        self.bitquery_token = os.getenv("BITQUERY_TOKEN", "").strip()
        if not self.bitquery_token:
            log.warning("BITQUERY_TOKEN not set — watcher will not start.")

        self.channel_id = int(os.getenv("GRADUATION_CHANNEL_ID", "0"))
        if not self.channel_id:
            log.warning("GRADUATION_CHANNEL_ID not set — watcher will not start.")

        self.program_id = os.getenv("LB_PROGRAM_ID", LAUNCHLAB_PROGRAM_DEFAULT).strip()
        self.success_only = os.getenv("LB_ONLY_SUCCESS_TX", "true").lower() != "false"

    # ------------- lifecycle -------------

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._task and self.bitquery_token and self.channel_id:
            self._task = asyncio.create_task(self._run_loop(), name="graduation-watcher")
            log.info("GraduationWatcher task started.")

    def cog_unload(self):
        if self._task:
            self._stop.set()
            self._task.cancel()

    # ------------- core loop -------------

    async def _run_loop(self):
        # Exponential backoff reconnect loop
        backoff = 1
        while not self._stop.is_set():
            try:
                await self._subscribe_and_forward()
                backoff = 1  # reset on clean exit
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("Graduation watcher error: %s", e)
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 60)

    async def _subscribe_and_forward(self):
        # Connect WebSocket to Bitquery streaming GraphQL
        url = f"wss://streaming.bitquery.io/graphql?token={self.bitquery_token}"
        transport = WebsocketsTransport(
            url=url,
            # gql v3 uses graphql-transport-ws automatically; protocol header optional
        )
        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            # Try fetch if not cached
            channel = await self.bot.fetch_channel(self.channel_id)

        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel, discord.StageChannel)):
            log.warning("Channel %s not a text-capable channel.", self.channel_id)

        async with Client(transport=transport, fetch_schema_from_transport=False) as session:
            query = gql(SUBSCRIPTION)
            variables = {"program": self.program_id, "successOnly": self.success_only}
            log.info("Subscribing to LaunchLab graduations for program %s", self.program_id)

            async for ev in session.subscribe(query, variable_values=variables):
                rows = ev.get("Solana", {}).get("Instructions", [])
                if not rows:
                    continue
                row = rows[0]

                block_time = row["Block"]["Time"]  # ISO
                signature = row["Transaction"]["Signature"]
                method = row["Instruction"]["Program"]["Method"]
                accounts = row["Instruction"]["Accounts"]
                mint = _first_mint(accounts) or "unknown"

                # Build links
                sig_url = f"https://solscan.io/tx/{signature}"
                mint_url = f"https://solscan.io/token/{mint}" if mint != "unknown" else None

                ts = _fmt_pt(block_time)

                # Format Discord message
                title_emoji = "🎓"
                raydium_kind = "AMM v4" if method == "migrate_to_amm" else "CPMM"
                header = f"{title_emoji} **Graduated to Raydium ({raydium_kind})**"

                lines = [
                    header,
                    f"• **Time (PT):** {ts}",
                    f"• **Method:** `{method}`",
                    f"• **Mint:** {f'`{mint}`' if not mint_url else f'[`{mint}`]({mint_url})'}",
                    f"• **Tx:** [{signature}]({sig_url})",
                    "",
                    "Next checks: watch first pool/trade on Raydium to confirm liquidity."
                ]

                try:
                    if hasattr(channel, "send"):
                        await channel.send("\n".join(lines))
                    else:
                        log.warning("Cannot send to channel type: %s", type(channel))
                except Exception as e:
                    log.exception("Failed to send Discord message: %s", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(GraduationWatcher(bot))
