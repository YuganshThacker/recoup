"""Worked-example tests.

Track 3's jury asks to be walked through one full case from the failure that
started it to the money that came back. The two things that must hold: the walk
is complete -- it starts at a verified delivery and ends at attributed money --
and the case is chosen by outcome rather than by hand.
"""

from __future__ import annotations

import json
import re

from recovery.live.casestudy import build_case_study
from recovery.live.casestudy_page import render_case_study

STUDY = build_case_study()


def test_a_worked_example_exists() -> None:
    assert STUDY is not None


def test_the_walk_starts_at_a_verified_delivery() -> None:
    assert STUDY is not None
    first = STUDY.stages[0]

    assert first.name == "DETECT"
    assert first.steps
    assert "verified" in first.steps[0].summary


def test_the_walk_ends_at_money_that_came_back() -> None:
    # "If the demonstration ends with a chart or notification, it is a
    # dashboard, not an agent."
    assert STUDY is not None
    last = STUDY.stages[-1]

    assert last.name == "MEASURE"
    assert STUDY.recovered is True
    assert "recovered" in last.steps[-1].summary


def test_the_recovery_is_attributed_not_organic() -> None:
    """The selection rule that keeps this honest.

    A customer who paid unprompted proves nothing about the agent, and picking
    one as the worked example would be claiming someone else's work. Organic
    recovery is the baseline the measured lift is taken against.
    """
    assert STUDY is not None

    assert STUDY.attributed is True
    assert "attributed" in STUDY.stages[-1].steps[-1].summary


def test_every_rubric_stage_is_present_and_in_order() -> None:
    assert STUDY is not None
    page = render_case_study(STUDY)

    order = [m.group(1) for m in re.finditer(r"<h2><span class='n'>\d+</span>(\w+)</h2>", page)]
    assert order == ["DETECT", "DIAGNOSE", "INTERVENE", "RECOVER", "MEASURE"]


def test_the_diagnosis_states_what_the_class_permits() -> None:
    # Diagnose is a conclusion, not an event, so it is derived. It still has to
    # say what follows from the class.
    assert STUDY is not None

    assert STUDY.diagnosis.decline_class
    assert STUDY.diagnosis.permits


def test_every_gated_decision_reports_how_many_gates_ran() -> None:
    assert STUDY is not None
    gated = [t for s in STUDY.stages for t in s.steps if t.gates_run]

    assert gated, "no decision in the walk recorded a gate evaluation"
    assert all(t.gates_run == 9 for t in gated), "every decision runs all nine"


def test_the_study_is_reproducible() -> None:
    # The video is re-shot more than once.
    again = build_case_study()

    assert STUDY is not None and again is not None
    assert again.case_id == STUDY.case_id
    assert again.recovered_amount == STUDY.recovered_amount


def test_the_payload_serialises() -> None:
    assert STUDY is not None
    json.dumps(STUDY.payload())


def test_the_page_says_the_outcome_is_simulated() -> None:
    assert "Simulated" in render_case_study(STUDY)


def test_a_batch_with_no_attributable_recovery_shows_no_example() -> None:
    # Better to show nothing than to substitute a case that did not work.
    page = render_case_study(None)

    assert "no worked example" in page.lower() or "No case in this batch" in page


def test_the_page_fetches_nothing() -> None:
    page = re.sub(r"xmlns='[^']*'", "", render_case_study(STUDY))

    assert re.findall(r"""["'(](?:https?:)?//[^"')\s]+""", page) == []
