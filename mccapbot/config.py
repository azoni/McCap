import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_int(key: str, default: int) -> int:
    """Read an int env var, falling back to the default if it is unset or junk."""
    raw = (os.getenv(key) or "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _env_flag(key: str, default: bool = False) -> bool:
    """Read a boolean env var. Anything falsy-looking turns the feature off."""
    raw = (os.getenv(key) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


# ---- Discord ----
# Railway/Render templates historically set DISCORD_TOKEN while this bot read
# MCCAP_TOKEN. Accept either so a deploy can't boot tokenless over a name typo.
TOKEN = (os.getenv("MCCAP_TOKEN") or os.getenv("DISCORD_TOKEN") or "").strip()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
PRESENCE_REFRESH_SECONDS = _env_int("PRESENCE_REFRESH_SECONDS", 300)

# ---- Alert polling ----
# Every tracked token costs one DexScreener request per sweep. The documented
# limit on the token endpoint is 300 req/min, so a flat 3s sweep over 36 tokens
# (720 req/min) was getting rate-limited and silently dropping alerts.
# Instead we tier by how close a token is to its target: only the ones about to
# fire get fast polling.
POLL_TICK_SECONDS = _env_int("POLL_TICK_SECONDS", 5)
POLL_HOT_SECONDS = _env_int("POLL_HOT_SECONDS", 10)
POLL_WARM_SECONDS = _env_int("POLL_WARM_SECONDS", 60)
POLL_COLD_SECONDS = _env_int("POLL_COLD_SECONDS", 300)
POLL_UNKNOWN_SECONDS = _env_int("POLL_UNKNOWN_SECONDS", 120)

# "Distance" is current_mc/target_mc for `above` alerts (inverted for `below`),
# so 0.85 means the token is within 15% of firing.
HOT_BAND = float(os.getenv("HOT_BAND", "0.85"))
WARM_BAND = float(os.getenv("WARM_BAND", "0.5"))

# ---- Momentum alerts ----
# A move alert needs enough samples inside its window to measure a change, so
# its token is sampled at window/DIVISOR (floored at MOVE_MIN_SECONDS).
MOVE_SAMPLE_DIVISOR = _env_int("MOVE_SAMPLE_DIVISOR", 12)
MOVE_MIN_SECONDS = _env_int("MOVE_MIN_SECONDS", 30)
MOVE_DEFAULT_COOLDOWN = _env_int("MOVE_DEFAULT_COOLDOWN", 1800)
# Cap on retained samples per token, so history can't grow without bound.
HISTORY_MAX_SAMPLES = _env_int("HISTORY_MAX_SAMPLES", 500)

# Client-side cap, kept under DexScreener's 300/min so we never get 429'd.
DEX_MAX_REQUESTS_PER_MIN = _env_int("DEX_MAX_REQUESTS_PER_MIN", 240)
# Instantaneous burst allowance. Sized so burst + sustained rate stays under the
# 300/min ceiling even in the worst rolling 60s window (50 + 240 = 290).
DEX_BURST = _env_int("DEX_BURST", 50)

DEX_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"
SOLANA_USE_FDV = True
DEX_BLACKLIST = {"heaven"}

# DexScreener 403s the default urllib/aiohttp user agent.
HTTP_USER_AGENT = os.getenv("HTTP_USER_AGENT", "McCapBot/3.0 (+https://github.com/azoni/McCap)")
HTTP_TIMEOUT_SECONDS = _env_int("HTTP_TIMEOUT_SECONDS", 12)

# ---- Files ----
# Railway containers have an ephemeral filesystem: without a mounted volume every
# deploy wipes the alert list. Point DATA_DIR at the volume mount path.
DATA_DIR = Path(os.getenv("DATA_DIR", ".")).expanduser()

REM_FILE = str(DATA_DIR / "reminders.json")
ALERTS_FILE = str(DATA_DIR / "alerts.json")
MOVES_FILE = str(DATA_DIR / "moves.json")
WATCH_FILE = str(DATA_DIR / "watchlists.json")

MAX_ALERT_EVENTS = _env_int("MAX_ALERT_EVENTS", 1000)
MAX_WATCH_PER_LIST = _env_int("MAX_WATCH_PER_LIST", 25)

# ---- Scan watching (Rick and other scanner bots) ----
# Reading other bots' scan messages needs the MESSAGE_CONTENT privileged intent,
# which is a Developer Portal toggle (no Discord approval required). Without it
# embeds/components arrive empty and detection is inert, so the listener logs a
# loud warning at startup rather than failing silently.
# Defaults OFF on purpose. Requesting a privileged intent the Developer Portal
# has not granted does not degrade gracefully — Discord closes the gateway with
# 4014 and the bot cannot log in at all. So the portal toggle goes on FIRST,
# then SCAN_WATCH_ENABLE=1. main.py also retries without the intent if this is
# set while the toggle is still off, so a mis-set flag can't brick the bot.
SCAN_WATCH_ENABLE = _env_flag("SCAN_WATCH_ENABLE", False)

# Bot user ids whose messages are treated as scans. Empty means "any bot except
# ourselves" — convenient, but see SCAN_IGNORE_SELF: reacting to our own posts
# would be an infinite loop.
SCANNER_BOT_IDS = {
    int(x) for x in (os.getenv("SCANNER_BOT_IDS") or "").replace(",", " ").split() if x.isdigit()
}
# Optional channel allowlist. Empty means every channel the bot can see.
SCAN_CHANNEL_IDS = {
    int(x) for x in (os.getenv("SCAN_CHANNEL_IDS") or "").replace(",", " ").split() if x.isdigit()
}

# Don't record the same token twice in this window (repeat scans are constant).
SCAN_DEDUPE_SECONDS = _env_int("SCAN_DEDUPE_SECONDS", 900)

# --- what to do on detection ---
SCAN_AUTO_WATCHLIST = _env_flag("SCAN_AUTO_WATCHLIST", True)
SCAN_AUTO_WATCHLIST_NAME = os.getenv("SCAN_AUTO_WATCHLIST_NAME", "scans")
SCAN_POST_OPINION = _env_flag("SCAN_POST_OPINION", True)
# Per-channel floor between second-opinion replies, so a scan flood can't make
# McCap the noisiest bot in the room.
SCAN_OPINION_MIN_SECONDS = _env_int("SCAN_OPINION_MIN_SECONDS", 60)

# Auto-arm a momentum alert on each newly scanned token. Every armed alert costs
# polling, so this is capped: past the cap new scans are recorded and watchlisted
# but not armed.
SCAN_AUTO_MOVE_PCT = float(os.getenv("SCAN_AUTO_MOVE_PCT", "30"))
SCAN_AUTO_MOVE_WINDOW = _env_int("SCAN_AUTO_MOVE_WINDOW", 3600)
SCAN_AUTO_MOVE_MAX = _env_int("SCAN_AUTO_MOVE_MAX", 15)
# Auto-armed alerts expire; a token scanned once shouldn't be polled forever.
SCAN_AUTO_MOVE_TTL = _env_int("SCAN_AUTO_MOVE_TTL", 86400)

# --- performance tracking ---
SCANS_FILE = str(DATA_DIR / "scans.json")
MAX_SCAN_EVENTS = _env_int("MAX_SCAN_EVENTS", 2000)
# How long a scanned token keeps being re-checked to find its peak.
SCAN_TRACK_HOURS = _env_int("SCAN_TRACK_HOURS", 48)
SCAN_TRACK_INTERVAL = _env_int("SCAN_TRACK_INTERVAL", 300)
