import os
import sys
from pathlib import Path

import pytest

# Import the package from the repo root without needing an install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Keep config import-time side effects away from real credentials/files.
os.environ.setdefault("MCCAP_TOKEN", "test-token")
os.environ.setdefault("DONATION_WALLET", "")

# DATA_DIR defaults to "." so a bare `python main.py` works in a checkout — which
# means every storage path resolves inside the repo. Anything that reaches a real
# save_*() during a test therefore overwrites the developer's own state files.
# This bit me: a watcher test that fires an alert calls save_reminders() and
# truncated a live reminders.json to []. Redirect every path once, for the whole
# session, so no individual test has to remember to.
_REDIRECTED = ("REM_FILE", "MOVES_FILE", "WATCH_FILE", "ALERTS_FILE", "SCANS_FILE")


@pytest.fixture(autouse=True, scope="session")
def _isolate_storage(tmp_path_factory):
    from mccapbot import storage

    sandbox = tmp_path_factory.mktemp("mccap-data")
    originals = {name: getattr(storage, name) for name in _REDIRECTED if hasattr(storage, name)}
    for name in originals:
        setattr(storage, name, str(sandbox / Path(originals[name]).name))
    storage.DATA_DIR = sandbox
    yield sandbox
    for name, value in originals.items():
        setattr(storage, name, value)


@pytest.fixture(autouse=True)
def _guard_repo_data_files():
    """Fail loudly if a test writes a state file into the repo anyway.

    A test that quietly destroys local data is worse than a failing test.
    """
    repo = Path(__file__).resolve().parents[1]
    watched = ["reminders.json", "moves.json", "watchlists.json", "alerts.json", "scans.json"]
    before = {n: (repo / n).stat().st_mtime_ns for n in watched if (repo / n).exists()}
    yield
    for name, mtime in before.items():
        assert (repo / name).stat().st_mtime_ns == mtime, (
            f"a test modified {name} in the repo — storage paths must be redirected"
        )
