"""Edge cases in /mc_remove input handling and table rendering.

All found by auditing the command surface; none were covered before.
"""

import json

import pytest

from mccapbot.cogs.alerts import resolve_targets
from mccapbot.models import Reminder
from mccapbot.storage import _coerce
from mccapbot.tables import dwidth, fixed_table


def rem(name, target=1_000_000):
    return Reminder(
        ca=f"CA-{name}", target_mc=target, direction="above", channel_id=1,
        creator_id=1, guild_id=1, name=name, symbol=name.upper(),
    )


# ---------------- input that used to crash the command ----------------


@pytest.mark.parametrize("tok", ["²", "⁵", "٣", "½", "Ⅻ"])
def test_unicode_digit_lookalikes_do_not_raise(tok):
    """str.isdigit() is True for '²' but int('²') raises ValueError. Unhandled,
    that killed the handler after the defer and the interaction hung forever."""
    picked, errs = resolve_targets(tok, [rem("a")])
    assert picked == []
    assert errs, "should report a problem rather than raising"


def test_huge_index_does_not_raise():
    picked, errs = resolve_targets("9" * 400, [rem("a")])
    assert picked == [] and errs


@pytest.mark.parametrize("raw", ["   ", ",,,", " , , ", "\t"])
def test_blank_input_always_explains_itself(raw):
    """These used to produce a bare 'Nothing to remove:' with no reason."""
    picked, errs = resolve_targets(raw, [rem("a")])
    assert picked == []
    assert errs and all(e.strip() for e in errs)


def test_no_numbered_alerts_explains_why():
    """A scope holding only momentum alerts used to answer 'out of range (1–0)'."""
    picked, errs = resolve_targets("1", [])
    assert picked == []
    assert "id" in errs[0].lower(), errs


# ---------------- scope wording ----------------


def test_error_wording_follows_the_scope():
    scoped = [rem("a")]
    _, server = resolve_targets("deadbe", scoped, "in this server")
    _, account = resolve_targets("deadbe", scoped, "on your account")
    assert "in this server" in server[0]
    assert "on your account" in account[0]
    assert "server" not in account[0], "DM context must not claim 'server'"


# ---------------- still works ----------------


def test_normal_paths_unaffected():
    scoped = [rem("a"), rem("b"), rem("c")]
    assert [r.name for r in resolve_targets("2", scoped)[0]] == ["b"]
    assert [r.name for r in resolve_targets("1 3", scoped)[0]] == ["a", "c"]
    assert resolve_targets(scoped[1].id, scoped)[0] == [scoped[1]]


def test_ids_win_over_indices_for_ambiguous_hex():
    """A 6-digit numeric string is both a valid id shape and an index."""
    scoped = [rem("a"), rem("b")]
    scoped[1].id = "123456"
    picked, _ = resolve_targets("123456", scoped)
    assert picked == [scoped[1]], "an exact id match must take precedence"


# ---------------- storage: a falsy id must not survive ----------------


def test_explicit_null_id_gets_a_real_one():
    """_coerce only skips *absent* keys, so "id": null overrode the factory
    default. Removal is keyed on id, so blanks could collide."""
    r = _coerce(Reminder, {
        "ca": "CA-x", "target_mc": 1.0, "direction": "above", "channel_id": 1,
        "creator_id": 2, "guild_id": 3, "name": "X", "symbol": "X", "id": None,
    })
    assert r.id, "a null id should fall back to the generated default"


def test_explicit_empty_id_gets_a_real_one():
    r = _coerce(Reminder, {
        "ca": "CA-x", "target_mc": 1.0, "direction": "above", "channel_id": 1,
        "creator_id": 2, "guild_id": 3, "name": "X", "symbol": "X", "id": "",
    })
    assert r.id


def test_a_real_id_is_preserved():
    r = _coerce(Reminder, {
        "ca": "CA-x", "target_mc": 1.0, "direction": "above", "channel_id": 1,
        "creator_id": 2, "guild_id": 3, "name": "X", "symbol": "X", "id": "abc123",
    })
    assert r.id == "abc123"


def test_ids_are_unique_across_a_loaded_batch():
    recs = [{
        "ca": f"CA-{i}", "target_mc": 1.0, "direction": "above", "channel_id": 1,
        "creator_id": 2, "guild_id": 3, "name": "X", "symbol": "X", "id": None,
    } for i in range(20)]
    ids = [_coerce(Reminder, json.loads(json.dumps(r))).id for r in recs]
    assert len(set(ids)) == len(ids), "blank ids must not all collapse to one value"


# ---------------- wide characters must not break alignment ----------------


def test_dwidth_counts_display_cells():
    assert dwidth("BONK") == 4
    assert dwidth("模因季节") == 8, "CJK glyphs occupy two cells each"
    assert dwidth("币安Holder") == 10


def test_cjk_token_names_stay_aligned():
    """These names are in the real alert list, so this is not hypothetical."""
    headers = ["#", "Token", "Target"]
    rows = [
        ["1", "模因季节", "≥ $1.00M"],
        ["2", "BONK", "≥ $2.00M"],
        ["3", "币安Holder", "≥ $3.00M"],
        ["4", "笑哭猫", "≥ $4.00M"],
    ]
    table = fixed_table(headers, rows, ["r", "l", "r"])
    lines = [l for l in table.split("\n") if l and not l.startswith("```")]
    assert len({dwidth(l) for l in lines}) == 1, f"misaligned: {[dwidth(l) for l in lines]}"


def test_wide_text_is_clipped_by_display_width():
    out = fixed_table(["T"], [["模" * 20]], ["l"], max_width=10)
    body = [l for l in out.split("\n") if l and not l.startswith("```")][-1]
    assert dwidth(body) <= 10
    assert "…" in body
