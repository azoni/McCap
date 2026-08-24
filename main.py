"""McCap entrypoint.

Run with `python main.py`. The Railway/Docker start command points here.
"""

import sys

import discord

from mccapbot.bot import Bot
from mccapbot.config import TOKEN
from mccapbot.logging_setup import log, setup_logging


def main() -> int:
    setup_logging()
    if not TOKEN:
        log.error("No bot token found. Set MCCAP_TOKEN (or DISCORD_TOKEN) in the environment.")
        return 1

    try:
        Bot().run(TOKEN, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        # SCAN_WATCH_ENABLE asked for MESSAGE_CONTENT but the Developer Portal
        # has not granted it, so Discord refused the connection outright (4014).
        # Staying offline over an optional feature is the wrong trade: drop the
        # intent, say so clearly, and run without scan detection.
        log.error(
            "Discord refused the MESSAGE_CONTENT intent, so scan detection cannot run. "
            "Enable it under Bot > Privileged Gateway Intents in the Developer Portal "
            "(no approval needed), or set SCAN_WATCH_ENABLE=0. "
            "Starting WITHOUT scan detection so the alerts keep working."
        )
        bot = Bot()
        bot.intents.message_content = False
        bot.run(TOKEN, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
