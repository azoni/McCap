"""Discord rejects an embed field over 1024 chars with a 400, which fails the
whole interaction. /mc_list was broken in production this way: 39 level alerts
rendered to 2262 chars, so the command just failed for anyone with a busy
server. These tests exist so no table command can regress into that again.
"""

import pytest

from mccapbot.tables import (
    ALERTS_ALIGNS,
    ALERTS_HEADERS,
    EMBED_FIELD_LIMIT,
    _normalise_aligns,
    _render,
    _widths,
    add_table_fields,
    alerts_rows,
    table_chunks,
)


class FakeEmbed:
    """Captures fields the way discord.Embed would receive them."""

    def __init__(self):
        self.fields = []

    def add_field(self, name, value, inline=False):
        self.fields.append((name, value))


def render_all(headers, rows, aligns, max_width=18):
    """Render chunks exactly as add_table_fields does, for measurement."""
    a = _normalise_aligns(len(headers), aligns)
    w = _widths(headers, rows, max_width)
    return [_render(headers, g, a, w) for g in table_chunks(headers, rows, aligns, max_width)]


# ---------------- the shapes each command renders ----------------

MC_LIST = (["#", "ID", "Token", "Target", "Current", "By"], ["r", "l", "l", "r", "r", "l"])
MOMENTUM = (["ID", "Token", "Trigger", "Window", "Now", "By"], ["l", "l", "r", "r", "r", "l"])
WATCH = (["Token", "MC", "24h", "Liq"], ["l", "r", "r", "r"])
SCANS = (["Token", "At scan", "Peak", "Now"], ["l", "r", "r", "r"])


def mc_row(i):
    return [str(i + 1), "a1b2c3", "LONGSYMBOL", "≥ $123.45M", "$99.99M", "charltonuw"]


@pytest.mark.parametrize("n", [1, 5, 18, 25, 40, 100, 400])
def test_mc_list_chunks_always_fit(n):
    headers, aligns = MC_LIST
    for chunk in render_all(headers, [mc_row(i) for i in range(n)], aligns):
        assert len(chunk) <= EMBED_FIELD_LIMIT, f"{n} rows produced a {len(chunk)}-char field"


@pytest.mark.parametrize("shape", [MC_LIST, MOMENTUM, WATCH, SCANS])
@pytest.mark.parametrize("n", [1, 20, 50, 200])
def test_every_table_shape_fits(shape, n):
    headers, aligns = shape
    rows = [["WWWWWWWWWWWW"] * len(headers) for _ in range(n)]
    for chunk in render_all(headers, rows, aligns):
        assert len(chunk) <= EMBED_FIELD_LIMIT


def test_the_exact_production_regression():
    """39 breakout alerts — the live case that was failing."""
    headers, aligns = MC_LIST
    rows = [mc_row(i) for i in range(39)]

    # One table would have been rejected outright.
    a = _normalise_aligns(len(headers), aligns)
    single = _render(headers, rows, a, _widths(headers, rows, 18))
    assert len(single) > EMBED_FIELD_LIMIT, "the regression case should be over the limit"

    embed = FakeEmbed()
    shown, total = add_table_fields(embed, "Breakouts", headers, rows, aligns, max_fields=4)
    assert shown == total == 39, "no alert may be dropped"
    assert all(len(v) <= EMBED_FIELD_LIMIT for _, v in embed.fields)
    assert len(embed.fields) > 1, "should have split"


# ---------------- chunking invariants ----------------


@pytest.mark.parametrize("n", [0, 1, 17, 18, 39, 200])
def test_no_rows_are_lost(n):
    headers, aligns = MC_LIST
    rows = [mc_row(i) for i in range(n)]
    assert sum(len(g) for g in table_chunks(headers, rows, aligns)) == n


def test_columns_align_across_chunks():
    """Widths are computed once over all rows, so chunks must share a header."""
    headers, aligns = MC_LIST
    rows = [mc_row(i) for i in range(60)]
    chunks = render_all(headers, rows, aligns)
    assert len(chunks) > 1
    header_lines = {c.split("\n")[1] for c in chunks}
    assert len(header_lines) == 1, "chunks sized independently would misalign"


def test_a_single_oversized_row_still_emits():
    """A row longer than the budget must not loop forever or vanish."""
    headers = ["A"]
    rows = [["x" * 5000]]
    groups = table_chunks(headers, rows, ["l"])
    assert len(groups) == 1 and len(groups[0]) == 1


def test_empty_rows_render_once():
    headers, aligns = MC_LIST
    assert table_chunks(headers, [], aligns) == [[]]
    embed = FakeEmbed()
    assert add_table_fields(embed, "Empty", headers, [], aligns) == (0, 0)
    assert len(embed.fields) == 1


# ---------------- truncation must be visible ----------------


def test_overflow_is_reported_not_hidden():
    """Showing a subset silently would read as 'that is everything'."""
    headers, aligns = MC_LIST
    rows = [mc_row(i) for i in range(1000)]
    embed = FakeEmbed()
    shown, total = add_table_fields(embed, "Big", headers, rows, aligns, max_fields=3)
    assert total == 1000
    assert shown < total, "should have dropped rows"
    assert shown > 0
    assert len(embed.fields) == 3
    assert all(len(v) <= EMBED_FIELD_LIMIT for _, v in embed.fields)


def test_field_count_is_respected():
    headers, aligns = MC_LIST
    rows = [mc_row(i) for i in range(500)]
    for cap in (1, 2, 5):
        embed = FakeEmbed()
        add_table_fields(embed, "X", headers, rows, aligns, max_fields=cap)
        assert len(embed.fields) == cap


def test_continuation_fields_are_labelled():
    headers, aligns = MC_LIST
    embed = FakeEmbed()
    add_table_fields(embed, "Breakouts", headers, [mc_row(i) for i in range(40)], aligns)
    names = [n for n, _ in embed.fields]
    assert names[0] == "Breakouts"
    assert all("cont." in n for n in names[1:])


# ---------------- /mc_recent, which allows count up to 50 ----------------


class Ev:
    ts = 1_700_000_000.0
    ca = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    name = "TokenName"
    symbol = "LONGSYM"
    direction = "above"
    target_mc = 12_345_678.0
    current_mc = 9_999_999.0
    creator_id = 2


@pytest.mark.parametrize("n", [5, 20, 50])
def test_mc_recent_fits_at_its_maximum_count(n):
    rows = alerts_rows([Ev()] * n, {2: "charltonuw"}, {Ev.ca: 9_999_999.0})
    embed = FakeEmbed()
    shown, total = add_table_fields(
        embed, "History", ALERTS_HEADERS, rows, ALERTS_ALIGNS, max_width=14, max_fields=5
    )
    assert shown == total == n, "the documented max count must render fully"
    assert all(len(v) <= EMBED_FIELD_LIMIT for _, v in embed.fields)
