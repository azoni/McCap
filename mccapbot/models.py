from dataclasses import dataclass
from typing import Optional

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
