"""Tests for the control room's stand-in agent.

Two kinds of test here. The first kind pins the demo beats, so a refactor that
quietly removes the refusal-and-re-plan loop fails rather than producing a dull
demo nobody notices until the stage. The second kind guards the stand-in against
being *more* capable than the real thing: it may not name an unregistered
template, propose an unproposable action, or carry an amount.
"""

from __future__ import annotations

import pytest

from recovery.agent.schema import PROPOSABLE_ACTIONS, InvalidProposal, validate
from recovery.live.demo import _REMEDIES, DemoClient
from recovery.templates import REGISTERED

KNOWN = frozenset(REGISTERED)


def _prompt(
    *,
    decline_class: str = "soft",
    reason: str = "insufficient_funds",
    amount: str = "Rs 499.00",
    notice: str = "none sent for this attempt",
    refusal: str | None = None,
) -> str:
    """A prompt shaped like the one build_prompt produces."""
    body = (
        "Case:\n"
        f"  failure reason: {reason}\n"
        f"  decline class:  {decline_class}\n"
        f"  amount:         {amount}\n"
        "  attempts used:  0 of 4 permitted\n"
        f"  pre-debit notice: {notice}\n"
        "  provider outage in progress: False\n"
    )
    if refusal is not None:
        body += f"\nYour proposal #1 (retry_debit) was REFUSED:\n{refusal}\n"
    return body


def _propose(prompt: str) -> dict[str, object]:
    reply = DemoClient().propose(system="", prompt=prompt, schema={})
    assert reply.payload is not None
    return reply.payload


# --- the demo beats --------------------------------------------------------


def test_the_opening_move_on_a_fresh_case_reaches_for_the_debit() -> None:
    # Beat 1. RBI requires a notice before each e-mandate debit, so this is the
    # overreach the mandate gate exists to catch.
    assert _propose(_prompt())["action"] == "retry_debit"


def test_it_re_plans_onto_the_remedy_the_refusal_named() -> None:
    refused = _prompt(
        refusal="- [mandate] predebit_notice_required: no notice on file "
        "(unblocked by: send_predebit_notice)"
    )

    assert _propose(refused)["action"] == "send_predebit_notice"


def test_the_remedy_is_read_from_the_refusal_not_inferred_from_the_code() -> None:
    # The gates carry a remediation precisely so a re-plan is a lookup. Same
    # refusal code, different named remedy, different answer.
    refused = _prompt(
        refusal="- [mandate] predebit_notice_required: contrived (unblocked by: escalate)"
    )

    assert _propose(refused)["action"] == "escalate"


def test_the_most_recent_refusal_wins_when_several_are_listed() -> None:
    refused = _prompt(
        refusal=(
            "- [mandate] afa_required_above_threshold: over ceiling "
            "(unblocked by: send_payment_link)\n"
            "- [attempt_budget] internal_attempt_cap: spent (unblocked by: stop)"
        )
    )

    assert _propose(refused)["action"] == "stop"


def test_an_over_ceiling_case_with_a_notice_is_offered_a_link_not_a_debit() -> None:
    # Above the AFA-free ceiling no automatic debit can carry the amount, so
    # proposing one again is the move that loops.
    served = _prompt(amount="Rs 49,999.00", notice="2026-08-27 10:00 IST (Thu)")

    assert _propose(served)["action"] == "send_payment_link"


def test_an_under_ceiling_case_with_a_notice_still_retries() -> None:
    served = _prompt(amount="Rs 499.00", notice="2026-08-27 10:00 IST (Thu)")

    assert _propose(served)["action"] == "retry_debit"


def test_an_unmapped_code_produces_a_proposal_the_schema_rejects() -> None:
    # Beat 2, and the strong form of the assertion: the real validator refuses
    # it, rather than the test merely observing a null template.
    payload = _propose(_prompt(decline_class="unknown", reason="unmapped_provider_code"))

    with pytest.raises(InvalidProposal, match="named no template"):
        validate(payload, known_templates=KNOWN)


def test_a_refusal_naming_no_remedy_falls_back_to_waiting() -> None:
    refused = _prompt(refusal="- [mandate] something_new: a code with no remedy attached")

    assert _propose(refused)["action"] == "wait"


# --- guards: the stand-in may not exceed the real thing --------------------


def _every_payload() -> list[dict[str, object]]:
    """Every proposal the stand-in is capable of emitting."""
    prompts = [
        _prompt(),
        _prompt(decline_class="hard", reason="card_expired"),
        _prompt(decline_class="unknown", reason="unmapped_provider_code"),
        _prompt(amount="Rs 49,999.00", notice="2026-08-27 10:00 IST (Thu)"),
        _prompt(notice="2026-08-27 10:00 IST (Thu)"),
    ]
    return [_propose(p) for p in prompts] + [make() for make in _REMEDIES.values()]


def test_no_proposal_it_can_make_carries_an_amount() -> None:
    # The structural property the whole architecture rests on: there is no
    # field for a figure to travel in, and the stand-in does not invent one.
    for payload in _every_payload():
        assert "amount" not in payload


def test_every_action_it_can_propose_is_in_the_bounded_menu() -> None:
    proposable = {action.value for action in PROPOSABLE_ACTIONS}

    for payload in _every_payload():
        assert payload["action"] in proposable


def test_every_template_it_can_name_is_registered() -> None:
    # A typo'd template id would surface as a schema rejection mid-demo, which
    # reads to a room as the system being broken rather than being careful.
    for payload in _every_payload():
        template_id = payload["template_id"]
        if template_id is not None:
            assert template_id in KNOWN


def test_only_the_deliberate_beat_is_invalid() -> None:
    # Everything except the unmapped-code proposal must survive validation.
    for payload in _every_payload():
        if payload["action"] == "escalate" and payload["template_id"] is None:
            continue
        validate(payload, known_templates=KNOWN)
