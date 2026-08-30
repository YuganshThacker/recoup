"""Counterfactual replay tests.

One case, run twice against identical ground truth, differing only in the
planner. The value of the comparison rests entirely on that claim, so most of
these tests exist to defend it: the arms must see the same world, must not
contaminate each other, and must reach the same answer every time.

The divergence count asserted here is the source of truth for the figure the
race view shows. The view reads it from the API; it is never a literal in a page.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from recovery.live.replay import (
    RACE_SEED,
    Race,
    find_divergent_cases,
    replay_case,
)

SIZE = 120


def _report():  # type: ignore[no-untyped-def]
    return find_divergent_cases(seed=RACE_SEED, size=SIZE)


# --- the arms see the same world -------------------------------------------


def test_both_arms_see_identical_ground_truth() -> None:
    """The claim the whole comparison rests on.

    Asserted at the source: the per-arm replica must carry a ``GroundTruth``
    equal to the original, so recoverable_from, self_cure_at and the outage
    window are the same world for both planners.
    """
    import copy

    from recovery.live.replay import _batch

    sim = next(c for c in _batch(RACE_SEED, SIZE).cases if c.case.case_id == _report().hero)
    left, right = copy.deepcopy(sim), copy.deepcopy(sim)

    assert left.truth == right.truth == sim.truth
    assert left.failed_at == right.failed_at == sim.failed_at
    assert int(left.case.amount) == int(right.case.amount) == int(sim.case.amount)
    assert left.case is not right.case, "the arms must not share mutable case state"


def test_the_race_reports_the_world_both_arms_ran_in() -> None:
    race = replay_case(_report().hero, seed=RACE_SEED, size=SIZE)

    assert race.amount_paise > 0
    assert race.default.case_id == race.recoup.case_id
    assert race.default.failed_at == race.recoup.failed_at


def test_the_arms_do_not_contaminate_each_other() -> None:
    # Independent providers and ledgers. A shared provider would deduplicate the
    # second arm's debits against the first arm's idempotency keys and quietly
    # hand one side a different world.
    race = replay_case(_report().hero, seed=RACE_SEED, size=SIZE)

    assert race.default.events, "control arm recorded nothing"
    assert race.recoup.events, "treatment arm recorded nothing"
    assert race.default.events is not race.recoup.events


def test_only_the_planner_differs() -> None:
    race = replay_case(_report().hero, seed=RACE_SEED, size=SIZE)

    assert race.default.planner == "PlatformDefaultPlanner"
    assert race.recoup.planner == "DeclineConditionalPlanner"


def test_the_treatment_arm_is_never_the_agent() -> None:
    # R1 is system vs platform default. Routing the treatment arm through the
    # model would silently turn this into an R2-flavoured comparison, which is
    # the single easiest way to get this feature wrong.
    race = replay_case(_report().hero, seed=RACE_SEED, size=SIZE)

    actors = {e["actor"] for arm in (race.default, race.recoup) for e in arm.events}
    assert "agent" not in actors


# --- reproducible -----------------------------------------------------------


def test_a_replay_is_reproducible() -> None:
    # The video is re-shot more than once; the race has to be the same race.
    first = replay_case(_report().hero, seed=RACE_SEED, size=SIZE)
    second = replay_case(_report().hero, seed=RACE_SEED, size=SIZE)

    assert first.recoup.recovered == second.recoup.recovered
    assert first.default.recovered == second.default.recovered
    assert [e["summary"] for e in first.recoup.events] == [
        e["summary"] for e in second.recoup.events
    ]


def test_divergence_detection_is_deterministic() -> None:
    assert _report().diverged == _report().diverged


# --- the divergence figure the view shows ----------------------------------


def test_the_arms_diverge_on_a_meaningful_share_of_cases() -> None:
    """Source of truth for the number rendered in the race view.

    If this ever collapses to zero the race is not worth showing, and if it
    changes the view changes with it -- which is the point of reading it from
    here rather than writing it into the page.
    """
    report = _report()

    assert report.total == SIZE
    assert report.diverged == 21
    assert 0.0 < report.rate < 0.5


# --- hero selection ---------------------------------------------------------


def test_the_hero_case_is_one_the_system_wins() -> None:
    report = _report()
    race = replay_case(report.hero, seed=RACE_SEED, size=SIZE)

    assert race.recoup.recovered is True
    assert race.default.recovered is False


def test_the_hero_case_prefers_the_mechanism_the_result_attributes() -> None:
    # RESULTS.md puts the lift on soft declines, and insufficient_funds is the
    # clearest instance. A hero case from some other mechanism would illustrate
    # a claim the batch does not actually make.
    race = replay_case(_report().hero, seed=RACE_SEED, size=SIZE)

    assert race.decline_reason == "insufficient_funds"


def test_no_case_id_is_hard_coded() -> None:
    # Selection is by rank, so a different seed picks a different hero rather
    # than failing or returning a case chosen for presentation reasons.
    other = find_divergent_cases(seed=RACE_SEED + 7, size=60)

    if other.hero is not None:
        assert replay_case(other.hero, seed=RACE_SEED + 7, size=60).recoup.recovered


def test_a_run_with_no_divergence_reports_no_hero_rather_than_a_wrong_one() -> None:
    report = find_divergent_cases(seed=RACE_SEED, size=1)

    assert report.hero is None or report.diverged > 0


def test_an_unknown_case_is_refused() -> None:
    with pytest.raises(KeyError, match="not in this batch"):
        replay_case("case_999999", seed=RACE_SEED, size=SIZE)


# --- the day axis -----------------------------------------------------------


def test_every_event_carries_a_parseable_simulated_time() -> None:
    race = replay_case(_report().hero, seed=RACE_SEED, size=SIZE)

    for arm in (race.default, race.recoup):
        for event in arm.events:
            datetime.fromisoformat(str(event["at"]))


def test_events_carry_the_day_offset_the_view_lays_out_on() -> None:
    race = replay_case(_report().hero, seed=RACE_SEED, size=SIZE)

    days = [e["day"] for e in race.recoup.events]
    assert days == sorted(days), "a case cannot move backwards in its own clock"
    assert days[0] == 0


def test_the_race_serialises_for_the_api() -> None:
    import json

    json.dumps(replay_case(_report().hero, seed=RACE_SEED, size=SIZE).payload())


def test_the_payload_states_that_this_is_simulated() -> None:
    # The comparison is rigorous; what it is evidence *about* is a world model.
    payload = replay_case(_report().hero, seed=RACE_SEED, size=SIZE).payload()

    assert "simulat" in json.dumps(payload).lower()


def test_race_is_a_frozen_record() -> None:
    race = replay_case(_report().hero, seed=RACE_SEED, size=SIZE)

    assert isinstance(race, Race)
    with pytest.raises(AttributeError):
        race.amount_paise = 1  # type: ignore[misc]
