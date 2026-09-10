"""What a trader wants to know about one token, in the moment they pick it.

A board row has to stay narrow enough for twenty-five of them, so it carries
the cheap figures only. The moment somebody picks a token they are deciding
whether to buy it, and that is worth three GeckoTerminal requests: how far it
is off its real high (hourly candles, not window boundaries), how many people
hold it and how concentrated they are, and whether it can be sold at all.

Everything here is best effort and cached. A figure McCap cannot get is left
out; nothing on this card is ever a reason a trade is allowed or refused —
the sell-back check in ``rhc/trade.py`` is what actually stands in the way of
a honeypot.
"""

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import chart, rhchain
from .dex import token_summary
from .helpers import SEP, UNKNOWN, age, footer, pct, plural, qty, usd
from .logging_setup import log
from .rhc import risk

# A 7-day high needs about a week of candles behind it. With fewer, the figure
# is still true, it just covers less: say so rather than implying a week.
WEEK_HOURS = 150


@dataclass
class Card:
    """One token, assembled from the market data, its highs and its holders."""

    ca: str
    symbol: str = ""
    name: str = ""
    mc: Optional[float] = None
    price: Optional[float] = None
    liq: Optional[float] = None
    depth_pct: Optional[float] = None          # liquidity as a share of market cap
    venue: str = ""
    quote: str = ""
    age_sec: Optional[float] = None
    buyers_m5: int = 0
    buys_m5: int = 0
    sells_m5: int = 0
    buyer_share_h1: Optional[float] = None
    vol_h24: Optional[float] = None
    highs: Optional[rhchain.Highs] = None
    info: Optional[risk.TokenInfo] = None

    # ----- derived -----

    def mc_at(self, price: Optional[float]) -> Optional[float]:
        """A past price expressed as a market cap, so the card talks in the same
        units as the rest of McCap. Supply does not move, so the ratio holds.
        The highs series converts against its own last close; only a card with
        no highs falls back to the listing price it was built from."""
        if self.highs is not None:
            got = self.highs.mc_of(price, self.mc)
            if got is not None:
                return got
        if not price or not self.price or not self.mc or self.price <= 0:
            return None
        return self.mc * price / self.price

    @property
    def off_24h(self) -> Optional[float]:
        return self.highs.off_24h if self.highs else None

    @property
    def off_7d(self) -> Optional[float]:
        return self.highs.off_7d if self.highs else None

    def high_label(self) -> str:
        """"7d" once there is a week of candles behind it, else the hours the
        record actually covers — "34h high" rather than a "7d high" that is
        really a day and a half of history."""
        hours = self.highs.hours if self.highs else 0
        return "7d" if hours >= WEEK_HOURS else f"{hours}h"

    # ----- text -----

    def lines(self) -> list:
        """The card, one figure per line, in the order a decision needs them."""
        out = []
        title = f"**{self.symbol or UNKNOWN}**"
        if self.name and self.name.lower() != (self.symbol or "").lower():
            title += f"{SEP}{self.name}"
        out.append(title)

        out.append(footer(
            f"**{usd(self.mc)}** MC" if self.mc else "",
            f"liq {usd(self.liq)}" + (f" ({pct(self.depth_pct, signed=False)} of cap)" if self.depth_pct else ""),
            f"{self.venue}" if self.venue else "",
            f"quote {self.quote}" if self.quote else "",
            f"{age(self.age_sec)} old" if self.age_sec else "",
        ))

        if self.off_24h is not None:
            high_24 = self.mc_at(self.highs.high_24h)
            line = f"**{pct(self.off_24h)}** off its 24h high" + (f" ({usd(high_24)} MC)" if high_24 else "")
            if self.off_7d is not None and self.highs.high_7d != self.highs.high_24h:
                high_7 = self.mc_at(self.highs.high_7d)
                when = age(time.time() - self.highs.high_7d_ts) if self.highs.high_7d_ts else ""
                line += (f"{SEP}{pct(self.off_7d)} off its {self.high_label()} high"
                         + (f" ({usd(high_7)} MC" + (f", {when} ago)" if when else ")") if high_7 else ""))
            out.append(line)

        activity = footer(
            f"5m: **{plural(self.buyers_m5, 'buyer')}**" if self.buyers_m5 else "",
            f"{self.buys_m5} buys / {self.sells_m5} sells" if (self.buys_m5 or self.sells_m5) else "",
            f"1h: {pct(self.buyer_share_h1, signed=False)} of wallets buying" if self.buyer_share_h1 is not None else "",
            f"24h vol {usd(self.vol_h24)}" if self.vol_h24 else "",
        )
        if activity:
            out.append(activity)

        info = self.info
        if info is not None:
            out.append(footer(
                f"**{qty(info.holders)} holders**" if info.holders else "",
                f"top 10 hold {pct(info.top10_pct, signed=False)}" if info.top10_pct is not None else "",
                f"dev {pct(info.dev_pct, signed=False)}" if info.dev_pct else "",
                f"gt {info.gt_score:.0f}" if info.gt_score is not None else "",
                "no socials" if not info.socials else "",
                "⚠️ flagged a honeypot" if info.honeypot == "yes" else "",
            ))
        out.append(risk.links_line(info, self.ca))
        return [line for line in out if line]

    def text(self) -> str:
        return "\n".join(self.lines())

    def chart_png(self) -> Optional[bytes]:
        """The market cap over the hours behind it, drawn from the candles the
        highs read already fetched. None when the token is too young to have a
        shape, or when there is no market cap to draw it in."""
        highs = self.highs
        if not highs or not highs.series or not self.mc or not self.price:
            return None
        points = [(ts, close * self.mc / self.price) for ts, close in highs.series]
        return chart.render(points, high=self.mc_at(highs.high_7d))


async def build(ca: str, token: Optional[rhchain.TokenActivity] = None, *, deep: bool = True) -> Card:
    """Assemble the card. ``token`` is the board row when the caller has one,
    which spares a DexScreener read. ``deep=False`` skips the two GeckoTerminal
    requests (highs and holders) for callers that only want the market line."""
    card = Card(ca=ca)
    if token is not None:
        ref = token.reference
        card.symbol, card.name = token.symbol, token.name
        card.mc, card.price, card.liq = token.mc_usd, ref.price_usd, token.liq_usd
        card.depth_pct = token.depth()
        card.venue, card.quote = ref.dex, ref.quote_symbol
        card.age_sec = (time.time() - token.created_ts) if token.created_ts else None
        card.buyers_m5, card.buys_m5, card.sells_m5 = token.buyers("m5"), token.buys("m5"), token.sells("m5")
        card.buyer_share_h1 = token.buyer_share("h1")
        card.vol_h24 = token.volume("h24")
    else:
        summary: Optional[Dict[str, Any]] = None
        try:
            summary = await token_summary(ca)
        except Exception:
            log.debug("Card could not read %s from DexScreener", ca, exc_info=True)
        if summary:
            card.symbol, card.name = summary.get("symbol") or "", summary.get("name") or ""
            card.mc, card.price, card.liq = summary.get("mc"), summary.get("price"), summary.get("liq")
            if card.mc and card.liq:
                card.depth_pct = card.liq / card.mc * 100.0
            card.buys_m5, card.sells_m5 = int(summary.get("buys_m5") or 0), int(summary.get("sells_m5") or 0)
            card.vol_h24 = summary.get("vol24")
            created = summary.get("created_ts") or summary.get("pair_created_ts")
            card.age_sec = (time.time() - created) if created else None
    if deep:
        card.highs = await rhchain.price_highs(ca)
        card.info = await risk.token_info(ca)
    return card
