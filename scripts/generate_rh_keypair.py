"""Generate an Ed25519 keypair for the Robinhood Crypto API.

    python scripts/generate_rh_keypair.py

Paste the PUBLIC key into the Robinhood API Credentials Portal; keep the PRIVATE
key as the RH_PRIVATE_KEY_B64 environment variable. Robinhood never sees the
private key and neither does this repository — it is printed once, here, and
nowhere else.

Run it locally, not on Railway: the point is that the secret starts on your
machine and is pasted into Railway's env vars by you.
"""

import base64

from nacl.signing import SigningKey


def main() -> None:
    key = SigningKey.generate()
    private_b64 = base64.b64encode(bytes(key)).decode()
    public_b64 = base64.b64encode(bytes(key.verify_key)).decode()

    print("Ed25519 keypair for Robinhood Crypto\n")
    print("PUBLIC KEY  — paste this into the Robinhood API Credentials Portal:")
    print(f"  {public_b64}\n")
    print("PRIVATE KEY — set as RH_PRIVATE_KEY_B64 (Railway variable, never committed):")
    print(f"  {private_b64}\n")
    print("Robinhood then issues an API key — set that as RH_API_KEY.")
    print("\nThis private key is shown once. Anyone holding it can trade your account.")


if __name__ == "__main__":
    main()
