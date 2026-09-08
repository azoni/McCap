"""Content tests for mccapbot.rhc.tutorial (pure functions, no Discord I/O)."""

import pytest

from mccapbot import config
from mccapbot.helpers import UNKNOWN, human_window, usd
from mccapbot.rhc import tutorial
from mccapbot.rhc.tutorial import DONE, TODO, TutorialState, build, next_step, row_state

ADDR = "0x" + "a1" * 20
TOPIC_VALUES = [v for v, _ in tutorial.TOPICS]


def _state(**kw) -> TutorialState:
    base = dict(display_name="gambeezy", allowed=True, has_wallet=True, address=ADDR,
                balance_wei=10**17, eth_usd=3000.0, traded=False)
    base.update(kw)
    return TutorialState(**base)


def _text(embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    parts += [f"{f.name}\n{f.value}" for f in embed.fields]
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(parts)


def _field(embed, name):
    for f in embed.fields:
        if f.name.startswith(name):
            return f.value
    raise AssertionError(f"no field starting with {name!r}: {[f.name for f in embed.fields]}")


# ---------------- shape ----------------

def test_topics_are_the_six_choices_in_order():
    assert TOPIC_VALUES == ["start", "buy", "sell", "buttons", "auto", "safety"]
    assert all(label for _, label in tutorial.TOPICS)


@pytest.mark.parametrize("topic", TOPIC_VALUES)
@pytest.mark.parametrize("personal", [True, False])
def test_every_topic_builds_within_discord_limits(topic, personal):
    e = build(topic, _state(personal=personal, remaining_usd=120.0, gate_reason="paused"))
    assert e.title == "How McCap trading works"
    assert e.colour.value == tutorial.NEUTRAL
    assert len(e) <= 6000
    assert e.description and len(e.description) <= 4096
    for f in e.fields:
        assert len(f.name) <= 256
        assert 0 < len(f.value) <= 1024
    assert "Quotes, refusals and anything about your keys are only ever shown to you" in e.footer.text
    assert "/help lists every command" in e.footer.text


def test_unknown_topic_falls_back_to_start():
    assert _text(build("nonsense", _state())) == _text(build("start", _state()))


def test_gate_footer_only_when_paused():
    plain = build("start", _state()).footer.text
    paused = build("start", _state(gate_reason="kill switch")).footer.text
    assert "trading is paused right now" not in plain
    assert paused.endswith("trading is paused right now")


# ---------------- start checklist ----------------

def test_checklist_no_wallet():
    st = _state(has_wallet=False, address="", balance_wei=None)
    lines = _field(build("start", st), "Your checklist").splitlines()
    assert lines[0].startswith(DONE) and "allowlist" in lines[0]
    assert lines[1].startswith(TODO) and "/rh wallet create" in lines[1]
    assert lines[2].startswith(TODO) and "Funded" in lines[2]
    assert lines[3].startswith(TODO) and "First trade" in lines[3]
    assert "/rh wallet create" in next_step(st)


def test_checklist_unfunded_shows_address():
    st = _state(balance_wei=0)
    lines = _field(build("start", st), "Your checklist").splitlines()
    assert lines[1].startswith(DONE)
    assert lines[2].startswith(TODO) and ADDR in lines[2]
    assert lines[3].startswith(TODO)
    assert "Send some ETH" in next_step(st)


def test_checklist_funded_not_traded():
    st = _state(balance_wei=10**17)
    lines = _field(build("start", st), "Your checklist").splitlines()
    assert lines[2].startswith(DONE) and "0.1 ETH" in lines[2] and usd(300) in lines[2]
    assert lines[3].startswith(TODO)
    assert "/rh buy" in next_step(st)


def test_checklist_traded():
    st = _state(traded=True)
    lines = _field(build("start", st), "Your checklist").splitlines()
    assert all(line.startswith(DONE) for line in lines)
    assert "/rh holdings" in next_step(st)


def test_unreadable_balance_is_unknown_never_funded():
    st = _state(balance_wei=None)
    line = _field(build("start", st), "Your checklist").splitlines()[2]
    assert line.startswith(TODO)
    assert f"balance {UNKNOWN}" in line


def test_not_allowed_checklist_and_next():
    st = _state(allowed=False, has_wallet=False, balance_wei=None)
    lines = _field(build("start", st), "Your checklist").splitlines()
    assert lines[0].startswith(TODO)
    assert "allowlist" in next_step(st)


def test_next_field_matches_next_step():
    st = _state(balance_wei=0)
    assert _field(build("start", st), "Next") == next_step(st)


def test_start_has_buttons_field_with_permission_sentence():
    v = _field(build("start", _state()), "Buttons")
    assert "answer only to you" in v
    assert "act on whoever clicks" in v
    assert "keep working after McCap restarts" in v


# ---------------- public variant ----------------

def test_public_start_has_no_personal_data():
    st = _state(personal=False, remaining_usd=120.0, traded=True)
    e = build("start", st)
    text = _text(e)
    assert ADDR not in text
    assert "gambeezy" not in text
    assert DONE not in text and TODO not in text
    assert "ETH**" not in text  # no balance line
    assert "allowlist-only" in text
    assert _field(e, "The four steps").count("\n") == 3


def test_public_buy_hides_remaining_cap():
    priv = _text(build("buy", _state(remaining_usd=120.0)))
    pub = _text(build("buy", _state(remaining_usd=120.0, personal=False)))
    assert f"{usd(120.0)}** left today" in priv
    assert "left today" not in pub


# ---------------- row_state ----------------

@pytest.mark.parametrize("personal,allowed,gate,wallet,expect", [
    (True, True, None, False, "no_wallet"),
    (True, True, None, True, "wallet"),
    (True, True, "paused", False, "none"),
    (True, True, "paused", True, "none"),
    (True, False, None, False, "none"),
    (True, False, None, True, "none"),
    (False, True, None, True, "public"),
    (False, False, "paused", False, "public"),
])
def test_row_state(personal, allowed, gate, wallet, expect):
    st = _state(personal=personal, allowed=allowed, gate_reason=gate, has_wallet=wallet)
    assert row_state(st) == expect


# ---------------- config figures ----------------

def test_buy_topic_carries_config_figures():
    text = _text(build("buy", _state()))
    assert f"**{usd(config.RHC_MAX_TRADE_USD)}** per buy" in text
    assert f"**{usd(config.RHC_MAX_DAILY_USD)}** per day" in text
    assert f"**{human_window(config.RHC_CONFIRM_TIMEOUT)}** to press Confirm" in text
    assert "honeypot" in text
    assert "Slippage" in text and "2%" in text


def test_sell_topic():
    text = _text(build("sell", _state()))
    assert "2.1x" in text and "$250K → $525K MC" in text
    assert "sells never are" in text
    assert usd(config.RHC_MAX_TRADE_USD) in text
    assert "Sell 25%" in text and "TP / SL" in text


def test_buttons_topic_lists_sizes_from_config():
    text = _text(build("buttons", _state()))
    for size in config.RHC_BUTTON_USD_SIZES:
        if size <= config.RHC_MAX_TRADE_USD:
            # The tutorial prints the figure the way the Buy button does: "$5", not "$5.00".
            money = usd(size)
            assert (money[:-3] if money.endswith(".00") else money) in text
    assert "answer only to you" in text
    assert human_window(config.RHC_CONFIRM_TIMEOUT) in text
    assert "carries no buttons" in text


def test_auto_topic_is_honest_about_firing():
    text = _text(build("auto", _state()))
    assert "without asking again" in text
    assert f"**{config.RHC_AUTO_SELL_TTL}** for sells" in text
    assert f"**{config.RHC_AUTO_BUY_TTL}** for buys" in text
    assert f"**{config.RHC_AUTO_MAX_TTL}** at most" in text
    assert "/rh auto sell PONS percent:50 at:2x" in text
    assert "/rh auto buy PONS usd:10 condition:<Market cap at or below> value:200k" in text
    assert "/rh auto list" in text and "/rh auto cancel <id>" in text
    assert "never widens" in text
    assert "10–60s" in text


def test_safety_topic_has_custody_warning_verbatim():
    text = _text(build("safety", _state()))
    assert tutorial.CUSTODY_WARNING in text
    assert "seed phrase" in text
    assert usd(config.RHC_MAX_TRADE_USD) in text and usd(config.RHC_MAX_DAILY_USD) in text
    assert "KyberSwap" in text


def test_custody_warning_matches_the_cog_today():
    from mccapbot.cogs import rhc as cog
    assert cog.CUSTODY_WARNING == tutorial.CUSTODY_WARNING


def test_no_banned_style_tokens():
    for topic in TOPIC_VALUES:
        for personal in (True, False):
            text = _text(build(topic, _state(personal=personal, remaining_usd=5.0)))
            assert "•" not in text
            assert "(s)" not in text
