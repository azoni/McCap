"""McCap entrypoint.

Run with `python main.py`. The Railway/Docker start command points here.
"""

import sys

from mccapbot.bot import Bot
from mccapbot.config import TOKEN
from mccapbot.logging_setup import log, setup_logging


def main() -> int:
    setup_logging()
    if not TOKEN:
        log.error("No bot token found. Set MCCAP_TOKEN (or DISCORD_TOKEN) in the environment.")
        return 1
    Bot().run(TOKEN, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
