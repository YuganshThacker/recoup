"""Red team tests.

Each attack on the panel drives real code and reports what really happened.
The point of testing them is that a demo which *says* an attack was repelled
while quietly returning a canned string is worse than no demo at all -- so
these assert on the mechanism, not the wording.

The last test is the important one: every attack names a test in this suite,
and that name is checked to exist. The claim on stage is "each of these is in
CI", and this is what stops that claim from rotting.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from recovery.domain.events import EventKind, InMemoryLedger, Ledger
from recovery.live.redteam import ATTACKS, UnknownAttack, catalogue, run_attack


def _run(slug: str) -> object:
    return run_attack(slug)


# --- every attack ----------------------------------------------------------


@pytest.mark.parametrize("slug", sorted(ATTACKS))
def test_every_defence_holds(slug: str) -> None:
    result = run_attack(slug)

    assert result.held is True, f"{slug}: {result.verdict}"
    assert result.evidence, f"{slug} produced a verdict with no evidence behind it"


@pytest.mark.parametrize("slug", sorted(ATTACKS))
def test_every_attack_names_the_code_that_stopped_it(slug: str) -> None:
    result = run_attack(slug)

    assert result.defended_by.endswith(".py") or "::" in result.defended_by
    assert Path("src") / result.defended_by.split("::")[0] != Path("src")


def test_an_unknown_attack_is_refused_rather_than_ignored() -> None:
    with pytest.raises(UnknownAttack, match="not an attack"):
        run_attack("drop_the_database")


def test_the_catalogue_describes_every_attack_without_running_one() -> None:
    listed = catalogue()

    assert {entry["slug"] for entry in listed} == set(ATTACKS)
    assert all(entry["title"] and entry["claim"] for entry in listed)


# --- the individual mechanisms ---------------------------------------------


def test_a_forged_webhook_is_rejected_and_a_genuine_one_is_not() -> None:
    # Both halves matter. An implementation that rejected everything would
    # pass the first assertion and be useless.
    result = _run("forge_webhook")
    body = " ".join(result.evidence)

    assert "accepted" in body
    assert "signature mismatch" in body


def test_an_injected_amount_has_nowhere_to_land() -> None:
    result = _run("prompt_injection")
    body = " ".join(result.evidence)

    assert "no amount field" in body or "no 'amount'" in body
    assert "5,000,000" in body or "50,00,000" in body or "5000000" in body


def test_a_model_blackout_completes_the_batch_on_the_fallback() -> None:
    result = _run("model_blackout")
    body = " ".join(result.evidence)

    assert "fallback" in body
    assert "completed" in body


def test_a_debit_over_the_afa_ceiling_is_refused_and_names_the_remedy() -> None:
    result = _run("afa_ceiling")
    body = " ".join(result.evidence)

    assert "afa_required_above_threshold" in body
    assert "send_payment_link" in body


def test_a_contact_outside_the_window_is_refused() -> None:
    result = _run("quiet_hours")
    body = " ".join(result.evidence)

    assert "outside_contact_hours" in body
    assert "21:" in body, "the evidence should state the local time it refused"


def test_a_double_fired_debit_charges_once() -> None:
    result = _run("double_fire")
    body = " ".join(result.evidence)

    assert "1" in body
    assert "deduplicated" in body


@pytest.mark.parametrize("slug", ["quiet_hours", "afa_ceiling"])
def test_a_policy_attack_trips_exactly_one_gate(slug: str) -> None:
    # An attack that also fails consent and template binding shows a sloppy
    # attacker, not a precise defence. The interesting claim is that seven
    # gates pass and exactly one objects -- which is also what makes the gate
    # matrix legible when the attack fires behind the panel.
    refusals = [line for line in run_attack(slug).evidence if line.startswith("refused [")]

    assert len(refusals) == 1, f"{slug} was refused by {len(refusals)} gates: {refusals}"


# --- the audit trail -------------------------------------------------------


def test_an_attack_writes_to_the_ledger_when_given_one() -> None:
    # So the attack appears in the control room's lanes as it happens, rather
    # than only in a panel nobody is looking at.
    ledger = Ledger(InMemoryLedger())

    result = run_attack("afa_ceiling", ledger=ledger)

    history = ledger.history(result.case_id)
    assert history
    assert any(e.kind is EventKind.ACTION_REFUSED for e in history)


def test_an_attack_runs_without_a_ledger() -> None:
    assert run_attack("afa_ceiling").held is True


def test_attack_case_ids_are_marked_so_they_cannot_be_read_as_real_cases() -> None:
    for slug in ATTACKS:
        assert run_attack(slug).case_id.startswith("redteam:")


# --- the claim that these are in CI ----------------------------------------


def test_every_attack_names_a_test_that_exists() -> None:
    # "Each of these is a passing test in CI" is said out loud on stage. This
    # is what keeps it true after a rename.
    defined = set()
    for path in Path("tests").glob("test_*.py"):
        defined.update(re.findall(r"^def (test_\w+)", path.read_text(), re.M))

    for slug in ATTACKS:
        named = run_attack(slug).test
        assert named in defined, f"{slug} cites {named}, which no test defines"
