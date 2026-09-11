"""Push discovery feed for Robinhood Chain: the bot finds it.

Every minute the feed reads GeckoTerminal's newest pools, its 5-minute
trending list and the busiest pools, evaluates every token against each
server's thresholds, and posts one embed per qualifying candidate to that
server's feed channel with the alert buttons underneath. Three kinds:

- ``new_pair``: a pool under 15 minutes old with real liquidity, distinct
  buyers and buys well ahead of sells. Never posted on first sight: it goes
  to ``pending`` and posts only after a second look a minute later shows the
  liquidity stayed, and DexScreener can see the pair.
- ``spike``: five-minute volume running several times the hour's pace.
- ``mover``: a 5-minute price move on a major-quoted pool (a stock-quoted
  pool's swing says as much about the stock as the token).

Spikes and movers are confirmed against one DexScreener read before posting
(right chain, liquidity still there). Every post is graded: a ``ScanEvent``
with ``source="feed"`` is recorded, and the tracker (``mccapbot/tracker.py``)
follows its peak so ``/rh feed status`` can say how good the calls were.

What this module must never do:
- trade, quote, or touch a wallet — it posts; the buttons run the normal
  private quote + Confirm on the clicker's own wallet;
- post before the seen-set is written and saved (a redeploy between the
  send and the save would re-post the same token);
- post a token twice inside its cooldown, a major, a muted token, or run an
  hour past ``max_per_hour`` × ``OVERFLOW_MULT`` in one server;
- keep polling GeckoTerminal when its bucket is under half — the boards and
  the momentum backfill come first;
- rely on ``token_cache`` for anything: the alert watcher evicts feed tokens
  from it within minutes, so the feed keeps its own ``posted`` store.
"""

import asyncio
import io
import statistics
import time
from collections import deque
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Awaitable, Callable, Deque, Dict, Iterable, List, Optional, Tuple

import discord

from . import alerts, chart, gecko, grading, rhchain, storage
from .config import FEED_ENABLE, FEED_MAX_PENDING, MAX_SCAN_EVENTS, RHC_DEX_CHAIN_ID, RHCHAIN_NETWORK, RHCHAIN_NEW_PAGES
from .helpers import SEP, UNKNOWN, age, colour_for, footer, is_evm_address, mult, pct, plural, usd
from .logging_setup import log
from .models import ScanEvent, TokenSnapshot, new_id
from .rhchain import CHAIN_MAJORS, Pool, TokenActivity, aggregate

TICK_SECONDS = 60
KINDS = ("new_pair", "spike", "mover")

# Evaluation rules (see the plan; every threshold a server can tune is on FeedConfig).
NEW_PAIR_MAX_AGE = 900          # a "new pair" is under 15 minutes old at first sight
SECOND_LOOK_MIN_AGE = 60        # seconds in pending before the second look
SECOND_LOOK_KEEP = 0.7          # reserve must hold at least this share of what we first saw
CONFIRM_LIQ_FACTOR = 0.7        # DexScreener liquidity vs the server's floor
NEW_PAIR_MAX_TRIES = 10         # DexScreener lag: retries (ticks) before a new pair is dropped
SPIKE_MOVER_MIN_LIQ = 20_000.0
SPIKE_MIN_VOL_M5 = 2_000.0
KIND_COOLDOWN = 6 * 3600        # per token per kind
NEW_POST_QUIET = 600            # no spike inside 10 minutes of a new-pair post on the same token
SEEN_TTL = 7 * 86400
POSTED_TTL = 86400
MAX_CONFIRMS_PER_TICK = 20      # DexScreener reads per tick, however many candidates qualify
OVERFLOW_MULT = 2.0             # a busy hour may run to this much of the cap, never past it
OVERFLOW_BAR = 1.5              # ... and only for finds this much stronger than the hour's median
OFF_HIGH_WORTH_SAYING = 5.0     # within 5% of its high, a token has no dip worth a sentence
RATE_WINDOW = 300               # status shows request rates averaged over this


@dataclass
class FeedConfig:
    guild_id: int
    channel_id: int
    enabled: bool = True
    new_pairs: bool = True
    spikes: bool = True
    movers: bool = True
    min_liq: float = 5000.0
    min_buyers: int = 8
    pace: float = 3.0
    move_pct: float = 25.0
    max_per_hour: int = 10
    charts: bool = True                                     # the price line and the off-high figure
    muted: Dict[str, float] = field(default_factory=dict)   # ca -> until_ts

    def is_muted(self, ca: str, now: float) -> bool:
        return self.muted.get(ca, 0.0) > now


@dataclass
class Candidate:
    """One token that qualified under one rule, with the listing-row numbers
    the post is written from. ``guild_id``/``channel_id`` are stamped at post
    time so ``on_post`` subscribers know where it went."""

    kind: str
    ca: str
    symbol: str
    name: str
    pool: str
    decimals: int
    mc: Optional[float]
    mc_before: Optional[float]
    liq: float
    age_sec: float
    buyers_m5: int
    buys_m5: int
    sells_m5: int
    vol_m5: float
    vol_h1: float
    pace: Optional[float]
    change_m5: Optional[float]
    venue: str
    quote: str
    image_url: str
    guild_id: int = 0
    channel_id: int = 0

    def score(self) -> float:
        return self.buyers_m5 * max(1.0, self.pace or 1.0)

    def signals(self, info=None) -> Dict[str, Any]:
        """The numbers this call is being made on, frozen for the record.

        Without these the feed can say how a call did but never why it was
        made, which is the difference between a scoreboard and something that
        can tell a good threshold from a lucky one. ``stock`` is here because a
        tokenized equity tracks a share price and cannot behave like a
        memecoin — rather than hard-coding that, let it be learned.
        """
        out: Dict[str, Any] = {
            "kind": self.kind,
            "venue": self.venue,
            "stock": "yes" if is_stock_token(self.name) else "no",
            "buyers": self.buyers_m5,
            "liq": self.liq,
            "age_h": round(self.age_sec / 3600.0, 3) if self.age_sec else 0.0,
        }
        if self.mc:
            out["mc"] = self.mc
            if self.liq:
                out["depth"] = self.liq / self.mc * 100.0
        if self.pace:
            out["pace"] = self.pace
        traders = self.buyers_m5 + max(0, self.sells_m5)
        if traders:
            out["buyer_share"] = self.buyers_m5 / traders * 100.0
        if info is not None:
            if getattr(info, "holders", None):
                out["holders"] = float(info.holders)
            if getattr(info, "top10_pct", None) is not None:
                out["top10"] = float(info.top10_pct)
        return out

    def seen_key(self) -> str:
        return f"new:{self.pool}" if self.kind == "new_pair" else f"{'spike' if self.kind == 'spike' else 'move'}:{self.ca}"


@dataclass
class PendingNew:
    """A new pair waiting for its second look and its DexScreener confirm."""

    ca: str
    pool: str
    symbol: str
    name: str
    decimals: int
    first_ts: float
    first_liq: float
    first_buyers: int
    tries: int = 0
    cand: Dict[str, Any] = field(default_factory=dict)

    def candidate(self) -> Optional[Candidate]:
        return _candidate_from(self.cand)


@dataclass
class PostedToken:
    """The feed's own record of what it posted (24h): symbol resolution for
    ``/rh buy PONS``, the hourly cap, and the strategy's event source."""

    ca: str
    symbol: str
    decimals: int
    ts: float
    kind: str
    guild_id: int = 0
    score: float = 0.0          # how strong the find was, so the hour has a median to raise a bar against


# ---------------- state (one process, one feed) ----------------

configs: Dict[int, FeedConfig] = {}          # guild_id -> config
seen: Dict[str, float] = {}                  # "new:<pool>" | "spike:<ca>" | "move:<ca>" -> ts
pending: Dict[str, PendingNew] = {}          # ca -> pending new pair
posted: List[PostedToken] = []


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _candidate_from(d: Dict[str, Any]) -> Optional[Candidate]:
    if not isinstance(d, dict) or not d.get("ca"):
        return None
    known = {f.name for f in fields(Candidate)}
    try:
        return Candidate(**{k: v for k, v in d.items() if k in known})
    except TypeError:
        return None


def config_for(guild_id: int) -> Optional[FeedConfig]:
    return configs.get(int(guild_id))


def set_config(cfg: FeedConfig) -> FeedConfig:
    """Install a server's feed settings, keeping whatever it had muted: the
    manager re-running ``/rh feed on`` to change a threshold is not asking to
    un-mute the tokens they silenced."""
    old = configs.get(int(cfg.guild_id))
    if old is not None and old.muted and not cfg.muted:
        cfg.muted = dict(old.muted)
    configs[int(cfg.guild_id)] = cfg
    return cfg


def recent_tokens(hours: float = 24) -> Dict[str, Tuple[str, int]]:
    """ca -> (symbol, decimals) for everything the feed posted lately, newest
    wins; the symbol resolver's last stop after the live boards."""
    cutoff = time.time() - hours * 3600
    out: Dict[str, Tuple[str, int]] = {}
    for p in sorted(posted, key=lambda p: p.ts):
        if p.ts >= cutoff and p.ca:
            out[p.ca] = (p.symbol, int(p.decimals or 0))
    return out


def posts_last_hour(guild_id: int, now: Optional[float] = None) -> int:
    return len(hour_scores(guild_id, now))


def hour_scores(guild_id: int, now: Optional[float] = None) -> List[float]:
    """How strong everything this server posted in the last hour was. The
    selector raises its bar against the median of these."""
    now = time.time() if now is None else now
    return [float(p.score or 0.0) for p in posted if p.guild_id == guild_id and now - p.ts <= 3600]


async def load_feed() -> None:
    """Hydrate the module state from feed.json. An unreadable file leaves the
    in-memory state alone (storage keeps the original as .corrupt)."""
    raw = await storage.load_feed()
    if raw is None:
        return
    new_configs: Dict[int, FeedConfig] = {}
    for c in raw["configs"]:
        try:
            cfg = storage._coerce(FeedConfig, c)
            cfg.muted = {str(k): float(v) for k, v in (cfg.muted or {}).items() if _f(v) is not None}
            new_configs[int(cfg.guild_id)] = cfg
        except Exception:
            log.warning("Skipping unreadable feed config: %r", c)
    new_seen = {str(k): float(v) for k, v in raw["seen"].items() if _f(v) is not None}
    new_pending: Dict[str, PendingNew] = {}
    for ca, d in raw["pending"].items():
        try:
            new_pending[str(ca).lower()] = storage._coerce(PendingNew, d)
        except Exception:
            log.warning("Skipping unreadable pending feed entry: %r", d)
    new_posted: List[PostedToken] = []
    for d in raw["posted"]:
        try:
            new_posted.append(storage._coerce(PostedToken, d))
        except Exception:
            log.warning("Skipping unreadable feed post record: %r", d)
    configs.clear()
    configs.update(new_configs)
    seen.clear()
    seen.update(new_seen)
    pending.clear()
    pending.update(new_pending)
    posted[:] = new_posted


async def save_feed() -> None:
    await storage.save_feed({
        "configs": [asdict(c) for c in configs.values()],
        "seen": dict(seen),
        "pending": {ca: asdict(p) for ca, p in pending.items()},
        "posted": [asdict(p) for p in posted],
    })


def prune(now: Optional[float] = None) -> bool:
    """Drop expired seen keys (7d), posts (24h), mutes and stale pending
    entries. Returns True when anything changed."""
    now = time.time() if now is None else now
    changed = False
    for k in [k for k, ts in seen.items() if now - ts > SEEN_TTL]:
        del seen[k]
        changed = True
    kept = [p for p in posted if now - p.ts <= POSTED_TTL]
    if len(kept) != len(posted):
        posted[:] = kept
        changed = True
    for cfg in configs.values():
        for ca in [ca for ca, until in cfg.muted.items() if until <= now]:
            del cfg.muted[ca]
            changed = True
    # A pending pair nobody could confirm inside the new-pair window is not new any more.
    for ca in [ca for ca, pn in pending.items() if now - pn.first_ts > NEW_PAIR_MAX_AGE]:
        seen[f"new:{pending[ca].pool}"] = now
        del pending[ca]
        changed = True
    return changed


# ---------------- evaluation (pure) ----------------

def _candidate(kind: str, t: TokenActivity, pool: Pool, now: float) -> Candidate:
    return Candidate(
        kind=kind,
        ca=t.address,
        symbol=t.symbol,
        name=t.name,
        pool=pool.address,
        decimals=int(pool.base_decimals or 0),
        mc=t.mc_usd,
        mc_before=t.mc_before("m5"),
        liq=t.liq_usd,
        age_sec=max(0.0, now - t.created_ts) if t.created_ts else 0.0,
        buyers_m5=t.buyers("m5"),
        buys_m5=t.buys("m5"),
        sells_m5=t.sells("m5"),
        vol_m5=t.volume("m5"),
        vol_h1=t.volume("h1"),
        pace=t.volume_pace("m5", "h1"),
        change_m5=t.change("m5"),
        venue=pool.dex,
        quote=pool.quote_symbol,
        image_url=pool.base_image_url or "",
    )


def _new_pair(t: TokenActivity, cfg: FeedConfig, seen_map: Dict[str, float], now: float) -> Optional[Candidate]:
    if not cfg.new_pairs or not t.created_ts or now - t.created_ts > NEW_PAIR_MAX_AGE:
        return None
    if t.liq_usd < cfg.min_liq or t.buyers("m5") < cfg.min_buyers:
        return None
    if t.buys("m5") < 2 * t.sells("m5"):
        return None
    fresh = [
        p for p in t.pools
        if p.created_ts and now - p.created_ts <= NEW_PAIR_MAX_AGE and f"new:{p.address}" not in seen_map
    ]
    if not fresh:
        return None
    return _candidate("new_pair", t, max(fresh, key=lambda p: p.liq_usd), now)


def _spike(t: TokenActivity, cfg: FeedConfig, seen_map: Dict[str, float], now: float) -> Optional[Candidate]:
    if not cfg.spikes:
        return None
    pace = t.volume_pace("m5", "h1")
    if pace is None or pace < cfg.pace or t.volume("m5") < SPIKE_MIN_VOL_M5:
        return None
    if t.liq_usd < max(cfg.min_liq, SPIKE_MOVER_MIN_LIQ) or t.buyers("m5") < cfg.min_buyers:
        return None
    if now - seen_map.get(f"spike:{t.address}", 0.0) < KIND_COOLDOWN:
        return None
    if any(now - seen_map.get(f"new:{p.address}", -1e12) < NEW_POST_QUIET for p in t.pools):
        return None
    return _candidate("spike", t, t.reference, now)


def _mover(t: TokenActivity, cfg: FeedConfig, seen_map: Dict[str, float], now: float) -> Optional[Candidate]:
    if not cfg.movers:
        return None
    chg = t.change("m5")
    if chg is None or chg < cfg.move_pct:
        return None
    if t.liq_usd < max(cfg.min_liq, SPIKE_MOVER_MIN_LIQ) or t.buyers("m5") < cfg.min_buyers:
        return None
    if not t.quote_is_major:
        return None
    if now - seen_map.get(f"move:{t.address}", 0.0) < KIND_COOLDOWN:
        return None
    return _candidate("mover", t, t.reference, now)


def evaluate(
    tokens: Iterable[TokenActivity],
    cfg: FeedConfig,
    seen_map: Dict[str, float],
    now: float,
    pending_cas: Optional[Iterable[str]] = None,
) -> List[Candidate]:
    """Which tokens qualify under this server's thresholds, from the listing
    rows alone (no requests). One candidate per token: a new pair outranks a
    spike outranks a move, so one token is never two posts in one minute.
    Majors, muted tokens and tokens already pending are skipped."""
    skip = {c.lower() for c in (pending_cas or ())}
    out: List[Candidate] = []
    for t in tokens:
        if not t.pools or t.symbol.upper() in CHAIN_MAJORS:
            continue
        if cfg.is_muted(t.address, now) or t.address in skip:
            continue
        cand = _new_pair(t, cfg, seen_map, now) or _spike(t, cfg, seen_map, now) or _mover(t, cfg, seen_map, now)
        if cand is not None:
            out.append(cand)
    return out


def strength(cand: Candidate) -> float:
    """How strong a find is: the rule's own score, weighted by how calls that
    looked like it have actually gone. With nothing learned yet the weight is
    exactly 1.0, so this is the score it always was."""
    return cand.score() * grading.weight(cand.signals(), grading.table(storage.scan_events))


def rank(cands: List[Candidate], room: int) -> List[Candidate]:
    """Strongest first (strength, then youngest), at most ``room``."""
    if room <= 0:
        return []
    return sorted(cands, key=lambda c: (-strength(c), c.age_sec))[:room]


def select(cands: List[Candidate], cfg: FeedConfig,
           now: Optional[float] = None) -> Tuple[List[Candidate], List[Candidate]]:
    """Split this tick's confirmed finds into what posts and what waits.

    The hourly cap used to be a shutter: the first ``max_per_hour`` finds took
    the slots and every later one was dropped however good it was, so a quiet
    token at :01 could cost the server the run of the hour at :45. Past the cap
    the feed no longer goes quiet, it gets picky — a find has to beat the
    median of what the hour already carried by ``OVERFLOW_BAR``, and an hour
    still cannot run past ``OVERFLOW_MULT`` × the cap however good the tape is.

    Nothing held here is lost: it is never written to the seen-set, so the next
    tick weighs it again against whatever the hour looks like by then, and the
    tokens that arrive later have to be better than the ones already posted.
    """
    budget = max(0, int(cfg.max_per_hour))
    ceiling = int(round(budget * OVERFLOW_MULT))
    scores = hour_scores(cfg.guild_id, now)
    picked: List[Candidate] = []
    held: List[Candidate] = []
    for cand in sorted(cands, key=lambda c: (-strength(c), c.age_sec)):
        mark = strength(cand)
        bar = statistics.median(scores) * OVERFLOW_BAR if scores else 0.0
        room = len(scores) < budget or (len(scores) < ceiling and mark >= bar)
        (picked if room else held).append(cand)
        if room:
            scores.append(mark)
    return picked, held


def qualifies(cfg: FeedConfig, cand: Candidate, now: float) -> bool:
    """A server's thresholds against a candidate built elsewhere (a pending
    new pair that passed its second look)."""
    if not cfg.enabled or cfg.is_muted(cand.ca, now):
        return False
    if cand.kind == "new_pair":
        return cfg.new_pairs and cand.liq >= cfg.min_liq and cand.buyers_m5 >= cfg.min_buyers
    if cand.kind == "spike":
        return cfg.spikes
    return cfg.movers


# ---------------- the poller ----------------

# Robinhood's own tokenized equities are named "NVIDIA • Robinhood Token".
# The suffix is the test, not the word: "Robinhood Wallet" and "Robinhood Hat
# Strategy" are ordinary memecoins that happen to be named after the company.
STOCK_SUFFIX = "robinhood token"


def is_stock_token(name: str) -> bool:
    """A tokenized share rather than something that trades like a memecoin."""
    return (name or "").strip().lower().endswith(STOCK_SUFFIX)


def _ago(seconds: float) -> str:
    """23s under a minute, then the board's 4m / 2h / 3d."""
    secs = max(0, int(seconds))
    return f"{secs}s" if secs < 60 else age(secs)


def _risk_placeholder() -> str:
    return f"Risk: {UNKNOWN}"


async def _risk_line(ca: str) -> str:
    """The GeckoTerminal risk line once mccapbot/rhc/risk.py exists; the
    placeholder until then or on any failure."""
    try:
        from .rhc import risk  # type: ignore
        line = risk.risk_line(await risk.token_info(ca))
        return line or _risk_placeholder()
    except Exception:
        return _risk_placeholder()


async def _links_line(ca: str) -> str:
    """X, Telegram, the site, the chart, GMGN and the explorer, as links. The
    same cached lookup the risk line uses, so it costs no extra request."""
    try:
        from .rhc import risk  # type: ignore
        return risk.links_line(await risk.token_info(ca), ca)
    except Exception:
        return ""


TITLES = {"new_pair": "🆕 New pair", "spike": "📈 Volume spike", "mover": "🚀 Mover"}


def _off_high_line(cand: Candidate, highs: Optional[rhchain.Highs]) -> str:
    """Where this token sits against the best it has managed. A token coming
    back to life well under its own high is a different trade from one making
    a new high, and the post should not make the reader go and look."""
    if highs is None:
        return ""
    off = highs.off_7d if highs.off_7d is not None else highs.off_24h
    if off is None or off > -OFF_HIGH_WORTH_SAYING:
        return ""
    price = highs.high_7d if highs.off_7d is not None else highs.high_24h
    peak = highs.mc_of(price, cand.mc)
    span = "7d" if highs.hours >= 150 else f"{highs.hours}h"
    return f"**{pct(off)}** off its {span} high" + (f" ({usd(peak)} MC)" if peak else "")


def chart_png(cand: Candidate, highs: Optional[rhchain.Highs]) -> Optional[bytes]:
    """The line behind the numbers, from the candles the off-high figure already
    paid for. A pair minutes old has no shape yet and gets none."""
    if highs is None or not highs.series or not cand.mc:
        return None
    points = [(ts, highs.mc_of(close, cand.mc) or 0.0) for ts, close in highs.series]
    return chart.render(points, high=highs.mc_of(highs.high_7d, cand.mc))


async def build_embed(cand: Candidate, snap: Optional[TokenSnapshot],
                      highs: Optional[rhchain.Highs] = None) -> discord.Embed:
    """One post. Every figure through helpers; one bold figure per line."""
    lines = [f"MC **{usd(cand.mc)}**" + (
        f" (was {usd(cand.mc_before)} 5m ago)" if cand.kind == "mover" and cand.mc_before else ""
    )]
    off_high = _off_high_line(cand, highs)
    if off_high:
        lines.append(off_high)
    lines.append(footer(f"Liq {usd(cand.liq)}", f"age {age(cand.age_sec)}" if cand.age_sec else "",
                        cand.venue, f"quote {cand.quote}" if cand.quote and cand.quote != "?" else ""))
    lines.append(footer(f"5m: **{plural(cand.buyers_m5, 'buyer')}**",
                        f"{cand.buys_m5} buys / {cand.sells_m5} sells", f"vol {usd(cand.vol_m5)}"))
    if cand.kind == "spike":
        lines.append(footer(f"Pace **{mult(cand.pace)}** the hour's rate", f"vol 1h {usd(cand.vol_h1)}"))
    elif cand.kind == "mover":
        lines.append(f"5m **{pct(cand.change_m5)}**")
    lines.append(await _risk_line(cand.ca))
    links = await _links_line(cand.ca)
    if links:
        lines.append(links)
    lines.append(f"`{cand.ca}`")
    embed = discord.Embed(
        title=f"{TITLES.get(cand.kind, cand.kind)}{SEP}{cand.symbol}",
        description="\n".join(lines),
        colour=colour_for(cand.change_m5),
        url=(snap.url if snap is not None and snap.url else None),
    )
    image = (snap.image_url if snap is not None else "") or cand.image_url
    if image:
        embed.set_thumbnail(url=image)
    embed.set_footer(text=footer(
        "GeckoTerminal",
        "second look passed" if cand.kind == "new_pair" else "",
        "most new pairs are dust: check the sell-back line before buying",
    ))
    return embed


def tradeable(ca: str, snap: Optional[TokenSnapshot]) -> bool:
    """Whether the trade buttons belong under this token — the same gate
    ``trade_view`` applies, named so the vote handler can ask it later without
    having to keep the snapshot around."""
    return (snap is not None and getattr(snap, "chain", "") == RHC_DEX_CHAIN_ID
            and is_evm_address(ca))


def feed_view(ca: str, eid: str, ups: int = 0, downs: int = 0,
              *, trade: bool = True) -> Optional[discord.ui.View]:
    """The buttons under a feed post: the trade row when the token is one McCap
    can trade, and the two votes either way. One builder for the first post and
    for every redraw after a vote, so a tally can never drift from the row it
    sits under."""
    try:
        from . import views
        maker = getattr(views, "feed_row", None)
        if maker is None:
            return None
        return maker(ca, eid, ups, downs, trade=bool(trade))
    except Exception:
        log.exception("Could not build the feed's button row for %s", ca)
        return None


def trade_view(ca: str, snap: Optional[TokenSnapshot]) -> Optional[discord.ui.View]:
    """The alert buttons, only under a Robinhood Chain token (same gate as
    ``alerts._trade_row``); never for anything else, never on a view error."""
    try:
        if snap is None or getattr(snap, "chain", "") != RHC_DEX_CHAIN_ID or not is_evm_address(ca):
            return None
        from . import views
        maker = getattr(views, "alert_row", None)
        return maker(ca) if maker else None
    except Exception:
        log.exception("Could not build the feed's trade row for %s", ca)
        return None


class Feed:
    """The one poller (``bot.feed``). ``run`` loops; ``tick`` does one pass
    and is what the tests drive; ``status`` renders ``/rh feed status``."""

    def __init__(self, bot):
        self.bot = bot
        self.on_post: List[Callable[[Candidate, TokenSnapshot], Awaitable[None]]] = []
        self.paused = False
        self.last_tick_ts = 0.0
        self.last_fetch_ts = 0.0
        self._capped: Dict[int, bool] = {}
        self._held: Dict[int, List[Tuple[float, str]]] = {}     # gid -> (ts, symbol) waiting on the bar
        self._unreachable: Dict[int, float] = {}
        self._gecko_ts: Deque[float] = deque()
        self._dex_ts: Deque[float] = deque()

    # ----- lifecycle -----

    async def run(self) -> None:
        if not FEED_ENABLE:
            log.info("Discovery feed is off (FEED_ENABLE=0).")
            return
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(TICK_SECONDS)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("discovery feed tick error")

    # ----- one pass -----

    async def tick(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self.last_tick_ts = now
        dirty = prune(now)
        self._capped.clear()
        self._held.clear()
        active = [c for c in configs.values() if c.enabled]
        try:
            if not active:
                return
            if gecko.gecko_limiter.available() < 0.5 * gecko.gecko_limiter.capacity:
                self.paused = True
                return
            self.paused = False

            pools = await self._fetch(now)
            self.last_fetch_ts = now
            if rhchain.last_error:
                return                      # a cached list is not a fresh listing; status says stale
            tokens = aggregate(pools)
            by_ca = {t.address: t for t in tokens}

            jobs: List[Tuple[FeedConfig, Candidate]] = []
            for cfg in active:
                for cand in evaluate(tokens, cfg, seen, now, pending_cas=pending):
                    if cand.kind == "new_pair":
                        dirty |= self._enqueue(cand, now)
                    else:
                        jobs.append((cfg, cand))

            passed, changed = await self._second_look(now, by_ca)
            dirty |= changed
            for cand in passed:
                for cfg in active:
                    if qualifies(cfg, cand, now):
                        jobs.append((cfg, cand))

            confirmed, changed = await self._confirm(jobs, now)
            dirty |= changed

            per_guild: Dict[int, List[Tuple[Candidate, TokenSnapshot]]] = {}
            for cfg, cand, snap in confirmed:
                per_guild.setdefault(cfg.guild_id, []).append((cand, snap))
            chosen: List[Tuple[FeedConfig, Candidate, TokenSnapshot]] = []
            for gid, items in per_guild.items():
                cfg = configs[gid]
                picked, held = select([c for c, _ in items], cfg, now)
                self._note_held(gid, held, now)
                snaps = {c.ca: s for c, s in items}
                chosen.extend((cfg, c, snaps[c.ca]) for c in picked)

            if chosen:
                # Seen + posted go to disk BEFORE anything is sent: a redeploy
                # between the send and the save would re-post the same token.
                for cfg, cand, snap in chosen:
                    seen[cand.seen_key()] = now
                    posted.append(PostedToken(ca=cand.ca, symbol=cand.symbol, decimals=cand.decimals,
                                              ts=now, kind=cand.kind, guild_id=cfg.guild_id,
                                              score=strength(cand)))
                    pending.pop(cand.ca, None)
                await save_feed()
                dirty = False
                for cfg, cand, snap in chosen:
                    await self._post(cfg, cand, snap, now)
        finally:
            if dirty:
                await save_feed()

    def _note_held(self, guild_id: int, held: List[Candidate], now: float) -> None:
        """What the bar turned away this hour, for ``/rh feed status``. These are
        not dropped — they are simply not the strongest thing on the tape."""
        rows = [(ts, sym) for ts, sym in self._held.get(guild_id, []) if now - ts <= 3600]
        seen_syms = {sym for _, sym in rows}
        rows += [(now, c.symbol) for c in held if c.symbol not in seen_syms]
        self._held[guild_id] = rows[-25:]
        self._capped[guild_id] = bool(rows)

    def held_last_hour(self, guild_id: int, now: float) -> List[str]:
        return [sym for ts, sym in self._held.get(guild_id, []) if now - ts <= 3600]

    async def _fetch(self, now: float) -> List[Pool]:
        before = rhchain.request_count
        new = await rhchain.new_pools(pages=RHCHAIN_NEW_PAGES)
        trending = await rhchain.trending_pools("5m")
        top = await rhchain.top_pools()
        self._count(self._gecko_ts, now, rhchain.request_count - before)
        return rhchain.dedup_pools(new, trending, top)

    def _enqueue(self, cand: Candidate, now: float) -> bool:
        """A new pair at first sight goes to pending, never to the channel.
        The set is capped; the weakest candidate makes room."""
        if cand.ca in pending:
            return False
        if len(pending) >= FEED_MAX_PENDING:
            def mark(p) -> float:
                got = p.candidate()
                return strength(got) if got else 0.0
            weakest = min(pending.values(), key=mark)
            if strength(cand) <= mark(weakest):
                return False
            del pending[weakest.ca]
        pending[cand.ca] = PendingNew(
            ca=cand.ca, pool=cand.pool, symbol=cand.symbol, name=cand.name, decimals=cand.decimals,
            first_ts=now, first_liq=cand.liq, first_buyers=cand.buyers_m5, cand=asdict(cand),
        )
        return True

    async def _second_look(self, now: float, by_ca: Dict[str, TokenActivity]) -> Tuple[List[Candidate], bool]:
        """Pending pairs at least a minute old: one ``tokens/multi`` call. A
        pair whose reserve fell below 70% of what we first saw is dropped and
        marked seen; one GeckoTerminal has not indexed yet waits; the rest pass
        with their numbers refreshed from this tick's rows when present."""
        due = [ca for ca, pn in pending.items() if now - pn.first_ts >= SECOND_LOOK_MIN_AGE]
        if not due:
            return [], False
        due = due[:gecko.TOKENS_MULTI_MAX]
        multi = await gecko.tokens_multi(due, RHCHAIN_NETWORK)
        self._count(self._gecko_ts, now, 1)
        if multi is None:
            return [], False                                    # a blip; try again next tick
        passed: List[Candidate] = []
        changed = False
        for ca in due:
            pn = pending[ca]
            attrs = multi.get(ca)
            if attrs is None:
                continue                                        # not indexed yet; prune() bounds the wait
            reserve = _f(attrs.get("total_reserve_in_usd"))
            if reserve is None or reserve < SECOND_LOOK_KEEP * pn.first_liq:
                seen[f"new:{pn.pool}"] = now
                del pending[ca]
                changed = True
                continue
            t = by_ca.get(ca)
            cand = None
            if t is not None:
                pool = next((p for p in t.pools if p.address == pn.pool), t.reference)
                cand = _candidate("new_pair", t, pool, now)
            cand = cand or pn.candidate()
            if cand is None:
                del pending[ca]
                changed = True
                continue
            passed.append(cand)
        return passed, changed

    async def _confirm(
        self, jobs: List[Tuple[FeedConfig, Candidate]], now: float
    ) -> Tuple[List[Tuple[FeedConfig, Candidate, TokenSnapshot]], bool]:
        """One DexScreener read per token (``alerts._refresh``), strongest
        first, at most ``MAX_CONFIRMS_PER_TICK``. No pairs yet: a new pair
        stays pending (dropped after 10 tries), a spike/mover is skipped this
        tick without touching ``seen``. Wrong chain drops a new pair for good.
        A server's liquidity floor is checked per server."""
        jobs = sorted(jobs, key=lambda j: j[1].score(), reverse=True)
        snaps: Dict[str, Optional[TokenSnapshot]] = {}
        out: List[Tuple[FeedConfig, Candidate, TokenSnapshot]] = []
        changed = False
        for cfg, cand in jobs:
            ca = cand.ca
            if ca not in snaps:
                if len(snaps) >= MAX_CONFIRMS_PER_TICK:
                    continue
                snap = await self._read(ca, now)
                snaps[ca] = snap
                if snap is None and cand.kind == "new_pair" and ca in pending:
                    pn = pending[ca]
                    pn.tries += 1
                    changed = True
                    if pn.tries >= NEW_PAIR_MAX_TRIES:
                        seen[f"new:{pn.pool}"] = now
                        del pending[ca]
                elif snap is not None and snap.chain != RHC_DEX_CHAIN_ID and cand.kind == "new_pair" and ca in pending:
                    seen[f"new:{pending[ca].pool}"] = now
                    del pending[ca]
                    changed = True
            snap = snaps[ca]
            if snap is None or snap.chain != RHC_DEX_CHAIN_ID:
                continue
            if (snap.liq_usd or 0.0) < CONFIRM_LIQ_FACTOR * cfg.min_liq:
                continue
            out.append((cfg, cand, snap))
        return out, changed

    async def _read(self, ca: str, now: float) -> Optional[TokenSnapshot]:
        """The confirm read. None when the request failed or DexScreener has
        no pairs for the token yet (a snapshot without a chain)."""
        try:
            ok = await alerts._refresh(ca)
        except Exception:
            log.exception("feed confirm read failed for %s", ca)
            ok = False
        self._count(self._dex_ts, now, 1)
        if not ok:
            return None
        snap = storage.cache_snapshot(ca)
        if snap is None or snap.mc is None or not snap.chain:
            return None
        return snap

    async def _post(self, cfg: FeedConfig, cand: Candidate, snap: TokenSnapshot, now: float) -> None:
        cand = replace(cand, guild_id=cfg.guild_id, channel_id=cfg.channel_id)
        # The record's id is minted before the message so the vote buttons can
        # name the call they belong to. Two servers getting the same token get
        # two records and two tallies: they are two different calls.
        eid = new_id()
        try:
            # Two GeckoTerminal reads, cached ten minutes, for the two things a
            # listing row cannot say: how far under its own high this is, and
            # the shape that got it there. A brand-new pair has neither.
            highs = await rhchain.price_highs(cand.ca) if cfg.charts else None
            embed = await build_embed(cand, snap, highs)
            png = await asyncio.to_thread(chart_png, cand, highs) if cfg.charts else None
            view = feed_view(cand.ca, eid, trade=tradeable(cand.ca, snap))
            ch = await self.bot.fetch_channel(cfg.channel_id)
            kw: Dict[str, Any] = {"embed": embed}
            if png:
                kw["file"] = discord.File(io.BytesIO(png), filename="token.png")
                embed.set_image(url="attachment://token.png")
            if view is not None:
                kw["view"] = view
            msg = await ch.send(**kw)
        except Exception as e:
            # The seen-set already holds this post, so a dead channel costs the
            # server the post, never a repeat. The manager sees it in status.
            self._unreachable[cfg.guild_id] = now
            log.warning("Feed could not post to channel %s in guild %s: %s: %s",
                        cfg.channel_id, cfg.guild_id, type(e).__name__, e)
            return
        self._unreachable.pop(cfg.guild_id, None)
        info = None
        try:
            from .rhc import risk  # type: ignore
            info = await risk.token_info(cand.ca)          # the cached read the risk line just made
        except Exception:
            log.debug("No holder data for the record of %s", cand.ca, exc_info=True)
        ev = ScanEvent(
            id=eid,
            ca=cand.ca, guild_id=cfg.guild_id, channel_id=cfg.channel_id, scanner_id=0,
            name=cand.name, symbol=cand.symbol, mc_at_scan=snap.mc,
            message_id=int(getattr(msg, "id", 0) or 0), ts=now,
            last_mc=snap.mc, peak_mc=snap.mc, peak_ts=now, last_checked_ts=now,
            source="feed", kind=cand.kind, signals=cand.signals(info),
        )
        storage.scan_events.insert(0, ev)
        del storage.scan_events[MAX_SCAN_EVENTS:]
        try:
            await storage.save_scans()
        except Exception:
            log.exception("Could not save the feed's scan event")
        log.info("Feed posted %s %s (%s) to %s", cand.kind, cand.symbol, cand.ca, cfg.channel_id)
        for cb in list(self.on_post):
            try:
                await cb(cand, snap)
            except Exception:
                log.exception("feed on_post subscriber failed")

    # ----- status -----

    @staticmethod
    def _count(bucket: Deque[float], now: float, n: int) -> None:
        for _ in range(max(0, int(n))):
            bucket.append(now)
        while bucket and now - bucket[0] > RATE_WINDOW:
            bucket.popleft()

    @staticmethod
    def _rate(bucket: Deque[float], now: float) -> str:
        recent = [ts for ts in bucket if now - ts <= RATE_WINDOW]
        if not recent:
            return "0"
        minutes = max(1.0, min(RATE_WINDOW, now - recent[0]) / 60.0)
        per_min = len(recent) / minutes
        return "<1" if per_min < 1 else f"{per_min:.0f}"

    def state_parts(self, guild_id: int, now: float) -> List[str]:
        parts: List[str] = []
        if not FEED_ENABLE:
            parts.append("off (FEED_ENABLE=0)")
        elif self.paused:
            parts.append("paused (GeckoTerminal busy)")
        if rhchain.last_error:
            parts.append(f"stale: last refresh failed {_ago(now - rhchain.last_error_ts)} ago")
        if self.held_last_hour(guild_id, now):
            parts.append("over cap")
        if guild_id in self._unreachable:
            parts.append("channel unreachable")
        return parts

    def status(self, guild_id: int, now: Optional[float] = None) -> Dict[str, Any]:
        """Everything ``/rh feed status`` shows, plus ``lines``/``text`` ready to send."""
        now = time.time() if now is None else now
        cfg = configs.get(int(guild_id))
        if cfg is None:
            text = "No feed in this server. A manager can turn one on with /rh feed on."
            return {"enabled": False, "config": None, "lines": [text], "text": text}

        evs = [s for s in storage.scan_events
               if s.source == "feed" and s.guild_id == cfg.guild_id and now - s.ts <= 86400]
        peaks = [m for m in (s.multiple() for s in evs) if m is not None]
        currents = [m for m in (s.current_multiple() for s in evs) if m is not None]
        median_peak = statistics.median(peaks) if peaks else None
        hit_2x = sum(1 for m in peaks if m >= 2.0)
        below = sum(1 for m in currents if m < 1.0)
        n_hour = posts_last_hour(cfg.guild_id, now)
        held = self.held_last_hour(cfg.guild_id, now)
        state = self.state_parts(cfg.guild_id, now)
        kinds = ", ".join(k for k, on in (("new pairs", cfg.new_pairs), ("spikes", cfg.spikes),
                                          ("movers", cfg.movers)) if on) or "none"
        fetch = f"last fetch {_ago(now - self.last_fetch_ts)} ago" if self.last_fetch_ts else f"last fetch {UNKNOWN}"
        lines = [
            footer(f"Feed → <#{cfg.channel_id}>", f"**{'on' if cfg.enabled else 'off'}**", *state),
            footer(f"Kinds: {kinds}", f"liq ≥ {usd(cfg.min_liq)}", f"buyers ≥ {cfg.min_buyers}",
                   f"pace ≥ {mult(cfg.pace)}", f"move ≥ {pct(cfg.move_pct)}"),
            footer(f"Last hour: **{n_hour}** {'post' if n_hour == 1 else 'posts'} (cap {cfg.max_per_hour}"
                   + (" · past it only stronger finds post)" if held else ")"),
                   f"waiting on the bar: {', '.join(held[:5])}" if held else "",
                   f"pending {len(pending)}", fetch),
            footer(f"Last 24h: {plural(len(evs), 'post')}",
                   f"median peak **{mult(median_peak)}**" if median_peak is not None else "",
                   f"{hit_2x} hit 2x" if peaks else "",
                   f"{below} below entry" if currents else ""),
            footer(f"GeckoTerminal ~{self._rate(self._gecko_ts, now)} req/min",
                   f"DexScreener ~{self._rate(self._dex_ts, now)} req/min"),
        ]
        muted = sum(1 for until in cfg.muted.values() if until > now)
        if muted:
            lines.append(f"Muted: {plural(muted, 'token')}")
        return {
            "enabled": bool(cfg.enabled) and bool(FEED_ENABLE),
            "config": cfg,
            "channel_id": cfg.channel_id,
            "state": state,
            "paused": self.paused,
            "stale": bool(rhchain.last_error),
            "capped": bool(held),
            "held": held,
            "ceiling": int(round(cfg.max_per_hour * OVERFLOW_MULT)),
            "unreachable": cfg.guild_id in self._unreachable,
            "posts_last_hour": n_hour,
            "cap": cfg.max_per_hour,
            "pending": len(pending),
            "last_fetch_ts": self.last_fetch_ts,
            "posts_24h": len(evs),
            "median_peak": median_peak,
            "hit_2x": hit_2x,
            "below_entry": below,
            "muted": muted,
            "lines": lines,
            "text": "\n".join(lines),
        }
