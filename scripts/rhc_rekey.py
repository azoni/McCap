"""Re-encrypt the Robinhood Chain wallet vault under a new secret.

A rotated RHC_WALLET_SECRET locks every wallet permanently unless the records
are re-encrypted first. This tool does that offline, never touching the input:

    set RHC_OLD_SECRET=...   (the secret the file was written with)
    set RHC_NEW_SECRET=...   (the one you are rotating to; 32+ characters)
    python scripts/rhc_rekey.py rhc_wallets.json rhc_wallets.rekeyed.json

Every record is decrypted with the old secret, checked (the key must derive the
record's address and carry the record's user id), re-encrypted with the new
secret, and the whole output is decrypted again with the new secret before the
file is written. Then upload the output over the old file on the volume and set
the new secret on the service in the same deploy.

Run it locally, on a machine you trust. It prints addresses, never keys.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MCCAP_TOKEN", "rekey")

from mccapbot.rhc import wallets  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    old = (os.getenv("RHC_OLD_SECRET") or "").strip()
    new = (os.getenv("RHC_NEW_SECRET") or "").strip()
    if len(old) < 1 or len(new) < wallets.MIN_SECRET_LEN:
        print(f"Set RHC_OLD_SECRET and RHC_NEW_SECRET (new one at least {wallets.MIN_SECRET_LEN} characters).")
        return 2
    if dst.exists():
        print(f"{dst} already exists; refusing to overwrite.")
        return 2

    records = [wallets.Wallet(**{k: r[k] for k in wallets.Wallet.__dataclass_fields__ if k in r})
               for r in json.loads(src.read_text(encoding="utf-8"))]
    print(f"{len(records)} record(s) in {src}")

    rekeyed = []
    wallets.RHC_WALLET_SECRET = old
    keys = []
    for w in records:
        key = wallets._decrypt(w)          # verifies user id and address
        keys.append((w, key))
        print(f"  ok  user {w.user_id}  {w.address}")

    wallets.RHC_WALLET_SECRET = new
    for w, key in keys:
        env = wallets._encrypt(w.user_id, key)
        rekeyed.append(wallets.Wallet(user_id=w.user_id, address=w.address, created_ts=w.created_ts,
                                      label=w.label, **env))
    # Prove the output opens with the new secret before writing anything.
    for w, (_orig, key) in zip(rekeyed, keys):
        assert wallets._decrypt(w) == key, f"re-encryption check failed for {w.address}"

    dst.write_text(json.dumps([w.__dict__ for w in rekeyed], indent=2), encoding="utf-8")
    print(f"wrote {dst}; verified every record decrypts with the new secret")
    print("Next: upload it over rhc_wallets.json on the volume AND set RHC_WALLET_SECRET to the new value in the same deploy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
