import logging
from .config import LOG_LEVEL

class Color:
    RESET="\x1b[0m"; GRAY="\x1b[90m"; GREEN="\x1b[32m"; YELLOW="\x1b[33m"; RED="\x1b[31m"
    BLUE="\x1b[34m"; CYAN="\x1b[36m"; BOLD="\x1b[1m"

class ColorFormatter(logging.Formatter):
    COLORS={"DEBUG":Color.BLUE,"INFO":Color.GREEN,"WARNING":Color.YELLOW,"ERROR":Color.RED,"CRITICAL":Color.RED+Color.BOLD}
    def format(self, rec):
        lvl=f"{self.COLORS.get(rec.levelname,'')}{rec.levelname}{Color.RESET}"
        t=f"{Color.GRAY}{self.formatTime(rec, '%H:%M:%S')}{Color.RESET}"
        name=f"{Color.CYAN}{rec.name}{Color.RESET}"
        return f"{t} | {lvl} | {name} | {super().format(rec)}"

def setup_logging():
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    h = logging.StreamHandler()
    h.setFormatter(ColorFormatter("%(message)s"))
    root.handlers[:] = [h]
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.INFO)
    logging.getLogger("discord.gateway").setLevel(logging.INFO)

log = logging.getLogger("mc-bot")
