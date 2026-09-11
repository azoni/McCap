"""Learning from the feed's own calls.

The danger in a module like this is not that it computes the wrong number, it
is that it computes a confident number from four samples and quietly changes
what a channel of traders gets shown. So most of what is pinned here is what
it refuses to do: no evidence, no opinion; a thin bucket, no opinion; and
whatever the evidence says, it can reorder the queue and never overrule the
rules that put a candidate in it.
"""

import time

import pytest

from mccapbot import grading
from mccapbot.models import ScanEvent

NOW = 1_700_000_000.0
CA = "0x" + "ab" * 20
OLD = NOW - 2 * 3600          # long enough ago to be judged on price alone


def ev(*, mc=1_000_000.0, peak=None, last=None, ts=OLD, votes=None, signals=None, kind="spike", source="feed"):
    return ScanEvent(
        ca=CA, guild_id=1, channel_id=2, scanner_id=0, name="Tok", symbol="TOK",
        mc_at_scan=mc, ts=ts, peak_mc=peak if peak is not None else mc,
        last_mc=last if last is not None else mc, source=source, kind=kind,
        signals=signals if signals is not None else {"kind": kind, "pace": 6.0},
        votes=votes or {},
    )


@pytest.fixture(autouse=True)
def clean():
    grading.clear_cache()
    yield
    grading.clear_cache()


# ---------------- what a call was worth ----------------


def test_a_call_that_doubled_and_held_scores_well():
    assert grading.label(ev(peak=2_000_000.0, last=2_000_000.0), NOW) == pytest.approx(1.0)


def test_a_call_that_went_nowhere_scores_nothing_either_way():
    assert grading.label(ev(), NOW) == pytest.approx(0.0)


def test_a_call_that_halved_scores_against_the_feed():
    assert grading.label(ev(last=500_000.0), NOW) == pytest.approx(-1.0)


def test_a_call_that_doubled_then_round_tripped_is_a_wash():
    """It was tradeable and it ended badly. Both are true, and the number
    should not pretend one of them did not happen."""
    assert grading.label(ev(peak=2_000_000.0, last=1_000_000.0), NOW) == pytest.approx(1.0)
    assert grading.label(ev(peak=2_000_000.0, last=500_000.0), NOW) == pytest.approx(0.0)


def test_a_call_too_young_to_judge_is_not_judged():
    assert grading.label(ev(ts=NOW - 60), NOW) is None
    assert grading.label(ev(ts=NOW - grading.MIN_AGE_SECONDS - 1), NOW) is not None


def test_a_vote_does_not_wait_for_the_price():
    """Somebody who watched it happen knows things the market cap does not —
    a rug that has not dumped yet still looks fine on price."""
    fresh = ev(ts=NOW - 60, votes={"1": -1, "2": -1, "3": -1})
    assert grading.label(fresh, NOW) == pytest.approx(-1.0)


def test_votes_outweigh_the_price_when_both_are_there():
    """A tokenized stock that drifted up 5% reads as a fine call on price
    alone; three people saying otherwise should win."""
    both = ev(peak=1_500_000.0, last=1_500_000.0, votes={"1": -1, "2": -1, "3": -1})
    assert grading.label(both, NOW) == pytest.approx(0.6 * -1.0 + 0.4 * 0.5)
    assert grading.label(both, NOW) < 0


def test_taking_a_vote_back_leaves_no_trace():
    e = ev(votes={"1": 1})
    assert e.vote_score() == 1
    e.votes.pop("1")
    assert e.vote_score() == 0 and grading.label(e, NOW) == pytest.approx(0.0)


# ---------------- bucketing ----------------


def test_a_signal_lands_in_a_bucket_named_the_way_the_report_reads_it():
    assert grading.bucket("pace", 7.0) == "pace 5x-10x"
    assert grading.bucket("pace", 1.5) == "pace under 3x"
    assert grading.bucket("pace", 40.0) == "pace over 10x"
    assert grading.bucket("kind", "mover") == "kind mover"
    assert grading.bucket("stock", "yes") == "stock yes"


def test_a_signal_nobody_bins_is_not_invented_into_one():
    assert grading.bucket("weather", 3) is None
    assert grading.bucket("pace", "sunny") is None
    assert grading.bucket("kind", "") is None


def test_features_are_every_bucket_a_call_belongs_to():
    feats = grading.features({"kind": "spike", "pace": 12.0, "depth": 1.0, "nonsense": 4})
    assert set(feats) == {"kind spike", "pace over 10x", "depth under 2%"}


# ---------------- the verdicts ----------------


def test_a_bucket_learns_the_average_of_the_calls_in_it():
    events = [ev(peak=2_000_000.0, last=2_000_000.0, signals={"kind": "spike"}),
              ev(signals={"kind": "spike"}),
              ev(last=500_000.0, signals={"kind": "mover"})]
    table = grading.verdicts(events, NOW)
    assert table["kind spike"][0] == pytest.approx(0.5) and table["kind spike"][1] == 2
    assert table["kind mover"][0] == pytest.approx(-1.0) and table["kind mover"][1] == 1


def test_only_the_feeds_own_calls_with_signals_are_learned_from():
    """A scanner bot's post has no signal vector, so there is nothing about it
    to generalise from; grading it would teach the feed about someone else."""
    assert grading.verdicts([ev(source="")], NOW) == {}
    assert grading.verdicts([ev(signals={})], NOW) == {}
    assert grading.verdicts([ev(ts=NOW - grading.GRADE_WINDOW - 1)], NOW) == {}


# ---------------- what it is allowed to do about it ----------------


def test_nothing_learned_means_the_feed_ranks_exactly_as_it_always_did():
    assert grading.weight({"kind": "spike"}, {}) == 1.0
    assert grading.weight({}, {"kind spike": (1.0, 999, 0, 0)}) == 1.0


def test_a_bucket_with_too_few_calls_behind_it_counts_for_nothing():
    thin = {"kind spike": (1.0, grading.FEED_LEARN_MIN_SAMPLES - 1, 0, 0)}
    assert grading.weight({"kind": "spike"}, thin) == 1.0
    ready = {"kind spike": (1.0, grading.FEED_LEARN_MIN_SAMPLES, 0, 0)}
    assert grading.weight({"kind": "spike"}, ready) > 1.0


def test_evidence_can_move_a_candidate_but_never_take_over():
    n = grading.FEED_LEARN_MIN_SAMPLES
    great = {"kind spike": (99.0, n, 0, 0)}
    awful = {"kind spike": (-99.0, n, 0, 0)}
    assert grading.weight({"kind": "spike"}, great) == pytest.approx(grading.FEED_LEARN_MAX_WEIGHT)
    assert grading.weight({"kind": "spike"}, awful) == pytest.approx(1.0 / grading.FEED_LEARN_MAX_WEIGHT)
    assert grading.weight({"kind": "spike"}, awful) > 0, "a weight of zero would be a ban, not an opinion"


def test_an_attribute_we_know_nothing_about_is_neutral_never_a_bonus():
    """If a thin bucket simply dropped out of the average, the divisor would
    shrink and a token with an unproven attribute would outscore one with a
    known-harmless attribute — "no evidence yet" would read as "good"."""
    n = grading.FEED_LEARN_MIN_SAMPLES
    table = {"kind spike": (0.4, n, 0, 0), "pace over 10x": (0.4, n, 0, 0),
             "stock no": (0.0, n, 0, 0), "stock yes": (-0.9, n - 1, 0, 9)}
    known = grading.weight({"kind": "spike", "pace": 20.0, "stock": "no"}, table)
    unproven = grading.weight({"kind": "spike", "pace": 20.0, "stock": "yes"}, table)
    assert unproven <= known, (unproven, known)
    assert unproven == pytest.approx(1 + (0.4 + 0.4 + 0.0) / 3)


def test_a_candidates_buckets_are_averaged_not_multiplied():
    """Six signals all mildly positive should not compound into a landslide."""
    n = grading.FEED_LEARN_MIN_SAMPLES
    table = {f: (0.5, n, 0, 0) for f in ("kind spike", "pace over 10x", "depth 5%-15%")}
    got = grading.weight({"kind": "spike", "pace": 20.0, "depth": 8.0}, table)
    assert got == pytest.approx(1.5)
    assert grading.weight({"kind": "spike"}, table) == pytest.approx(1.5), "one bucket, same average"


def test_learning_can_be_switched_off_entirely(monkeypatch):
    monkeypatch.setattr(grading, "FEED_LEARN_ENABLE", False)
    ready = {"kind spike": (1.0, grading.FEED_LEARN_MIN_SAMPLES, 0, 0)}
    assert grading.weight({"kind": "spike"}, ready) == 1.0


def test_it_can_say_why_it_scored_something_the_way_it_did():
    n = grading.FEED_LEARN_MIN_SAMPLES
    table = {"kind mover": (-0.8, n, 1, 9), "pace under 3x": (0.2, 2, 0, 0)}
    said = grading.explain({"kind": "mover", "pace": 1.0}, table)
    assert said == [f"kind mover -0.80 over {n} calls"], "a thin bucket is not a reason"


# ---------------- the table cache ----------------


def test_the_table_is_not_rebuilt_for_every_candidate(monkeypatch):
    calls = []
    real = grading.verdicts
    monkeypatch.setattr(grading, "verdicts", lambda e, n=None: (calls.append(1), real(e, n))[1])
    events = [ev()]
    grading.table(events, NOW)
    grading.table(events, NOW + 1)
    assert len(calls) == 1
    grading.table(events, NOW + grading.TABLE_CACHE_SECONDS + 1)
    assert len(calls) == 2
    grading.table(events, NOW, force=True)
    assert len(calls) == 3, "a vote forces a rebuild so the next tick sees it"


# ---------------- the report ----------------


def test_the_report_says_what_it_is_scoring_on_and_what_it_is_still_waiting_for():
    n = grading.FEED_LEARN_MIN_SAMPLES
    events = [ev(last=400_000.0, signals={"kind": "mover"}) for _ in range(n)]
    events += [ev(peak=3_000_000.0, last=3_000_000.0, signals={"kind": "spike"})]
    text = "\n".join(grading.report(events, NOW))
    assert "kind mover" in text and "Scoring on" in text
    assert "Not enough yet" in text and "kind spike (1)" in text


def test_the_report_is_honest_about_having_nothing_to_say():
    assert "No feed calls" in "\n".join(grading.report([], NOW))
    fresh = [ev(ts=NOW - 60, signals={"kind": "spike"})]
    assert "Nothing learned yet" in "\n".join(grading.report(fresh, NOW))


def test_the_report_says_so_when_it_is_switched_off(monkeypatch):
    monkeypatch.setattr(grading, "FEED_LEARN_ENABLE", False)
    events = [ev(signals={"kind": "spike"}) for _ in range(grading.FEED_LEARN_MIN_SAMPLES)]
    assert "switched off" in "\n".join(grading.report(events, NOW))
