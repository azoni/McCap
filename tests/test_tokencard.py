"""The token card, the chart under it, and the signals both are written from.

What matters here is that a figure McCap cannot get is left out rather than
guessed at, that past prices are restated as market caps against the series
they came from, and that a chart is never the thing that breaks a card: no
candles, no market cap, no matplotlib — the buttons still arrive.
"""

import time
from types import SimpleNamespace

import pytest

from mccapbot import chart, rhchain, tokencard
from mccapbot.rhc import risk

CA = "0x" + "ab" * 20
NOW = 1_700_000_000.0


def highs(**kw):
    base = dict(price_now=1.0, high_24h=1.5, high_7d=2.0, high_7d_ts=NOW - 86_400, hours=34,
                series=[(NOW - (34 - i) * 3600, 1.0 + i * 0.02) for i in range(34)])
    base.update(kw)
    return rhchain.Highs(**base)


def card(**kw):
    base = dict(ca=CA, symbol="PONS", name="Pons", mc=400_000_000.0, price=1.0, liq=20_000_000.0,
                depth_pct=5.0, venue="Uniswap V4", quote="USDG", age_sec=5 * 86400,
                buyers_m5=15, buys_m5=23, sells_m5=13, buyer_share_h1=47.0, vol_h24=119_000_000.0,
                highs=highs())
    base.update(kw)
    return tokencard.Card(**base)


# ---------------- the numbers ----------------


def test_a_past_price_is_restated_as_a_market_cap_against_its_own_series():
    """The high comes from GeckoTerminal candles, so it is converted against
    that series' last close — not against a listing price from somewhere else."""
    c = card(price=99.0)                                    # a listing price that disagrees
    assert c.mc_at(2.0) == pytest.approx(800_000_000.0), "2.0 is 2x the series' last close"
    assert c.mc_at(None) is None and c.mc_at(0.0) is None


def test_a_card_with_no_highs_falls_back_to_the_price_it_was_built_from():
    c = card(highs=None, price=2.0, mc=100.0)
    assert c.mc_at(1.0) == pytest.approx(50.0)
    assert card(highs=None, price=None).mc_at(1.0) is None


def test_the_high_label_never_claims_a_week_it_does_not_have():
    assert card(highs=highs(hours=34)).high_label() == "34h"
    assert card(highs=highs(hours=168)).high_label() == "7d"
    assert card(highs=None).high_label() == "0h"


def test_the_card_reads_as_one_figure_per_line():
    text = card().text()
    assert text.startswith("**PONS**")
    assert "**$400M** MC" in text and "liq $20M (5% of cap)" in text
    assert "-50%" in text and "off its 34h high" in text, "against the 7d high, 2.0 vs 1.0"
    assert "5m: **15 buyers**" in text and "23 buys / 13 sells" in text
    assert "24h vol $119M" in text


def test_a_card_that_knows_almost_nothing_still_renders():
    """Every lookup can fail at once. What survives is the symbol and a line of
    links — never a row of dashes pretending to be data."""
    text = tokencard.Card(ca=CA, symbol="NEW").text()
    assert text.startswith("**NEW**")
    assert "MC" not in text and "off its" not in text
    assert "[Explorer](" in text and CA in text


def test_holder_context_appears_only_when_geckoterminal_had_it():
    c = card(info=risk.TokenInfo(holders=90_097, top10_pct=58.9, gt_score=94.0, socials=["x"]))
    assert "**90,097 holders**" in c.text() and "top 10 hold 58.9%" in c.text() and "gt 94" in c.text()
    assert "holders" not in card(info=risk.TokenInfo()).text()


def test_a_honeypot_flag_is_said_out_loud():
    assert "⚠️ flagged a honeypot" in card(info=risk.TokenInfo(honeypot="yes")).text()


# ---------------- the chart ----------------


def test_the_chart_is_drawn_from_the_candles_the_highs_read_already_paid_for():
    png = card().chart_png()
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_no_market_cap_and_no_candles_mean_no_chart_not_a_broken_one():
    assert card(mc=None).chart_png() is None
    assert card(highs=highs(series=[])).chart_png() is None
    assert card(highs=None).chart_png() is None


def test_a_pair_too_young_to_have_a_shape_gets_no_chart():
    young = highs(series=[(NOW - 3600, 1.0), (NOW, 1.1)])
    assert card(highs=young).chart_png() is None
    assert len(young.series) < chart.MIN_POINTS


def test_the_chart_refuses_junk_points_rather_than_drawing_them():
    assert chart.render([]) is None
    assert chart.render([(NOW, 0.0)] * 20) is None, "a flat zero is not a price"
    assert chart.render([(NOW + i, None) for i in range(20)]) is None
    mixed = [(NOW + i * 3600, 1.0 + i) for i in range(8)] + [("bad", "worse")]
    assert chart.render(mixed)


def test_a_chart_is_never_what_breaks_a_card(monkeypatch):
    """matplotlib is the one import here that could be missing on a host."""
    import builtins
    real = builtins.__import__

    def no_matplotlib(name, *a, **kw):
        if name.startswith("matplotlib"):
            raise ImportError("no matplotlib on this host")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_matplotlib)
    assert card().chart_png() is None
    assert card().text().startswith("**PONS**"), "the numbers are unaffected"


def test_the_span_label_says_what_the_line_actually_covers():
    assert chart._span(3600) == "1h" and chart._span(34 * 3600) == "34h"
    assert chart._span(7 * 86400) == "7d" and chart._span(60 * 3600) == "2d"


# ---------------- assembly ----------------


@pytest.mark.asyncio
async def test_build_prefers_the_board_row_it_was_given_over_a_fresh_read(monkeypatch):
    """A picker already holds the listing row; paying DexScreener again for it
    would be a request spent on something McCap has in hand."""
    asked = []
    monkeypatch.setattr(tokencard, "token_summary", lambda ca: asked.append(ca))

    async def no_highs(ca, *a, **kw):
        return None

    async def no_info(ca, *a, **kw):
        return None

    monkeypatch.setattr(rhchain, "price_highs", no_highs)
    monkeypatch.setattr(risk, "token_info", no_info)
    pool = SimpleNamespace(price_usd=2.0, dex="Uniswap V4", quote_symbol="USDG")
    row = SimpleNamespace(
        symbol="PONS", name="Pons", mc_usd=400.0, liq_usd=20.0, created_ts=time.time() - 600,
        reference=pool, depth=lambda: 5.0, buyers=lambda w: 15, buys=lambda w: 23, sells=lambda w: 13,
        buyer_share=lambda w: 47.0, volume=lambda w: 119.0,
    )
    built = await tokencard.build(CA, row)
    assert asked == [], "no DexScreener read when the row was handed over"
    assert built.symbol == "PONS" and built.venue == "Uniswap V4" and built.price == 2.0
    assert built.buyers_m5 == 15 and built.depth_pct == 5.0


@pytest.mark.asyncio
async def test_deep_false_skips_the_two_geckoterminal_reads(monkeypatch):
    calls = []

    async def note_highs(ca, *a, **kw):
        calls.append("highs")

    async def note_info(ca, *a, **kw):
        calls.append("info")

    monkeypatch.setattr(rhchain, "price_highs", note_highs)
    monkeypatch.setattr(risk, "token_info", note_info)

    async def summary(ca):
        return {"symbol": "X", "mc": 100.0, "price": 1.0, "liq": 10.0}

    monkeypatch.setattr(tokencard, "token_summary", summary)
    built = await tokencard.build(CA, deep=False)
    assert calls == [] and built.symbol == "X" and built.depth_pct == pytest.approx(10.0)
    await tokencard.build(CA, deep=True)
    assert calls == ["highs", "info"]


@pytest.mark.asyncio
async def test_a_dead_dexscreener_leaves_a_card_with_its_address_and_nothing_invented(monkeypatch):
    async def boom(ca):
        raise RuntimeError("DexScreener is down")

    async def none(ca, *a, **kw):
        return None

    monkeypatch.setattr(tokencard, "token_summary", boom)
    monkeypatch.setattr(rhchain, "price_highs", none)
    monkeypatch.setattr(risk, "token_info", none)
    built = await tokencard.build(CA)
    assert built.ca == CA and built.mc is None and built.symbol == ""
    assert CA in built.text()


def test_a_high_far_above_the_line_is_left_off_rather_than_flattening_it():
    """A token down 90% would have its whole shape squashed into the bottom
    tenth of the panel just to fit the dashed line in."""
    points = [(NOW + i * 3600, 1.0 + (i % 3)) for i in range(12)]      # tops out at 3.0
    assert chart.render(points, high=4.0), "close enough to mark"
    assert chart.render(points, high=400.0), "still drawn, just without the line"
    assert chart.HIGH_LINE_MAX == 3.0
