import secrets
import time
from dataclasses import dataclass, field
from typing import Optional


def new_id() -> str:
    """Short, collision-resistant handle for an alert."""
    return secrets.token_hex(3)


@dataclass
class Reminder:
    """A one-shot level alert: fire when MC crosses a fixed target."""

    ca: str
    target_mc: float
    direction: str
    channel_id: int
    creator_id: int
    guild_id: int
    name: str
    symbol: str
    note: str = ""
    # Stable identity. Removing by list position raced with the watcher firing
    # an alert and shifting every index, so removals key off this instead.
    id: str = field(default_factory=new_id)
    created_ts: float = field(default_factory=time.time)
    # Set when the target was given relatively ("2x", "+50%"). Keeps the
    # original intent visible in /mc_list instead of a bare resolved number.
    spec: str = ""
    anchor_mc: Optional[float] = None


@dataclass
class MoveAlert:
    """A recurring momentum alert: fire when MC moves pct% within a window.

    Unlike a level alert this does not know its trigger price in advance, so it
    re-arms after firing (subject to a cooldown) rather than being consumed.
    """

    ca: str
    pct: float                 # magnitude, e.g. 30 for a 30% move
    window_sec: int
    direction: str             # "up" | "down" | "both"
    channel_id: int
    creator_id: int
    guild_id: int
    name: str
    symbol: str
    note: str = ""
    id: str = field(default_factory=new_id)
    created_ts: float = field(default_factory=time.time)
    cooldown_sec: int = 1800
    last_fired_ts: float = 0.0


@dataclass
class WatchItem:
    """A token on a server's watchlist. Display only — drives no polling."""

    ca: str
    guild_id: int
    added_by: int
    name: str
    symbol: str
    list_name: str = "default"
    added_ts: float = field(default_factory=time.time)


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
class AlertEvent:
    ts: float
    ca: str
    name: str
    symbol: str
    direction: str           # "above"|"below"|"up"|"down"
    target_mc: float
    current_mc: Optional[float]
    channel_id: int
    guild_id: int
    creator_id: int
    kind: str = "level"      # "level" | "move"
