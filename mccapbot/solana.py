import aiohttp
from typing import Optional
from .config import SOLANA_RPC, DONATION_WALLET
from .constants import BASE58_ALPHABET
from .helpers import is_solana_address

async def sol_rpc(method: str, params: list):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.post(SOLANA_RPC, json={"jsonrpc":"2.0","id":1,"method":method,"params":params}) as r:
                if r.status != 200: return None
                return await r.json()
    except Exception:
        return None

async def get_solana_balance(address: str) -> Optional[float]:
    from .constants import LAMPORTS
    if not is_solana_address(address):
        return None
    res = await sol_rpc("getBalance", [address, {"commitment":"processed"}])
    try:
        lamports = int(((res or {}).get("result") or {}).get("value"))
        return lamports / LAMPORTS
    except Exception:
        return None

def _random_pubkey() -> str:
    import secrets
    raw = secrets.token_bytes(32)
    n = int.from_bytes(raw, "big")
    out = ""
    while n > 0:
        n, rem = divmod(n, 58)
        out = BASE58_ALPHABET[rem] + out
    lead_zeros = len(raw) - len(raw.lstrip(b"\x00"))
    return "1"*lead_zeros + (out or "1")
