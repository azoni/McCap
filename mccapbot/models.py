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
    # Auto-armed alerts (e.g. from a detected scan) expire, so one scanned token
    # doesn't consume request budget forever. 0 means never expires — the
    # default, and what every user-created alert keeps.
    auto_expires_ts: float = 0.0


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


@dataclass
class ScanEvent:
    """A token seen in a scanner bot's post, tracked for later performance.

    ``mc_at_scan`` is the whole point: it is the entry price the call is judged
    against. ``peak_mc`` is updated as the token is re-checked, so the report can
    say what the best exit would have been rather than only where it ended up.
    """

    ca: str
    guild_id: int
    channel_id: int
    scanner_id: int              # bot that posted the scan
    name: str
    symbol: str
    mc_at_scan: Optional[float]
    message_id: int = 0
    requested_by: int = 0        # human who triggered the scan, 0 if unknown
    id: str = field(default_factory=new_id)
    ts: float = field(default_factory=time.time)
    # --- performance tracking, updated by the tracker loop ---
    peak_mc: Optional[float] = None
    peak_ts: float = 0.0
    last_mc: Optional[float] = None
    last_checked_ts: float = 0.0

    def multiple(self) -> Optional[float]:
        """Peak gain as a multiple of the scan price (2.0 == a 2x)."""
        if not self.mc_at_scan or self.mc_at_scan <= 0 or self.peak_mc is None:
            return None
        return self.peak_mc / self.mc_at_scan

    def current_multiple(self) -> Optional[float]:
        if not self.mc_at_scan or self.mc_at_scan <= 0 or self.last_mc is None:
            return None
        return self.last_mc / self.mc_at_scan
