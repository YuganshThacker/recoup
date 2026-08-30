"""Compliance x-ray tests.

A compliance report that can only say "pass" is decoration. These tests spend
most of their effort on the failing direction: a fabricated ledger where an
action executed without a permit, a contact with no registered template, a
debit whose mandate gate never ran. If the x-ray cannot catch those, it is not
evidence of anything.

The other thing pinned here is the caveat. Ledger timestamps are wall-clock --
a whole simulated case spans four milliseconds of real time -- so the x-ray
cannot re-derive the 24-hour notice interval and must say so rather than
print a number it does not have.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from recovery.domain.events import Actor, AuditEvent, EventKind
from recovery.live.xray import build_xray
from recovery.live.xray_page import render_xray

NOW = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


def _event(
    seq: int,
    kind: EventKind,
    summary: str,
    payload: dict[str, object] | None = None,
    actor: Actor = Actor.RULES,
) -> AuditEvent:
    return AuditEvent(
        event_id=f"evt_{seq}",
        case_id="case_x",
        seq=seq,
        occurred_at=NOW,
        kind=kind,
        actor=actor,
        summary=summary,
        payload=payload or {},
    )


def _gates(**overrides: bool) -> list[dict[str, object]]:
    names = (
        "consent",
        "suppression",
        "mandate",
        "attempt_budget",
        "quiet_hours",
        "cooldown",
        "template",
        "channel_economics",
    )
    return [
        {
            "gate": name,
            "passed": overrides.get(name, True),
            "code": None if overrides.get(name, True) else f"{name}_refused",
            "explanation": f"{name} ok",
            "retry_after": None,
            "remediation": None if overrides.get(name, True) else "wait",
        }
        for name in names
    ]


def _clean_case() -> list[AuditEvent]:
    """A notice, then a debit, both permitted, both executed."""
    return [
        _event(
            1,
            EventKind.CASE_DETECTED,
            "charge failed: insufficient_funds (soft)",
            {"amount_paise": 499900, "arm": "treatment"},
            Actor.WEBHOOK,
        ),
        _event(
            2,
            EventKind.POLICY_EVALUATED,
            "permitted: send_predebit_notice via sms [RP_PREDEBIT_01]",
            {
                "action": "send_predebit_notice via sms [RP_PREDEBIT_01]",
                "proposed_by": "rules",
                "permitted": True,
                "gates": _gates(),
            },
        ),
        _event(
            3,
            EventKind.NOTICE_SENT,
            "sent RP_PREDEBIT_01 via sms",
            {"message_id": "msg_1", "cost_paise": 20},
            Actor.SYSTEM,
        ),
        _event(
            4,
            EventKind.POLICY_EVALUATED,
            "permitted: retry_debit",
            {"action": "retry_debit", "proposed_by": "rules", "permitted": True, "gates": _gates()},
        ),
        _event(
            5, EventKind.ACTION_EXECUTED, "debit succeeded", {"payment_id": "pay_1"}, Actor.SYSTEM
        ),
        _event(
            6,
            EventKind.OUTCOME_RECORDED,
            "recovered (attributed)",
            {"hours_to_recovery": 30.0},
            Actor.SYSTEM,
        ),
    ]


# --- the clean case --------------------------------------------------------


def test_a_clean_case_passes_every_check() -> None:
    xray = build_xray("case_x", _clean_case())

    failed = [c.code for c in xray.checks if not c.passed]
    assert failed == [], f"a compliant case reported exceptions: {failed}"
    assert xray.verdict == "clean"


def test_it_lists_every_contact_with_its_template() -> None:
    xray = build_xray("case_x", _clean_case())

    assert len(xray.contacts) == 1
    assert xray.contacts[0].template_id == "RP_PREDEBIT_01"
    assert xray.contacts[0].channel == "sms"
    assert xray.contacts[0].registered is True


def test_it_lists_every_money_action_with_its_authority() -> None:
    xray = build_xray("case_x", _clean_case())

    assert len(xray.money_actions) == 1
    assert xray.money_actions[0].authorised is True


def test_it_tallies_every_gate_across_the_case() -> None:
    # Passes are evidence too: a compliance engine that logged only refusals
    # could not show the envelope was applied.
    xray = build_xray("case_x", _clean_case())

    assert xray.gate_tally["mandate"] == (2, 0)
    assert set(xray.gate_tally) == {
        "consent",
        "suppression",
        "mandate",
        "attempt_budget",
        "quiet_hours",
        "cooldown",
        "template",
        "channel_economics",
    }


def test_it_states_the_amount_from_the_detection_event() -> None:
    assert build_xray("case_x", _clean_case()).amount == "Rs 4,999.00"


# --- executions that are messages, not debits ------------------------------


def _message_case() -> list[AuditEvent]:
    """A permitted instrument-update request, executed.

    The runner records pre-debit notices as NOTICE_SENT and *every other*
    message as ACTION_EXECUTED, so this shape is ordinary. An earlier version
    of the x-ray read the send as an unauthorised debit and reported an
    exception on a compliant case.
    """
    return [
        _event(
            1,
            EventKind.CASE_DETECTED,
            "charge failed: debit_instrument_inactive (hard)",
            {"amount_paise": 49900, "arm": "treatment"},
            Actor.WEBHOOK,
        ),
        _event(
            2,
            EventKind.POLICY_EVALUATED,
            "permitted: request_instrument_update via sms [RP_INSTRUMENT_01]",
            {
                "action": "request_instrument_update via sms [RP_INSTRUMENT_01]",
                "proposed_by": "agent",
                "permitted": True,
                "gates": _gates(),
            },
        ),
        _event(
            3,
            EventKind.ACTION_EXECUTED,
            "sent RP_INSTRUMENT_01 via sms",
            {"message_id": "msg_2", "cost_paise": 20},
            Actor.SYSTEM,
        ),
    ]


def test_a_permitted_message_execution_is_not_an_exception() -> None:
    xray = build_xray("case_x", _message_case())

    assert xray.verdict == "clean", [c.detail for c in xray.exceptions]


def test_a_message_recorded_as_an_execution_still_counts_as_a_contact() -> None:
    # The worse half of the same bug: only notices were being checked for
    # template registration, so payment links and dunning reminders were
    # escaping C2 entirely.
    xray = build_xray("case_x", _message_case())

    assert [c.template_id for c in xray.contacts] == ["RP_INSTRUMENT_01"]
    assert xray.contacts[0].channel == "sms"


def test_a_message_is_not_counted_as_an_execution_against_the_instrument() -> None:
    assert build_xray("case_x", _message_case()).money_actions == ()


def test_sending_a_different_template_than_the_one_permitted_is_an_exception() -> None:
    # Stronger than the check it replaces: permitted A, sent B.
    events = _message_case()
    events[2] = _event(
        3,
        EventKind.ACTION_EXECUTED,
        "sent RP_DUNNING_01 via sms",
        {"message_id": "msg_2", "cost_paise": 20},
        Actor.SYSTEM,
    )

    xray = build_xray("case_x", events)

    assert any(c.code == "C1" and not c.passed for c in xray.checks)


def test_a_debit_is_not_covered_by_a_message_permit() -> None:
    events = _message_case()
    events[2] = _event(
        3, EventKind.ACTION_EXECUTED, "debit succeeded", {"payment_id": "pay_9"}, Actor.SYSTEM
    )

    xray = build_xray("case_x", events)

    assert any(c.code == "C1" and not c.passed for c in xray.checks)


# --- the failing direction, which is the point -----------------------------


def test_an_execution_with_no_permit_is_an_exception() -> None:
    # The core compliance question, and the one the ledger genuinely answers.
    events = _clean_case()
    del events[3]  # remove the policy_evaluated that authorised the debit

    xray = build_xray("case_x", events)

    assert xray.verdict == "exceptions"
    assert any(c.code == "C1" and not c.passed for c in xray.checks)


def test_a_contact_with_no_registered_template_is_an_exception() -> None:
    events = _clean_case()
    events[2] = _event(3, EventKind.NOTICE_SENT, "sent via sms", {"cost_paise": 20}, Actor.SYSTEM)

    xray = build_xray("case_x", events)

    assert any(c.code == "C2" and not c.passed for c in xray.checks)


def test_a_debit_whose_mandate_gate_refused_is_an_exception() -> None:
    # Executing over a refusal is the thing that must never happen quietly.
    events = _clean_case()
    events[3] = _event(
        4,
        EventKind.POLICY_EVALUATED,
        "permitted: retry_debit",
        {
            "action": "retry_debit",
            "proposed_by": "rules",
            "permitted": True,
            "gates": _gates(mandate=False),
        },
    )

    xray = build_xray("case_x", events)

    assert any(c.code == "C3" and not c.passed for c in xray.checks)


def test_a_broken_sequence_is_an_exception() -> None:
    # Evidence integrity: an append-only ledger with a hole in it is not one.
    events = _clean_case()
    events[4] = _event(
        9, EventKind.ACTION_EXECUTED, "debit succeeded", {"payment_id": "pay_1"}, Actor.SYSTEM
    )

    xray = build_xray("case_x", events)

    assert any(c.code == "C5" and not c.passed for c in xray.checks)


def test_a_refusal_with_no_code_is_an_exception() -> None:
    events = _clean_case()
    events.append(
        _event(
            7,
            EventKind.ACTION_REFUSED,
            "refused: retry_debit",
            {
                "action": "retry_debit",
                "permitted": False,
                "gates": [
                    {
                        "gate": "mandate",
                        "passed": False,
                        "code": None,
                        "explanation": "",
                        "retry_after": None,
                        "remediation": None,
                    }
                ],
            },
        )
    )

    xray = build_xray("case_x", events)

    assert any(c.code == "C4" and not c.passed for c in xray.checks)


def test_every_check_explains_itself_whether_it_passed_or_not() -> None:
    for events in (_clean_case(), _clean_case()[:1]):
        for check in build_xray("case_x", events).checks:
            assert check.question and check.detail


# --- the printable document ------------------------------------------------


def test_the_document_stamps_the_verdict() -> None:
    page = render_xray(build_xray("case_x", _clean_case()))

    assert "NO EXCEPTIONS" in page
    assert "<title>" in page


def test_the_document_stamps_an_exception_when_there_is_one() -> None:
    events = _clean_case()
    del events[3]

    page = render_xray(build_xray("case_x", events))

    assert "EXCEPTIONS FOUND" in page
    assert "NO PERMIT" in page


def test_the_document_prints_its_own_caveats() -> None:
    page = render_xray(build_xray("case_x", _clean_case()))

    assert "does not claim" in page
    assert "wall-clock" in page


def test_the_document_fetches_nothing() -> None:
    # Someone in compliance opens this offline, from a file, months later.
    page = render_xray(build_xray("case_x", _clean_case()))
    external = re.findall(r"""["'(](?:https?:)?//[^"')\s]+""", page)

    assert external == []


def test_the_document_escapes_what_it_prints() -> None:
    # Summaries are written by the system, but a report that interpolates
    # unescaped text into HTML is one provider string away from being wrong.
    events = _clean_case()
    events.append(
        _event(
            7,
            EventKind.ACTION_REFUSED,
            "refused: <script>alert(1)</script>",
            {
                "gates": [
                    {
                        "gate": "mandate",
                        "passed": False,
                        "code": "x",
                        "explanation": "",
                        "retry_after": None,
                        "remediation": None,
                    }
                ]
            },
        )
    )

    page = render_xray(build_xray("case_x", events))

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_an_empty_case_produces_a_document_that_says_so() -> None:
    page = render_xray(build_xray("case_nothing", []))

    assert "NO RECORD" in page
    assert "absence of record" in page


# --- C6: contact volume ----------------------------------------------------


def _notices(count: int) -> list[AuditEvent]:
    """A case that announces a debit over and over and never makes one."""
    events = [
        _event(
            1,
            EventKind.CASE_DETECTED,
            "charge failed: insufficient_funds (soft)",
            {"amount_paise": 49900, "arm": "treatment"},
            Actor.WEBHOOK,
        ),
    ]
    seq = 2
    for _ in range(count):
        events.append(
            _event(
                seq,
                EventKind.POLICY_EVALUATED,
                "permitted: send_predebit_notice via sms [RP_PREDEBIT_01]",
                {
                    "action": "send_predebit_notice via sms [RP_PREDEBIT_01]",
                    "proposed_by": "agent",
                    "permitted": True,
                    "gates": _gates(),
                },
            )
        )
        events.append(
            _event(
                seq + 1,
                EventKind.NOTICE_SENT,
                "sent RP_PREDEBIT_01 via sms",
                {"message_id": f"m{seq}", "cost_paise": 20},
                Actor.SYSTEM,
            )
        )
        seq += 2
    return events


def test_a_handful_of_notices_is_not_an_exception() -> None:
    xray = build_xray("case_x", _notices(3))

    assert all(c.passed for c in xray.checks if c.code == "C6")


def test_more_notices_than_the_attempt_budget_permits_is_an_exception() -> None:
    # A pre-debit notice announces a debit. The attempt budget caps debits at
    # four, so a case that announced thirty-nine was announcing debits that
    # could never happen -- and nothing stopped it, because the statutory
    # exemption from cooldown is deliberate.
    xray = build_xray("case_x", _notices(39))

    assert any(c.code == "C6" and not c.passed for c in xray.checks)
    assert xray.verdict == "exceptions"


def test_a_notice_for_every_attempt_plus_one_is_not_an_exception() -> None:
    # A case that spends its whole attempt budget legitimately sends a notice
    # per debit, plus one whose debit was then refused for another reason.
    # Flagging that would train a reader to ignore this check.
    events = _notices(5)
    seq = events[-1].seq
    for i in range(4):
        events.append(
            _event(
                seq + 1 + i * 2,
                EventKind.POLICY_EVALUATED,
                "permitted: retry_debit",
                {
                    "action": "retry_debit",
                    "proposed_by": "rules",
                    "permitted": True,
                    "gates": _gates(),
                },
            )
        )
        events.append(
            _event(
                seq + 2 + i * 2,
                EventKind.ACTION_EXECUTED,
                "debit failed",
                {"payment_id": f"p{i}"},
                Actor.SYSTEM,
            )
        )

    xray = build_xray("case_x", events)

    assert all(c.passed for c in xray.checks if c.code == "C6"), next(
        c.detail for c in xray.checks if c.code == "C6"
    )


def test_the_volume_check_says_what_it_counted() -> None:
    detail = next(c for c in build_xray("case_x", _notices(39)).checks if c.code == "C6").detail

    assert "39" in detail


def test_the_volume_check_is_reported_as_a_report_level_finding() -> None:
    # No gate prevents this today. The x-ray must not imply otherwise.
    xray = build_xray("case_x", _notices(39))
    check = next(c for c in xray.checks if c.code == "C6")

    assert "no gate" in " ".join(check.evidence).lower()


# --- what it refuses to claim ----------------------------------------------


def test_it_does_not_claim_to_have_measured_the_notice_interval() -> None:
    # Ledger timestamps are wall-clock. Printing "notice served 24h before the
    # debit" from them would be inventing a figure, which is the one thing this
    # project does not do.
    xray = build_xray("case_x", _clean_case())

    assert any("wall-clock" in caveat for caveat in xray.caveats)


def test_an_empty_case_is_reported_as_empty_rather_than_clean() -> None:
    # "No events, therefore compliant" is the worst possible bug in a
    # compliance report.
    xray = build_xray("case_nothing", [])

    assert xray.verdict == "empty"
