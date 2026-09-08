import os
import sys
from pathlib import Path

import pytest

# Import the package from the repo root without needing an install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Keep config import-time side effects away from real credentials/files.
os.environ.setdefault("MCCAP_TOKEN", "test-token")
os.environ.setdefault("DONATION_WALLET", "")
# Chat must never reach the real API from a test; an unset key also turns the
# feature off, which is the state most tests want.
os.environ.pop("ANTHROPIC_API_KEY", None)
# Tests exercise the wallet vault with a known secret and never enable live trading.
os.environ["RHC_WALLET_SECRET"] = "test-vault-secret-not-for-production-0123"
# Set explicitly (not popped): config's load_dotenv() would otherwise fill a
# popped variable back in from a developer's .env.
os.environ["RHC_TRADING_ENABLE"] = "0"
os.environ["RHC_REQUIRE_MOUNTED_DATA_DIR"] = "0"

# DATA_DIR defaults to "." so a bare `python main.py` works in a checkout — which
# means every storage path resolves inside the repo. Anything that reaches a real
# save_*() during a test therefore overwrites the developer's own state files.
# This bit me: a watcher test that fires an alert calls save_reminders() and
# truncated a live reminders.json to []. Redirect every path once, for the whole
# session, so no individual test has to remember to.
_REDIRECTED = (
    "REM_FILE", "MOVES_FILE", "WATCH_FILE", "ALERTS_FILE", "SCANS_FILE",
    "CHAT_MEMORY_FILE", "CHAT_HISTORY_FILE",
)
# The rhc package reads its paths from config at import time; redirect those too.
_RHC_REDIRECTED = ("RHC_WALLETS_FILE", "RHC_LEDGER_FILE", "RHC_JOURNAL_FILE")


@pytest.fixture(autouse=True, scope="session")
def _isolate_storage(tmp_path_factory):
    from mccapbot import storage

    sandbox = tmp_path_factory.mktemp("mccap-data")
    originals = {name: getattr(storage, name) for name in _REDIRECTED if hasattr(storage, name)}
    for name in originals:
        setattr(storage, name, str(sandbox / Path(originals[name]).name))
    storage.DATA_DIR = sandbox

    from mccapbot.rhc import ledger, wallets
    rhc_originals = {
        (mod, name): getattr(mod, name)
        for mod in (ledger, wallets) for name in _RHC_REDIRECTED if hasattr(mod, name)
    }
    for (mod, name), value in rhc_originals.items():
        setattr(mod, name, str(sandbox / Path(value).name))
    yield sandbox
    for name, value in originals.items():
        setattr(storage, name, value)
    for (mod, name), value in rhc_originals.items():
        setattr(mod, name, value)


@pytest.fixture(autouse=True)
def _guard_repo_data_files():
    """Fail loudly if a test writes a state file into the repo anyway.

    A test that quietly destroys local data is worse than a failing test.
    """
    repo = Path(__file__).resolve().parents[1]
    watched = [
        "reminders.json", "moves.json", "watchlists.json", "alerts.json", "scans.json",
        "chat_memory.json", "chat_history.json",
        "rhc_wallets.json", "rhc_ledger.json", "rhc_trades.json",
    ]
    before = {n: (repo / n).stat().st_mtime_ns for n in watched if (repo / n).exists()}
    yield
    for name, mtime in before.items():
        assert (repo / name).stat().st_mtime_ns == mtime, (
            f"a test modified {name} in the repo — storage paths must be redirected"
        )
