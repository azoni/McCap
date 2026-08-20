import secrets
from typing import Optional

from .config import SOLANA_RPC
from .constants import BASE58_ALPHABET, LAMPORTS
from .helpers import is_solana_address
from .http import post_json


async def sol_rpc(method: str, params: list):
    status, body = await post_json(
        SOLANA_RPC,
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=15,
    )
    return body if status == 200 else None


async def get_solana_balance(address: str) -> Optional[float]:
    if not is_solana_address(address):
        return None
    res = await sol_rpc("getBalance", [address, {"commitment": "processed"}])
    try:
        lamports = int(((res or {}).get("result") or {}).get("value"))
        return lamports / LAMPORTS
    except Exception:
        return None


def random_pubkey() -> str:
    """Base58-encode 32 random bytes — used as a Solana Pay reference key."""
    raw = secrets.token_bytes(32)
    n = int.from_bytes(raw, "big")
    out = ""
    while n > 0:
        n, rem = divmod(n, 58)
        out = BASE58_ALPHABET[rem] + out
    lead_zeros = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * lead_zeros + (out or "1")
