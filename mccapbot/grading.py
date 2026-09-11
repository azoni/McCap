"""What McCap has learned from its own calls.

Every threshold in the discovery feed — pace 3x, eight buyers, $5K of
liquidity — was picked by hand and has never been checked against an outcome.
This module is the check. It reads the calls the feed has already made, each
one carrying the numbers it fired on (``ScanEvent.signals``), what the token
then did (the tracker's peak and current market cap) and what people made of
it (``ScanEvent.votes``), and turns that into a per-bucket verdict: "spikes
with pace over 10x have gone 2.1x over twelve calls", "pools under 2% depth
have gone nowhere over fifteen".

Those verdicts become a bounded multiplier on a candidate's strength, so the
feed spends its hourly budget on the kinds of call that have worked. Three
rules keep that from becoming a black box nobody can argue with:

- **It cannot act on a handful of samples.** A bucket needs ``MIN_SAMPLES``
  graded calls before it counts for anything. Until then the multiplier is
  exactly 1.0 and the feed behaves as it always did.
- **It cannot take over.** The multiplier is clamped to ``MAX_WEIGHT`` either
  way, so evidence can move a candidate up or down the queue but never
  manufacture one that failed the rules or suppress one that passed them.
  Nothing here touches a guard: what is *safe* to trade is settled in
  ``rhc/trade.py`` and no amount of upvoting changes it.
- **It has to be able to explain itself.** ``report()`` prints every bucket,
  its sample count and its verdict, so ``/rh feed grade`` shows exactly what
  is being rewarded and what is being marked down.

A human vote outranks the price for a good reason: price says whether a token
went up, a person says whether it was a call worth making. A tokenized stock
that drifts 2% and a rug that has not dumped yet both look fine on price.
"""

import statistics
import time
from typing import Dict, Iterable, List, Optional, Tuple

from .config import FEED_LEARN_ENABLE, FEED_LEARN_MAX_WEIGHT, FEED_LEARN_MIN_SAMPLES
from .helpers import UNKNOWN, mult, pct, plural, usd
from .models import ScanEvent

# A call needs this long before its numbers mean anything: a token posted two
# minutes ago has not had the chance to do either thing.
MIN_AGE_SECONDS = 30 * 60
# ... unless somebody has already voted on it, which is a judgement, not a
# measurement, and does not need to wait.
GRADE_WINDOW = 14 * 86400          # how far back the evidence goes

# Where a signal stops being one thing and starts being another. Coarse on
# purpose: fine bins would each hold two calls and learn noise.
BIN_EDGES: Dict[str, Tuple[float, ...]] = {
    "pace": (3.0, 5.0, 10.0),
    "depth": (2.0, 5.0, 15.0),          # liquidity as a % of market cap
    "buyers": (10, 25, 60),
    "liq": (25_000, 100_000, 500_000),
    "mc": (250_000, 2_000_000, 20_000_000),
    "age_h": (0.25, 2.0, 24.0),
    "holders": (500, 2_000, 10_000),
    "top10": (25.0, 50.0, 75.0),
    "buyer_share": (40.0, 55.0, 70.0),
}
# Signals that are a name rather than a number.
CATEGORICAL = ("kind", "venue", "stock")

_FMT = {
    "liq": usd, "mc": usd,
    "depth": lambda v: pct(v, signed=False), "top10": lambda v: pct(v, signed=False),
    "buyer_share": lambda v: pct(v, signed=False),
    "pace": mult,
}


def _num(v: str, name: str) -> str:
    fmt = _FMT.get(name)
    try:
        return fmt(float(v)) if fmt else (f"{float(v):g}" + ("h" if name == "age_h" else ""))
    except (TypeError, ValueError):
        return str(v)


def bucket(name: str, value) -> Optional[str]:
    """The bucket label a signal falls in, or None when it is not a signal we
    bin. Labels read the way the report prints them: ``pace 5x-10x``."""
    if name in CATEGORICAL:
        text = str(value).strip().lower()
        return f"{name} {text}" if text else None
    edges = BIN_EDGES.get(name)
    if edges is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    lo = None
    for edge in edges:
        if v < edge:
            return f"{name} {_num(lo, name)}-{_num(edge, name)}" if lo is not None else f"{name} under {_num(edge, name)}"
        lo = edge
    return f"{name} over {_num(edges[-1], name)}"


def features(signals: Dict[str, float]) -> List[str]:
    """Every bucket a call belongs to."""
    out = []
    for name, value in (signals or {}).items():
        got = bucket(name, value)
        if got:
            out.append(got)
    return out


def label(ev: ScanEvent, now: Optional[float] = None) -> Optional[float]:
    """How good a call turned out, in -1..+1, or None when it is too early to say.

    The price half reads the peak and where it stands now as two separate
    facts, because they answer different questions: the peak is whether there
    was ever a trade in it, and the current figure is whether holding it hurt.
    A call that doubled and then round-tripped scores near zero — it was
    tradeable, and it ended badly, and both of those are true.

    A vote outweighs the price when there is one. Somebody who watched it
    happen knows things the market cap does not.
    """
    now = time.time() if now is None else now
    voted = bool(ev.votes)
    mature = now - ev.ts >= MIN_AGE_SECONDS
    if not voted and not mature:
        return None

    # A call posted a minute ago reads as "went nowhere" because its peak and
    # its last reading are both the price it was posted at. That is the clock,
    # not the market, and averaging it against a vote would water down the one
    # real opinion on the record.
    price: Optional[float] = None
    peak, current = (ev.multiple(), ev.current_multiple()) if mature else (None, None)
    if peak is not None or current is not None:
        up = min(1.0, max(0.0, ((peak or 1.0) - 1.0) / 1.0))        # a 2x peak is a full mark
        down = min(1.0, max(0.0, (1.0 - (current or 1.0)) / 0.5))   # halved is a full mark against
        price = up - down

    if not voted:
        return price
    votes = max(-1.0, min(1.0, ev.vote_score() / 3.0))
    if price is None:
        return votes
    return 0.6 * votes + 0.4 * price


def gradable(events: Iterable[ScanEvent], now: Optional[float] = None) -> List[Tuple[ScanEvent, float]]:
    """The calls that can be learned from, with their marks."""
    now = time.time() if now is None else now
    out = []
    for ev in events:
        if ev.source != "feed" or not ev.signals or now - ev.ts > GRADE_WINDOW:
            continue
        mark = label(ev, now)
        if mark is not None:
            out.append((ev, mark))
    return out


def verdicts(events: Iterable[ScanEvent], now: Optional[float] = None) -> Dict[str, Tuple[float, int, int, int]]:
    """bucket -> (mean mark, calls, upvotes, downvotes)."""
    marks: Dict[str, List[float]] = {}
    votes: Dict[str, List[Tuple[int, int]]] = {}
    for ev, mark in gradable(events, now):
        for feat in features(ev.signals):
            marks.setdefault(feat, []).append(mark)
            votes.setdefault(feat, []).append((ev.ups(), ev.downs()))
    return {
        feat: (statistics.fmean(vals), len(vals),
               sum(u for u, _ in votes[feat]), sum(d for _, d in votes[feat]))
        for feat, vals in marks.items()
    }


# ---------------- the part that changes what gets posted ----------------

_table: Dict[str, Tuple[float, int, int, int]] = {}
_table_at = 0.0
TABLE_CACHE_SECONDS = 60


def table(events: Iterable[ScanEvent], now: Optional[float] = None, *, force: bool = False):
    """The verdicts, recomputed at most once a minute. The feed asks for this
    once per candidate per tick, and walking a couple of thousand events each
    time would cost more than the ranking is worth."""
    global _table, _table_at
    now = time.time() if now is None else now
    if force or not _table_at or now - _table_at >= TABLE_CACHE_SECONDS:
        _table = verdicts(events, now)
        _table_at = now
    return _table


def clear_cache() -> None:
    global _table, _table_at
    _table, _table_at = {}, 0.0


def weight(signals: Dict[str, float], learned=None) -> float:
    """A multiplier on a candidate's strength, from how calls like it have gone.

    1.0 when there is nothing to say — no signals, learning switched off, or no
    bucket with enough calls behind it. Clamped both ways, so this reorders a
    queue and never overrules the rules that put a candidate in it.
    """
    if not FEED_LEARN_ENABLE or not signals:
        return 1.0
    learned = _table if learned is None else learned
    feats = features(signals)
    if not feats:
        return 1.0
    # Every bucket the candidate is in counts towards the average, and one we
    # have not learned about yet counts as zero rather than dropping out of it.
    # Dropping it would shrink the divisor, which quietly turns "we know
    # nothing about tokenized shares yet" into a bonus for being one — the
    # opposite of what too little evidence should mean.
    marks = [learned[f][0] if (f in learned and learned[f][1] >= FEED_LEARN_MIN_SAMPLES) else 0.0
             for f in feats]
    if not any(marks):
        return 1.0
    return max(1.0 / FEED_LEARN_MAX_WEIGHT, min(FEED_LEARN_MAX_WEIGHT, 1.0 + statistics.fmean(marks)))


def explain(signals: Dict[str, float], learned=None) -> List[str]:
    """Why a candidate scored the way it did, for the report and the logs."""
    learned = _table if learned is None else learned
    out = []
    for feat in features(signals):
        got = learned.get(feat)
        if got and got[1] >= FEED_LEARN_MIN_SAMPLES:
            out.append(f"{feat} {got[0]:+.2f} over {plural(got[1], 'call')}")
    return out


# ---------------- what /rh feed grade prints ----------------

def report(events: Iterable[ScanEvent], now: Optional[float] = None, limit: int = 10) -> List[str]:
    """What McCap has learned, in the order of how strongly it believes it."""
    now = time.time() if now is None else now
    events = list(events)
    graded = gradable(events, now)
    posts = [e for e in events if e.source == "feed" and now - e.ts <= GRADE_WINDOW]
    voted = [e for e in posts if e.votes]
    if not posts:
        return ["No feed calls to grade yet. `/rh feed on` starts one."]

    ups = sum(e.ups() for e in posts)
    downs = sum(e.downs() for e in posts)
    lines = [
        f"**{plural(len(posts), 'call')}** in the last {GRADE_WINDOW // 86400}d"
        f" · **{len(graded)}** graded · {len(voted)} voted on (👍 {ups} / 👎 {downs})",
    ]
    peaks = [m for m in (e.multiple() for e in posts) if m is not None]
    if peaks:
        lines.append(f"Median peak **{mult(statistics.median(peaks))}**"
                     f" · {sum(1 for p in peaks if p >= 2)} reached 2x")

    learned = verdicts(events, now)
    if not learned:
        lines.append(f"Nothing learned yet: a call needs {MIN_AGE_SECONDS // 60}m or a vote before it counts.")
        return lines

    ready = {f: v for f, v in learned.items() if v[1] >= FEED_LEARN_MIN_SAMPLES}
    rows = sorted(ready.items(), key=lambda kv: abs(kv[1][0]), reverse=True)[:limit]
    if rows:
        lines.append(f"\n**Scoring on {plural(len(ready), 'pattern')}** (needs {FEED_LEARN_MIN_SAMPLES} calls each):")
        for feat, (mark, n, u, d) in rows:
            arrow = "📈" if mark > 0.05 else ("📉" if mark < -0.05 else "▪️")
            lines.append(f"{arrow} `{feat}` **x{max(1.0 / FEED_LEARN_MAX_WEIGHT, min(FEED_LEARN_MAX_WEIGHT, 1 + mark)):.2f}**"
                         f" · {plural(n, 'call')}" + (f" · 👍 {u} / 👎 {d}" if (u or d) else ""))
    waiting = sorted(((f, v) for f, v in learned.items() if v[1] < FEED_LEARN_MIN_SAMPLES),
                     key=lambda kv: -kv[1][1])[:6]
    if waiting:
        lines.append("Not enough yet: " + ", ".join(f"{f} ({v[1]})" for f, v in waiting))
    if not FEED_LEARN_ENABLE:
        lines.append("⚠️ Learning is switched off (`FEED_LEARN_ENABLE=0`); this is what it would do.")
    return lines


__all__ = [
    "bucket", "features", "label", "gradable", "verdicts", "table", "weight",
    "explain", "report", "clear_cache", "MIN_AGE_SECONDS", "GRADE_WINDOW",
]
