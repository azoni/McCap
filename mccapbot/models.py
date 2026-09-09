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
    # Dollar volume over the last hour, from the same DexScreener payload. None
    # when the fetch had no pairs; auto-buy rules on volume never treat None as 0.
    vol1h: Optional[float] = None
    # The rest of what the same payload carries, so alerts, the discovery feed
    # and rule floors can read context without another request. None / 0 when
    # the pair did not report it; nothing here is ever a reason to fire.
    liq_usd: Optional[float] = None
    change_m5: Optional[float] = None
    change_h1: Optional[float] = None
    buys_m5: int = 0
    sells_m5: int = 0
    buys_h1: int = 0
    sells_h1: int = 0
    vol_m5: Optional[float] = None
    pair_created_ts: float = 0.0
    pair_address: str = ""


@dataclass
class AutoOrder:
    """One-shot rule that trades without a confirm click: confirmed by its owner
    when armed, consumed when it fires. Sells watch the market cap; buys watch
    the market cap or the 1h volume. Plain defaults (0, "", False) on purpose:
    storage._coerce drops falsy values only for default_factory fields."""

    ca: str                  # checksummed EVM address on Robinhood Chain
    symbol: str
    decimals: int
    side: str                # "buy" | "sell"
    metric: str              # "mc" | "vol1h"  (sells are always "mc")
    direction: str           # "above" | "below"  (helpers.meets semantics)
    target: float            # USD market cap or USD 1h volume
    size: float              # sell: percent 1..100 ; buy: USD (<= RHC_MAX_TRADE_USD)
    slippage_bps: int
    user_id: int
    guild_id: int            # 0 in a DM
    channel_id: int          # where reports go (unless private)
    expires_ts: float
    spec: str = ""                     # "2x", "-30%"; "" for an absolute target
    anchor_mc: Optional[float] = None  # what spec was measured from
    anchor: str = ""                   # "entry" | "now" | ""
    private: bool = False              # report by DM instead of the channel
    id: str = field(default_factory=new_id)
    created_ts: float = field(default_factory=time.time)
    status: str = "armed"              # "armed" | "firing" | "pending"
    tx: str = ""                       # set while status == "pending"
    fired_ts: float = 0.0
    attempts: int = 0
    last_attempt_ts: float = 0.0
    last_error: str = ""
    # Trailing stop: the target ratchets up as the market cap makes new highs
    # (only after two agreeing fresh samples), never down.
    trail_pct: float = 0.0
    high_mc: float = 0.0
    # Protection on fill: sell rules to arm when this buy lands ("tp=2x:50,sl=-30%:100"),
    # and where a rule came from (manual | tpsl | button | feed | strategy).
    then: str = ""
    parent_id: str = ""
    origin: str = ""
    # Floors re-checked at fire time for buys: liquidity and 5-minute buys.
    min_liq: float = 0.0
    min_buyers: int = 0
    # Momentum-triggered buys watch a window; 0 for every other metric.
    window_sec: int = 0

    @property
    def target_mc(self) -> Optional[float]:
        """Duck-types as a level alert for the polling scheduler."""
        return self.target if self.metric == "mc" else None


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
    # Who called it: "" for a scanner bot's post, "feed" for McCap's own
    # discovery feed; and for the feed, which rule fired (new_pair | spike | mover).
    source: str = ""
    kind: str = ""

    def multiple(self) -> Optional[float]:
        """Peak gain as a multiple of the scan price (2.0 == a 2x)."""
        if not self.mc_at_scan or self.mc_at_scan <= 0 or self.peak_mc is None:
            return None
        return self.peak_mc / self.mc_at_scan

    def current_multiple(self) -> Optional[float]:
        if not self.mc_at_scan or self.mc_at_scan <= 0 or self.last_mc is None:
            return None
        return self.last_mc / self.mc_at_scan


@dataclass
class MemoryNote:
    """Something McCap was told to remember: a fact, a preference, a task.

    ``scope`` is "g<guild_id>" inside a server and "u<user_id>" in a DM, so one
    server's notes never show up in another and DMs stay private.
    """

    scope: str
    text: str
    author_id: int
    author_name: str
    id: str = field(default_factory=new_id)
    created_ts: float = field(default_factory=time.time)


@dataclass
class ChatTurn:
    """One message in a channel's rolling conversation with McCap."""

    channel_id: int
    role: str                # "user" | "assistant"
    content: str
    author_name: str = ""
    ts: float = field(default_factory=time.time)
