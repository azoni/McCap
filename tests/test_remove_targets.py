"""`/mc_remove` used to key off list positions, which shifted when alerts fired."""

from mccapbot.cogs.alerts import resolve_targets
from mccapbot.models import Reminder


def mk(name, target=1_000_000):
    return Reminder(
        ca=f"CA-{name}",
        target_mc=target,
        direction="above",
        channel_id=1,
        creator_id=1,
        guild_id=1,
        name=name,
        symbol=name.upper(),
    )


def test_resolve_by_index():
    scoped = [mk("a"), mk("b"), mk("c")]
    picked, errs = resolve_targets("2", scoped)
    assert [r.name for r in picked] == ["b"]
    assert errs == []


def test_resolve_bulk_indices():
    scoped = [mk("a"), mk("b"), mk("c"), mk("d"), mk("e")]
    picked, errs = resolve_targets("1 3 5", scoped)
    assert [r.name for r in picked] == ["a", "c", "e"]
    assert errs == []


def test_resolve_by_stable_id():
    scoped = [mk("a"), mk("b"), mk("c")]
    picked, errs = resolve_targets(scoped[1].id, scoped)
    assert picked == [scoped[1]]
    assert errs == []


def test_ids_survive_a_shifting_list():
    """The actual race: an alert fires between /mc_list and /mc_remove.

    Position 2 now points at a different alert, but the id still resolves to
    the one the user picked.
    """
    scoped = [mk("a"), mk("b"), mk("c")]
    wanted = scoped[2]  # "c", shown as #3
    wanted_id = wanted.id

    # "a" fires and is removed; everything shifts up one slot.
    shifted = scoped[1:]

    by_index, _ = resolve_targets("3", shifted)
    assert by_index == [], "index 3 no longer exists after the shift"

    by_id, errs = resolve_targets(wanted_id, shifted)
    assert by_id == [wanted], "id must still resolve to the intended alert"
    assert errs == []


def test_missing_id_reports_error_rather_than_removing_wrong_alert():
    scoped = [mk("a"), mk("b")]
    picked, errs = resolve_targets("deadbe", scoped)
    assert picked == []
    assert len(errs) == 1 and "deadbe" in errs[0]


def test_dedupes_and_mixes_ids_with_indices():
    scoped = [mk("a"), mk("b"), mk("c")]
    # Index 2 and b's id refer to the same alert.
    picked, errs = resolve_targets(f"2 {scoped[1].id} 3", scoped)
    assert [r.name for r in picked] == ["b", "c"]
    assert errs == []


def test_out_of_range_and_garbage():
    scoped = [mk("a")]
    picked, errs = resolve_targets("9 zzz", scoped)
    assert picked == []
    assert len(errs) == 2


def test_empty_input():
    picked, errs = resolve_targets("   ", [mk("a")])
    assert picked == []
    assert errs == ["No alerts specified."]


def test_comma_separated_input():
    scoped = [mk("a"), mk("b"), mk("c")]
    picked, _ = resolve_targets("1,3", scoped)
    assert [r.name for r in picked] == ["a", "c"]
