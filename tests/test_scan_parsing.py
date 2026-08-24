"""Scan detection: the parser must never silently match nothing, and must
never react to McCap's own posts."""


import pytest

from mccapbot import scan
from mccapbot.models import ScanEvent

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WIF = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class Author:
    def __init__(self, id, bot=True):
        self.id = id
        self.bot = bot


class Button:
    def __init__(self, url=None, label=None):
        self.url = url
        self.label = label


class Row:
    def __init__(self, *children):
        self.children = list(children)


class Msg:
    """Minimal stand-in for discord.Message."""

    def __init__(self, content="", embeds=None, components=None, author=None):
        self.content = content
        self.embeds = embeds or []
        self.components = components or []
        self.author = author or Author(999)


# ---------------- extraction ----------------


def test_finds_mint_in_plain_content():
    assert scan.extract_mints(Msg(content=f"scanning {BONK} now")) == [BONK]


def test_finds_mint_in_embed_dict():
    m = Msg(embeds=[{"title": "BONK", "description": f"CA: {BONK}"}])
    assert BONK in scan.extract_mints(m)


def test_finds_mint_in_embed_fields():
    m = Msg(embeds=[{"fields": [{"name": "Contract", "value": f"`{BONK}`"}]}])
    assert BONK in scan.extract_mints(m)


def test_finds_mint_in_button_url():
    """Rick's trade quicklinks carry the mint even when the text doesn't."""
    m = Msg(components=[Row(Button(url=f"https://photon-sol.tinyastro.io/en/lp/{BONK}"))])
    assert BONK in scan.extract_mints(m)


def test_finds_mint_in_markdown_link_inside_description():
    m = Msg(embeds=[{"description": f"[chart](https://dexscreener.com/solana/{BONK})"}])
    assert BONK in scan.extract_mints(m)


def test_url_encoded_mint_is_decoded():
    m = Msg(embeds=[{"url": f"https://x.example/?a=%20{BONK}%20"}])
    assert BONK in scan.extract_mints(m)


# ---------------- the traps ----------------


def test_wsol_and_usdc_are_never_reported():
    """Every pool link contains a quote mint. Without filtering, the bot would
    'detect' wSOL on essentially every scan."""
    m = Msg(embeds=[{"description": f"pool {WSOL} / {USDC} for {BONK}"}])
    out = scan.extract_mints(m)
    assert WSOL not in out and USDC not in out
    assert out == [BONK]


def test_chart_link_mint_outranks_loose_text():
    """When several base58 runs appear, the one in a chart URL is the token."""
    m = Msg(
        content=f"noise {WIF} noise",
        embeds=[{"url": f"https://dexscreener.com/solana/{BONK}"}],
    )
    assert scan.extract_mints(m)[0] == BONK


def test_no_mint_returns_empty_not_garbage():
    """A message with nothing mint-shaped must return [], so the caller can
    log a format change instead of acting on nonsense."""
    m = Msg(content="gm everyone", embeds=[{"title": "hello", "description": "no tokens here"}])
    assert scan.extract_mints(m) == []


def test_short_base58_is_not_a_mint():
    assert scan.extract_mints(Msg(content="abc123 short")) == []


def test_evm_address_is_not_a_solana_mint():
    assert scan.extract_mints(Msg(content="0x37cc340fab73ff508c085558f611403810e24444")) == []


def test_duplicates_collapse():
    m = Msg(content=f"{BONK} {BONK}", embeds=[{"description": BONK}])
    assert scan.extract_mints(m) == [BONK]


# ---------------- who we listen to ----------------


def test_ignores_our_own_messages():
    """The loop guard. McCap's own alert embeds contain mints and chart links,
    so reacting to them would make it scan itself forever."""
    me = 4242
    m = Msg(content=BONK, author=Author(me))
    assert scan.is_scanner_message(m, scanner_ids=set(), self_id=me) is False
    assert scan.is_scanner_message(m, scanner_ids={me}, self_id=me) is False


def test_ignores_humans():
    m = Msg(content=BONK, author=Author(1, bot=False))
    assert scan.is_scanner_message(m, scanner_ids=set(), self_id=99) is False


def test_allowlist_restricts_to_named_scanner():
    rick, other = 111, 222
    assert scan.is_scanner_message(Msg(content=BONK, author=Author(rick)), {rick}, 99) is True
    assert scan.is_scanner_message(Msg(content=BONK, author=Author(other)), {rick}, 99) is False


def test_without_allowlist_any_bot_with_a_mint_counts():
    assert scan.is_scanner_message(Msg(content=BONK, author=Author(555)), set(), 99) is True


def test_without_allowlist_a_bot_chattering_is_ignored():
    assert scan.is_scanner_message(Msg(content="gm", author=Author(555)), set(), 99) is False


def test_surfaces_are_reportable():
    """Detection failures need to be diagnosable, so surfaces are countable."""
    m = Msg(content="x", embeds=[{"title": "t", "url": "https://e.example/"}])
    s = scan.scan_surfaces(m)
    assert s.texts and s.urls
    assert "text region" in s.describe() and "url" in s.describe()


# ---------------- scoring ----------------


def test_multiples():
    ev = ScanEvent(ca=BONK, guild_id=1, channel_id=2, scanner_id=3,
                   name="Bonk", symbol="BONK", mc_at_scan=100.0)
    ev.peak_mc, ev.last_mc = 250.0, 150.0
    assert ev.multiple() == pytest.approx(2.5)
    assert ev.current_multiple() == pytest.approx(1.5)


def test_multiples_none_without_an_anchor():
    """A scan with no market cap at detection can't be scored — it must say so
    rather than reporting a 0x or a 1x."""
    ev = ScanEvent(ca=BONK, guild_id=1, channel_id=2, scanner_id=3,
                   name="B", symbol="B", mc_at_scan=None)
    ev.peak_mc = 500.0
    assert ev.multiple() is None
    assert ev.current_multiple() is None


def test_multiples_none_before_first_check():
    ev = ScanEvent(ca=BONK, guild_id=1, channel_id=2, scanner_id=3,
                   name="B", symbol="B", mc_at_scan=100.0)
    assert ev.multiple() is None


def test_zero_anchor_does_not_divide_by_zero():
    ev = ScanEvent(ca=BONK, guild_id=1, channel_id=2, scanner_id=3,
                   name="B", symbol="B", mc_at_scan=0.0)
    ev.peak_mc = 10.0
    assert ev.multiple() is None
