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
    raw = (os.getenv(key) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


# ---- Discord ----
# Railway/Render templates historically set DISCORD_TOKEN while this bot read
# MCCAP_TOKEN. Accept either so a deploy can't boot tokenless over a name typo.
TOKEN = (os.getenv("MCCAP_TOKEN") or os.getenv("DISCORD_TOKEN") or "").strip()

# ---- Solana ----
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
DONATION_WALLET = os.getenv("DONATION_WALLET", "").strip()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

BALANCE_POLL_SECONDS = _env_int("BALANCE_POLL_SECONDS", 300)

# ---- Alert polling ----
# Every tracked token costs one DexScreener request per sweep. The documented
# limit on the token endpoint is 300 req/min, so a flat 3s sweep over 36 tokens
# (720 req/min) was getting rate-limited and silently dropping alerts.
# Instead we tier by how close a token is to its target: only the ones about to
# fire get fast polling.
POLL_TICK_SECONDS = _env_int("POLL_TICK_SECONDS", 5)  # scheduler wake-up cadence
POLL_HOT_SECONDS = _env_int("POLL_HOT_SECONDS", 10)  # within HOT_BAND of target
POLL_WARM_SECONDS = _env_int("POLL_WARM_SECONDS", 60)  # within WARM_BAND of target
POLL_COLD_SECONDS = _env_int("POLL_COLD_SECONDS", 300)  # far from target
POLL_UNKNOWN_SECONDS = _env_int("POLL_UNKNOWN_SECONDS", 120)  # no MC data yet

# "Distance" is current_mc/target_mc for `above` alerts (inverted for `below`),
# so 0.85 means the token is within 15% of firing.
HOT_BAND = float(os.getenv("HOT_BAND", "0.85"))
WARM_BAND = float(os.getenv("WARM_BAND", "0.5"))

# Client-side cap, kept under DexScreener's 300/min so we never get 429'd.
DEX_MAX_REQUESTS_PER_MIN = _env_int("DEX_MAX_REQUESTS_PER_MIN", 240)
# Instantaneous burst allowance. Sized so burst + sustained rate stays under the
# 300/min ceiling even in the worst rolling 60s window (50 + 240 = 290).
DEX_BURST = _env_int("DEX_BURST", 50)

DEX_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"
SOLANA_USE_FDV = True
DEX_BLACKLIST = {"heaven"}

# DexScreener 403s the default urllib/aiohttp user agent.
HTTP_USER_AGENT = os.getenv("HTTP_USER_AGENT", "McCapBot/2.0 (+https://github.com/azoni/McCap)")
HTTP_TIMEOUT_SECONDS = _env_int("HTTP_TIMEOUT_SECONDS", 12)

# ---- Payments ----
USDC_MINT = os.getenv("USDC_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
PAY_EXPIRY_SEC = _env_int("PAY_EXPIRY_SEC", 1800)
PAY_POLL_SEC = _env_int("PAY_POLL_SEC", 10)

# ---- Files ----
# Railway containers have an ephemeral filesystem: without a mounted volume every
# deploy wipes the alert list. Point DATA_DIR at the volume mount path.
DATA_DIR = Path(os.getenv("DATA_DIR", ".")).expanduser()

REM_FILE = str(DATA_DIR / "reminders.json")
PAY_FILE = str(DATA_DIR / "payments.json")
ALERTS_FILE = str(DATA_DIR / "alerts.json")

MAX_ALERT_EVENTS = _env_int("MAX_ALERT_EVENTS", 1000)

# ---- Display ----
MAX_ROWS_DEBUG = 10
