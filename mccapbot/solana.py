"""Minimal Solana RPC access — currently just the bot wallet's SOL balance.

This existed before the rebuild as part of the Solana Pay stack and went out
with it. All that is needed now is a balance for the presence line, so this is
deliberately one call rather than a resurrected payments layer.
"""

from typing import Optional

from .config import SOLANA_RPC
from .helpers import is_solana_address
from .http import post_json
from .logging_setup import log

LAMPORTS_PER_SOL = 1_000_000_000


async def get_balance(address: str) -> Optional[float]:
    """SOL balance for an address, or None if it could not be read.

    None means "we don't know" and is never conflated with zero — a wallet that
    failed to fetch must not be displayed as empty. Callers keep their last
    known value instead.
    """
    if not is_solana_address(address):
        log.warning("Not a valid Solana address, skipping balance: %r", address)
        return None

    status, body = await post_json(
        SOLANA_RPC,
        {"jsonrpc": "2.0", "id": 1, "method": "getBalance",
         "params": [address, {"commitment": "processed"}]},
        timeout=15,
    )
    if status != 200 or not isinstance(body, dict):
        log.debug("getBalance failed for %s (status %s)", address[:6], status)
        return None
    if "error" in body:
        log.debug("getBalance RPC error for %s: %s", address[:6], body["error"])
        return None
    try:
        lamports = (body.get("result") or {}).get("value")
        return int(lamports) / LAMPORTS_PER_SOL
    except (TypeError, ValueError):
        log.debug("Unexpected getBalance payload for %s: %r", address[:6], body)
        return None
