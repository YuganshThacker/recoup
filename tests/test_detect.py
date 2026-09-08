"""Detection tests: turning provider deliveries into cases.

Track 3's loop begins at Detect, and detection is where a system is easiest to
fool: a forged delivery, a replayed one, an event we do not handle. Every one of
those has to be refused *before* a case exists, because a case is the thing that
goes on to move money.

The detector is deliberately not a parser with a database attached. It answers
one question -- may this delivery open a case -- and records the answer either
way, so the audit trail starts at the front door rather than after it.
"""

from __future__ import annotations

import json

import pytest

from recovery.detect import Detection, Detector, sign_delivery
from recovery.domain.events import EventKind, InMemoryLedger, Ledger
from recovery.domain.failure import DeclineClass
from recovery.providers.webhooks import SignatureError, WebhookReceiver

SECRET = "detect_test_secret_not_a_real_one"


def _detector() -> tuple[Detector, Ledger, InMemoryLedger]:
    store = InMemoryLedger()
    ledger = Ledger(store)
    return Detector(receiver=WebhookReceiver(secret=SECRET), ledger=ledger), ledger, store


def _delivery(
    *,
    case_id: str = "case_000001",
    reason: str = "insufficient_funds",
    event: str = "payment.failed",
) -> tuple[bytes, str]:
    body = json.dumps(
        {
            "event": event,
            "payload": {
                "payment": {
                    "entity": {
                        "id": f"pay_{case_id}",
                        "order_id": f"order_{case_id}",
                        "amount": 499900,
                        "error_reason": reason,
                        "method": "card",
                    }
                }
            },
        }
    ).encode()
    return body, sign_delivery(body, SECRET)


# --- the happy path ---------------------------------------------------------


def test_a_genuine_failure_opens_a_case() -> None:
    detector, _ledger, _store = _detector()
    body, signature = _delivery()

    result = detector.deliver(body, signature, delivery_id="evt_1")

    assert result.accepted is True
    assert result.reason == "opened"
    assert result.case_id is not None
    assert result.decline_class is DeclineClass.SOFT


def test_the_decline_is_classified_at_the_door() -> None:
    # Diagnose starts here: the class determines what is even permitted later.
    detector, _ledger, _store = _detector()
    body, signature = _delivery(reason="card_expired")

    result = detector.deliver(body, signature, delivery_id="evt_1")

    assert result.decline_class is DeclineClass.HARD
    assert result.decline_reason == "card_expired"


def test_an_unmapped_reason_is_unknown_rather_than_guessed() -> None:
    detector, _ledger, _store = _detector()
    body, signature = _delivery(reason="something_we_have_never_seen")

    assert detector.deliver(body, signature, delivery_id="evt_1").decline_class is (
        DeclineClass.UNKNOWN
    )


def test_the_delivery_is_recorded_before_the_case_is_diagnosed() -> None:
    # The audit trail has to start at the front door. Otherwise the first thing
    # the record shows is a case that appeared by fiat.
    detector, ledger, _store = _detector()
    body, signature = _delivery()

    result = detector.deliver(body, signature, delivery_id="evt_1")

    assert result.case_id is not None
    kinds = [e.kind for e in ledger.history(result.case_id)]
    assert kinds[0] is EventKind.DELIVERY_RECEIVED


# --- what must not open a case ---------------------------------------------


def test_a_forged_delivery_never_opens_a_case() -> None:
    detector, _ledger, store = _detector()
    body, signature = _delivery()
    tampered = body.replace(b"499900", b"999900")

    with pytest.raises(SignatureError):
        detector.deliver(tampered, signature, delivery_id="evt_1")

    assert store.all_cases() == [], "a rejected delivery must leave no case behind"


def test_a_replayed_delivery_does_not_open_a_second_case() -> None:
    detector, _ledger, store = _detector()
    body, signature = _delivery()
    detector.deliver(body, signature, delivery_id="evt_1")

    replay = detector.deliver(body, signature, delivery_id="evt_1")

    assert replay.accepted is False
    assert replay.reason == "duplicate"
    assert len(store.all_cases()) == 1


def test_an_event_we_do_not_handle_opens_nothing() -> None:
    detector, _ledger, store = _detector()
    body, signature = _delivery(event="payment.pending")

    result = detector.deliver(body, signature, delivery_id="evt_1")

    assert result.accepted is False
    assert result.reason == "unhandled_event"
    assert store.all_cases() == []


def test_a_success_event_is_handled_but_opens_no_recovery_case() -> None:
    # order.paid is in HANDLED_EVENTS because it closes cases, not because it
    # opens them. Opening a recovery case on a successful payment would be the
    # worst possible detection bug.
    detector, _ledger, store = _detector()
    body, signature = _delivery(event="order.paid")

    result = detector.deliver(body, signature, delivery_id="evt_1")

    assert result.accepted is False
    assert store.all_cases() == []


# --- the tallies the console shows -----------------------------------------


def test_the_detector_counts_what_it_saw() -> None:
    detector, _ledger, _store = _detector()
    for n in range(3):
        body, signature = _delivery(case_id=f"case_{n:06d}")
        detector.deliver(body, signature, delivery_id=f"evt_{n}")
    body, signature = _delivery(case_id="case_000000")
    detector.deliver(body, signature, delivery_id="evt_0")

    assert detector.opened == 3
    assert detector.duplicates == 1


def test_detection_is_serialisable_for_the_api() -> None:
    detector, _ledger, _store = _detector()
    body, signature = _delivery()

    json.dumps(detector.deliver(body, signature, delivery_id="evt_1").payload())


def test_a_detection_names_the_delivery_it_came_from() -> None:
    detector, _ledger, _store = _detector()
    body, signature = _delivery()

    result: Detection = detector.deliver(body, signature, delivery_id="evt_abc")

    assert result.delivery_id == "evt_abc"
