import os
from dotenv import load_dotenv

load_dotenv()

# ---- Config / Env ----
TOKEN             = os.getenv("MCCAP_TOKEN")
SOLANA_RPC        = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
DONATION_WALLET   = os.getenv("DONATION_WALLET", "").strip()

LOG_LEVEL         = os.getenv("LOG_LEVEL", "INFO").upper()

BALANCE_POLL_SECONDS = int(os.getenv("BALANCE_POLL_SECONDS", "300"))
POLL_SECONDS          = 3

DEX_TOKEN_URL     = "https://api.dexscreener.com/latest/dex/tokens/{address}"
SOLANA_USE_FDV    = True
DEX_BLACKLIST     = {"heaven"}

USDC_MINT         = os.getenv("USDC_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
PAY_EXPIRY_SEC    = int(os.getenv("PAY_EXPIRY_SEC", "1800"))
PAY_POLL_SEC      = int(os.getenv("PAY_POLL_SEC", "10"))

# ---- Files ----
REM_FILE    = "reminders.json"
PAY_FILE    = "payments.json"
ALERTS_FILE = "alerts.json"

# ---- Display helpers ----
MAX_ROWS_DEBUG = 10
