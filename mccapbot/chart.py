"""The shape of a token, small enough to sit under a card without shouting.

The numbers on a token card say where a token is against its high. They do not
say how it got there: a token 40% off its high because it spiked once and bled
back is a different trade from one grinding up in steps. That is what a line
shows in the space three more figures would take.

Two rules keep this from becoming clutter. It draws only what some other read
already paid for — the hourly candles ``rhchain.price_highs`` fetched for the
off-high figure — so no chart ever costs a request of its own. And it draws
nothing when there is nothing to see: a pair minutes old has one candle and
gets no chart, only its numbers.

Nothing here can raise. A chart that will not render is simply absent; the
card, the post and the buttons under them are unaffected.
"""

import io
import time
from typing import List, Optional, Sequence, Tuple

from .helpers import BAD, GOOD, compact
from .logging_setup import log

MIN_POINTS = 6              # fewer than six candles is a dot, not a shape
BACKGROUND = "#2b2d31"      # Discord's dark embed background, so the PNG has no edges
INK = "#e3e5e8"
MUTED = "#9aa0a6"
SIZE = (6.4, 1.7)           # wide and short: a strip under the text, never the message
DPI = 130
# How far above the drawn line the high may sit and still be worth marking. A
# token down 90% would have its whole recent shape squashed into the bottom
# tenth of the panel just to fit a dashed line in — and the shape is the part
# the picture was for. Past this the text above the chart says the figure.
HIGH_LINE_MAX = 3.0


def _hex(colour: int) -> str:
    return f"#{colour:06X}"


def _span(seconds: float) -> str:
    """How much history the line covers, in the board's own words."""
    hours = max(1, int(round(seconds / 3600.0)))
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def render(points: Sequence[Tuple[float, float]], *, high: Optional[float] = None,
           now: Optional[float] = None) -> Optional[bytes]:
    """A PNG of ``points`` — (timestamp, market cap), oldest first — with the
    high marked. Green when it ends above where it started, red when below.
    Returns None whenever there is too little to draw or matplotlib is absent.
    """
    rows: List[Tuple[float, float]] = []
    for ts, value in points or ():
        try:
            ts, value = float(ts), float(value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            rows.append((ts, value))
    if len(rows) < MIN_POINTS:
        return None
    rows.sort(key=lambda r: r[0])

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001
        log.warning("matplotlib unavailable; no token chart")
        return None

    now = time.time() if now is None else now
    xs = [(r[0] - rows[-1][0]) / 3600.0 for r in rows]      # hours before the last candle
    ys = [r[1] for r in rows]
    colour = _hex(GOOD if ys[-1] >= ys[0] else BAD)

    fig, ax = plt.subplots(figsize=SIZE, dpi=DPI)
    fig.patch.set_facecolor(BACKGROUND)
    ax.set_facecolor(BACKGROUND)
    ax.plot(xs, ys, color=colour, linewidth=1.8, solid_capstyle="round")
    ax.fill_between(xs, ys, min(ys), color=colour, alpha=0.13, linewidth=0)

    mark = high if (high and 0 < high <= max(ys) * HIGH_LINE_MAX) else None
    if mark:
        ax.axhline(mark, color=MUTED, linewidth=0.8, linestyle=(0, (4, 4)))
        ax.annotate(f"high ${compact(mark)}", (xs[0], mark), color=MUTED, fontsize=7,
                    va="bottom", ha="left", xytext=(0, 3), textcoords="offset points")

    ax.annotate(f"${compact(ys[-1])}", (xs[-1], ys[-1]), color=INK, fontsize=8, fontweight="bold",
                va="center", ha="right", xytext=(-4, 9), textcoords="offset points")
    ax.annotate(_span(rows[-1][0] - rows[0][0]), (xs[0], min(ys)), color=MUTED, fontsize=7,
                va="bottom", ha="left", xytext=(1, 1), textcoords="offset points")

    # Headroom above the highest thing drawn, so the "high $x" label sits inside
    # the panel rather than clipped against its top edge.
    top = max(max(ys), mark or 0.0)
    ax.set_ylim(min(ys) * 0.98, top * (1.12 if mark else 1.06))
    ax.set_xlim(xs[0], 0)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.subplots_adjust(left=0.01, right=0.99, top=0.97, bottom=0.03)

    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    except Exception:  # noqa: BLE001
        log.debug("Could not render a token chart", exc_info=True)
        return None
    finally:
        plt.close(fig)
    return buf.getvalue()
