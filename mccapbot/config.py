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
DEX_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={query}"
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
    int(x) for x in (os.getenv("SCANNER_BOT_IDS") or "").replace(",", " ").split()
    if x.isascii() and x.isdigit()
}
# Optional channel allowlist. Empty means every channel the bot can see.
SCAN_CHANNEL_IDS = {
    int(x) for x in (os.getenv("SCAN_CHANNEL_IDS") or "").replace(",", " ").split()
    if x.isascii() and x.isdigit()
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

# ---- Wallet presence ----
# The bot's SOL balance in its Discord status line. DONATION_WALLET is accepted
# as an alias because that is the name already provisioned on Railway from the
# original Solana Pay setup.
SOLANA_WALLET = (os.getenv("SOLANA_WALLET") or os.getenv("DONATION_WALLET") or "").strip()
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com").strip()
SHOW_BALANCE = _env_flag("SHOW_BALANCE", True)

# ---- Jupiter (holder / risk enrichment + market-cap fallback) ----
# Free and keyless, and it batches: up to 100 mints in one request, so the whole
# watchlist is a single call rather than one per token. That also makes it a
# viable fallback for tokens DexScreener has stopped returning pairs for —
# measured 10 of 12 such tokens still have a market cap here.
JUPITER_ENABLE = _env_flag("JUPITER_ENABLE", True)
JUPITER_URL = os.getenv("JUPITER_URL", "https://lite-api.jup.ag/tokens/v2/search")
JUPITER_BATCH = _env_int("JUPITER_BATCH", 100)
# One sweep per interval covers every watched token, so this can be generous.
JUPITER_REFRESH_SECONDS = _env_int("JUPITER_REFRESH_SECONDS", 90)
JUPITER_TIMEOUT = _env_int("JUPITER_TIMEOUT", 10)
# Jupiter publishes no rate-limit headers and no documented keyless quota, so
# stay well clear of anything that could look abusive.
JUPITER_MAX_REQUESTS_PER_MIN = _env_int("JUPITER_MAX_REQUESTS_PER_MIN", 30)

# Flag a token in alert embeds when the top 10 wallets hold more than this.
TOP_HOLDER_WARN_PCT = float(os.getenv("TOP_HOLDER_WARN_PCT", "50"))

# ---- Robinhood chain (DEX activity for /rh trending and /rh new) ----
# Robinhood's own API is execution-only and has no chain data at all, and
# DexScreener has no per-chain listing. GeckoTerminal lists a network's pools
# sorted by 24h volume, 20 per page, keyless.
RHCHAIN_NETWORK = os.getenv("RHCHAIN_NETWORK", "robinhood").strip()
RHCHAIN_PAGES = _env_int("RHCHAIN_PAGES", 2)
RHCHAIN_CACHE_SECONDS = _env_int("RHCHAIN_CACHE_SECONDS", 60)

# ---- Chat (talk to McCap by @mentioning it, or in a DM) ----
# Answers come from the Claude API. Off until a key is set; a mention then gets
# a one-line hint so a missing key is visible rather than silently ignored.
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
CHAT_ENABLE = _env_flag("CHAT_ENABLE", True) and bool(ANTHROPIC_API_KEY)
# Haiku is the cheap tier; every @mention is a paid call in a shared server.
CHAT_MODEL = os.getenv("CHAT_MODEL", "claude-haiku-4-5").strip()
CHAT_MAX_TOKENS = _env_int("CHAT_MAX_TOKENS", 1024)
CHAT_TIMEOUT = _env_int("CHAT_TIMEOUT", 60)
# Rolling conversation kept per channel, on the volume, so a redeploy does not
# lose the thread of a discussion.
CHAT_HISTORY_TURNS = _env_int("CHAT_HISTORY_TURNS", 30)
# Long-term notes per server, all of them injected into every request.
CHAT_MAX_NOTES = _env_int("CHAT_MAX_NOTES", 200)
# Spend guards. In-memory, so they reset on redeploy — good enough to stop a
# spam loop from running up the bill overnight.
CHAT_DAILY_CAP = _env_int("CHAT_DAILY_CAP", 300)
CHAT_USER_COOLDOWN_SECONDS = _env_int("CHAT_USER_COOLDOWN_SECONDS", 3)
CHAT_MAX_TOOL_ROUNDS = _env_int("CHAT_MAX_TOOL_ROUNDS", 5)
CHAT_MEMORY_FILE = str(DATA_DIR / "chat_memory.json")
CHAT_HISTORY_FILE = str(DATA_DIR / "chat_history.json")

# ---- Robinhood Chain wallets + DEX trading (routed through KyberSwap) ----
# CUSTODIAL. McCap generates one EVM wallet per Discord user and holds the key,
# encrypted at rest under RHC_WALLET_SECRET. Whoever controls this host and the
# volume controls every wallet. Off by default; nobody may trade until they are
# on the allowlist; and every cap below is per user, per UTC day.
RHC_TRADING_ENABLE = _env_flag("RHC_TRADING_ENABLE", False)
RHC_WALLET_SECRET = (os.getenv("RHC_WALLET_SECRET") or "").strip()
RHC_TRADER_IDS = {
    int(x) for x in (os.getenv("RHC_TRADER_IDS") or "").replace(",", " ").split()
    if x.isascii() and x.isdigit()
}
# Optional server allowlist for trade commands. Empty = any server the bot is in.
RHC_GUILD_IDS = {
    int(x) for x in (os.getenv("RHC_GUILD_IDS") or "").replace(",", " ").split()
    if x.isascii() and x.isdigit()
}
RHC_CHAIN_ID = 4663
RHC_RPC_URLS = [
    u.strip() for u in (
        os.getenv("RHC_RPC_URLS")
        or "https://rpc.mainnet.chain.robinhood.com,https://robinhood-rpc.publicnode.com"
    ).split(",") if u.strip()
]
RHC_RPC_TIMEOUT = _env_int("RHC_RPC_TIMEOUT", 15)
RHC_EXPLORER = os.getenv("RHC_EXPLORER", "https://explorer.mainnet.chain.robinhood.com").rstrip("/")
RHC_KYBER_URL = os.getenv("RHC_KYBER_URL", "https://aggregator-api.kyberswap.com/robinhood/api/v1").rstrip("/")
RHC_KYBER_CLIENT_ID = os.getenv("RHC_KYBER_CLIENT_ID", "mccap")
# KyberSwap's MetaAggregationRouterV2 lives at the same address on every chain.
# Deliberately NOT env-overridable: the API's routerAddress must equal this or
# the trade is refused (see rhc/guard.py for why an address from a response is
# never trusted on this chain).
RHC_KYBER_ROUTER = "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"
RHC_MAX_TRADE_USD = float(os.getenv("RHC_MAX_TRADE_USD", "50"))
RHC_MAX_DAILY_USD = float(os.getenv("RHC_MAX_DAILY_USD", "200"))
RHC_DEFAULT_SLIPPAGE_BPS = _env_int("RHC_DEFAULT_SLIPPAGE_BPS", 200)
RHC_MAX_SLIPPAGE_BPS = _env_int("RHC_MAX_SLIPPAGE_BPS", 1000)
RHC_CONFIRM_TIMEOUT = _env_int("RHC_CONFIRM_TIMEOUT", 120)
# Post quotes, trade results, addresses and balances to the channel. Confirm
# prompts, refusals and the private-key export are always visible only to the
# user. Set to 0 to keep everything private.
RHC_PUBLIC_REPLIES = _env_flag("RHC_PUBLIC_REPLIES", True)
# Put the wallets' combined holdings in the bot's About Me (visible when someone
# clicks McCap) alongside the status line. Aggregate only; never per person.
RHC_ABOUT_ME_ENABLE = _env_flag("RHC_ABOUT_ME_ENABLE", True)
RHC_TX_TIMEOUT = _env_int("RHC_TX_TIMEOUT", 120)
# A wallet with an unresolved (unconfirmed) transaction refuses new ones for
# this long, so "it timed out, try again" cannot become a double spend.
RHC_PENDING_BLOCK_SECONDS = _env_int("RHC_PENDING_BLOCK_SECONDS", 900)
# In production DATA_DIR must be a mounted volume; keys minted onto ephemeral
# disk vanish on the next deploy together with the funds sent to them. The
# Dockerfile turns this on; local development leaves it off.
RHC_REQUIRE_MOUNTED_DATA_DIR = _env_flag("RHC_REQUIRE_MOUNTED_DATA_DIR", False)
RHC_WALLETS_FILE = str(DATA_DIR / "rhc_wallets.json")
RHC_LEDGER_FILE = str(DATA_DIR / "rhc_ledger.json")
RHC_JOURNAL_FILE = str(DATA_DIR / "rhc_trades.json")
RHC_ORDERS_FILE = str(DATA_DIR / "rhc_orders.json")

# Auto-orders: take-profit / stop-loss sells and one-shot dip or volume buys
# that fire without a confirm click (the rule itself is confirmed when armed).
# RHC_AUTO_ENABLE idles only the engine; RHC_TRADING_ENABLE still rules.
RHC_AUTO_ENABLE = _env_flag("RHC_AUTO_ENABLE", True)
RHC_AUTO_MAX_PER_USER = _env_int("RHC_AUTO_MAX_PER_USER", 10)
# Every armed rule's token is polled through the same DexScreener bucket the
# /mc watcher uses (a token near its target costs 6 requests a minute), so the
# total is bounded.
RHC_AUTO_MAX_TOTAL = _env_int("RHC_AUTO_MAX_TOTAL", 30)
RHC_AUTO_SELL_TTL = os.getenv("RHC_AUTO_SELL_TTL", "7d")
RHC_AUTO_BUY_TTL = os.getenv("RHC_AUTO_BUY_TTL", "24h")
RHC_AUTO_MAX_TTL = os.getenv("RHC_AUTO_MAX_TTL", "30d")
# Buy sizes offered as buttons (ordered). Sizes above RHC_MAX_TRADE_USD are
# left off the buttons and refused on click, never clamped.
RHC_BUTTON_USD_SIZES = [
    float(x) for x in (os.getenv("RHC_BUTTON_USD_SIZES") or "5,20").replace(",", " ").split()
    if x.replace(".", "", 1).isdigit()
]
# DexScreener's chainId slug for chain 4663 (verified live 2026-09-08). Trade
# buttons ride on an alert only when its token reports this chain.
RHC_DEX_CHAIN_ID = os.getenv("RHC_DEX_CHAIN_ID", "robinhood")
