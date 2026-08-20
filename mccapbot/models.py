import secrets
import time
from dataclasses import dataclass, field
from typing import Optional


def new_id() -> str:
    """Short, collision-resistant handle for an alert."""
    return secrets.token_hex(3)


@dataclass
class Reminder:
    ca: str
    target_mc: float
    direction: str
    channel_id: int
    creator_id: int
    guild_id: int
    name: str
    symbol: str
    note: str = ""
    # Stable identity. Removing by list position raced with the watcher firing an
    # alert and shifting every index, so removals now key off this instead.
    id: str = field(default_factory=new_id)
    created_ts: float = field(default_factory=time.time)
    # Scheduler bookkeeping (not persisted meaningfully; recomputed at runtime).
    last_checked_ts: float = 0.0


@dataclass
class TokenSnapshot:
    mc: Optional[float]
    url: str
    updated_ts: float
    source: str = "unknown"
    dex: str = ""
    chain: str = ""
    quote: str = ""
    consensus: float = 0.0
    delta: Optional[float] = None
    image_url: str = ""


@dataclass
class Invoice:
    id: str
    reference: str
    asset: str
    mint: str
    amount_base: int
    decimals: int
    user_id: int
    channel_id: int
    guild_id: int
    note: str = ""
    created_ts: float = 0.0
    status: str = "pending"  # pending|paid|expired
    tx_sig: str = ""         # set when confirmed


@dataclass
class AlertEvent:
    ts: float
    ca: str
    name: str
    symbol: str
    direction: str           # "above"|"below"
    target_mc: float
    current_mc: Optional[float]
    channel_id: int
    guild_id: int
    creator_id: int
