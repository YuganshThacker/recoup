"""Domain core tests: money, decline taxonomy, case lifecycle, audit ledger."""

from __future__ import annotations

from decimal import Decimal

import pytest

from recovery.domain.case import (
    CaseState,
    IllegalTransition,
    RecoveryCase,
    StopReason,
    TailSubtype,
)
from recovery.domain.events import Actor, AuditEvent, EventKind, InMemoryLedger, Ledger
from recovery.domain.failure import DeclineClass, PaymentMethod, classify
from recovery.domain.money import format_inr, from_rupees, paise, to_rupees, total

# --- money -----------------------------------------------------------------


def test_paise_rejects_float() -> None:
    with pytest.raises(TypeError, match="int minor units"):
        paise(500.0)  # type: ignore[arg-type]


def test_paise_rejects_bool() -> None:
    # bool is an int subclass; without an explicit guard True would become 1 paise.
    with pytest.raises(TypeError):
        paise(True)  # type: ignore[arg-type]


def test_from_rupees_refuses_float() -> None:
    with pytest.raises(TypeError, match="refuses float"):
        from_rupees(123.45)  # type: ignore[arg-type]


def test_from_rupees_accepts_decimal_and_str() -> None:
    assert from_rupees(Decimal("123.45")) == 12345
    assert from_rupees("500") == 50000


def test_from_rupees_rejects_sub_paise() -> None:
    with pytest.raises(ValueError, match="whole number of paise"):
        from_rupees(Decimal("1.005"))


def test_round_trip_is_exact() -> None:
    assert to_rupees(from_rupees(Decimal("99999.99"))) == Decimal("99999.99")


def test_format_inr_uses_indian_grouping() -> None:
    assert format_inr(paise(1234500)) == "Rs 12,345.00"
    assert format_inr(paise(100000000)) == "Rs 10,00,000.00"
    assert format_inr(paise(50000)) == "Rs 500.00"


def test_total_is_exact_over_many_values() -> None:
    amounts = [paise(3333) for _ in range(3000)]
    assert total(amounts) == 9999000


# --- decline taxonomy ------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "method", "expected"),
    [
        ("insufficient_funds", PaymentMethod.CARD, DeclineClass.SOFT),
        ("card_expired", PaymentMethod.CARD, DeclineClass.HARD),
        ("debit_instrument_blocked", PaymentMethod.CARD, DeclineClass.HARD),
        ("bank_technical_error", PaymentMethod.CARD, DeclineClass.DOWNTIME),
        ("gateway_technical_error", PaymentMethod.UPI, DeclineClass.DOWNTIME),
        ("invalid_vpa", PaymentMethod.UPI, DeclineClass.HARD),
        ("payment_collect_request_expired", PaymentMethod.UPI, DeclineClass.SOFT),
        ("customer_bank_account_mismatch", PaymentMethod.UPI, DeclineClass.HARD),
    ],
)
def test_classify_known_reasons(reason: str, method: PaymentMethod, expected: DeclineClass) -> None:
    assert classify(reason, method).decline_class is expected


def test_emandate_inherits_card_reason_table() -> None:
    assert classify("card_expired", PaymentMethod.EMANDATE).decline_class is DeclineClass.HARD


def test_unmapped_reason_is_unknown_not_soft() -> None:
    # The important property: an unrecognised code must not silently inherit
    # permission to retry.
    result = classify("some_new_razorpay_code", PaymentMethod.CARD)
    assert result.decline_class is DeclineClass.UNKNOWN
    assert not result.decline_class.allows_debit_retry


def test_missing_reason_is_unknown() -> None:
    assert classify(None, PaymentMethod.CARD).decline_class is DeclineClass.UNKNOWN


def test_only_soft_permits_debit_retry() -> None:
    assert DeclineClass.SOFT.allows_debit_retry
    assert not DeclineClass.HARD.allows_debit_retry
    assert not DeclineClass.DOWNTIME.allows_debit_retry
    assert not DeclineClass.UNKNOWN.allows_debit_retry


def test_hard_requires_instrument_repair() -> None:
    assert DeclineClass.HARD.requires_instrument_repair
    assert not DeclineClass.SOFT.requires_instrument_repair


# --- case lifecycle --------------------------------------------------------


def _case() -> RecoveryCase:
    return RecoveryCase(
        case_id="case_1",
        subscription_id="sub_1",
        invoice_id="inv_1",
        customer_id="cust_1",
        amount=paise(49900),
        method=PaymentMethod.EMANDATE,
    )


def test_happy_path_to_recovered() -> None:
    case = _case()
    for target in (
        CaseState.COOLING,
        CaseState.DIAGNOSED,
        CaseState.PLANNED,
        CaseState.NOTICE_PENDING,
        CaseState.SCHEDULED,
        CaseState.EXECUTING,
        CaseState.AWAITING_OUTCOME,
        CaseState.RECOVERED,
    ):
        case.transition_to(target)
    assert case.state.is_terminal


def test_lifecycle_loops_back_for_next_attempt() -> None:
    # The defining property of dunning: a failed attempt re-enters diagnosis.
    case = _case()
    case.transition_to(CaseState.COOLING)
    case.transition_to(CaseState.DIAGNOSED)
    case.transition_to(CaseState.PLANNED)
    case.transition_to(CaseState.SCHEDULED)
    case.transition_to(CaseState.EXECUTING)
    case.transition_to(CaseState.AWAITING_OUTCOME)
    case.transition_to(CaseState.DIAGNOSED)
    assert case.state is CaseState.DIAGNOSED


def test_illegal_transition_raises_and_names_the_case() -> None:
    case = _case()
    with pytest.raises(IllegalTransition, match="case_1"):
        case.transition_to(CaseState.EXECUTING)


def test_terminal_states_are_dead_ends() -> None:
    case = _case()
    case.transition_to(CaseState.COOLING)
    case.transition_to(CaseState.RECOVERED)
    with pytest.raises(IllegalTransition, match="terminal"):
        case.transition_to(CaseState.DIAGNOSED)


def test_stopping_requires_a_reason() -> None:
    case = _case()
    with pytest.raises(ValueError, match="requires a StopReason"):
        case.transition_to(CaseState.STOPPED)


def test_stop_reason_is_recorded() -> None:
    case = _case()
    case.transition_to(CaseState.STOPPED, stop_reason=StopReason.HARD_DECLINE_UNRECOVERABLE)
    assert case.stop_reason is StopReason.HARD_DECLINE_UNRECOVERABLE


def test_stop_reason_rejected_on_non_stop_transition() -> None:
    case = _case()
    with pytest.raises(ValueError, match="only meaningful for STOPPED"):
        case.transition_to(CaseState.COOLING, stop_reason=StopReason.NO_CONSENT)


def test_tail_membership_follows_subtype() -> None:
    case = _case()
    assert not case.is_tail
    case.tail_subtype = TailSubtype.HIGH_VALUE
    assert case.is_tail


# --- audit ledger ----------------------------------------------------------


def test_ledger_records_history_in_order() -> None:
    ledger = Ledger(InMemoryLedger())
    ledger.record("case_1", EventKind.CASE_DETECTED, Actor.WEBHOOK, "charge failed")
    ledger.record("case_1", EventKind.ARM_ASSIGNED, Actor.SYSTEM, "treatment")
    history = ledger.history("case_1")
    assert [e.seq for e in history] == [1, 2]
    assert [e.kind for e in history] == [EventKind.CASE_DETECTED, EventKind.ARM_ASSIGNED]


def test_ledger_rejects_non_monotonic_seq() -> None:
    store = InMemoryLedger()
    bad = AuditEvent(
        event_id="e1",
        case_id="case_1",
        seq=7,
        occurred_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        kind=EventKind.CASE_DETECTED,
        actor=Actor.WEBHOOK,
        summary="out of order",
    )
    with pytest.raises(ValueError, match="monotonic"):
        store.append(bad)


def test_ledger_events_are_immutable() -> None:
    ledger = Ledger(InMemoryLedger())
    event = ledger.record("case_1", EventKind.CASE_DETECTED, Actor.WEBHOOK, "detected")
    with pytest.raises((AttributeError, TypeError)):
        event.summary = "tampered"  # type: ignore[misc]


def test_ledger_serialises_deterministically() -> None:
    ledger = Ledger(InMemoryLedger())
    event = ledger.record(
        "case_1", EventKind.POLICY_EVALUATED, Actor.RULES, "gates ran", {"b": 2, "a": 1}
    )
    assert '"a":1,"b":2' in event.to_json()
