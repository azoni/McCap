import os
import sys
from pathlib import Path

# Import the package from the repo root without needing an install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Keep config import-time side effects away from real credentials/files.
os.environ.setdefault("MCCAP_TOKEN", "test-token")
os.environ.setdefault("DONATION_WALLET", "")
