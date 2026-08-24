"""Razorpay adapter tests.

Everything here runs offline. Webhook verification is the one piece of this
system where being wrong means accepting a forged instruction to move money, so
it is tested against constructed vectors rather than against the network.
"""

from __future__ import annotations

import json

import pytest

from recovery.domain.money import paise
from recovery.providers.razorpay import (
    Downtime,
    DowntimeFeed,
    RazorpayError,
    RazorpayGateway,
)
from recovery.providers.webhooks import (
    SignatureError,
    WebhookReceiver,
    compute_signature,
    verify_signature,
)

SECRET = "whsec_test_value"


def body(event: str = "payment.failed", **entity: object) -> bytes:
    payload = {
        "entity": "event",
        "event": event,
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_abc123",
                    "amount": 49900,
                    "currency": "INR",
                    "status": "failed",
                    "error_reason": "insufficient_funds",
                    **entity,
                }
            }
        },
    }
    return json.dumps(payload).encode("utf-8")


# --- signature verification -----------------------------------------------


def test_valid_signature_is_accepted() -> None:
    raw = body()
    verify_signature(raw, compute_signature(raw, SECRET), SECRET)


def test_tampered_body_is_rejected() -> None:
    # The attack this exists to stop: a forged webhook claiming a payment
    # succeeded, which would close a case and credit money that never moved.
    raw = body()
    signature = compute_signature(raw, SECRET)
    forged = raw.replace(b'"failed"', b'"captured"')
    # Assert the tamper actually landed, or this test silently becomes a no-op
    # that verifies an untouched body against its own valid signature.
    assert forged != raw
    with pytest.raises(SignatureError, match="mismatch"):
        verify_signature(forged, signature, SECRET)


def test_wrong_secret_is_rejected() -> None:
    raw = body()
    with pytest.raises(SignatureError):
        verify_signature(raw, compute_signature(raw, "other_secret"), SECRET)


def test_missing_signature_is_rejected() -> None:
    with pytest.raises(SignatureError):
        verify_signature(body(), "", SECRET)


def test_unset_secret_is_rejected_rather_than_skipped() -> None:
    # An unconfigured secret must fail closed. Treating "no secret" as "no
    # verification needed" would accept every forged webhook.
    with pytest.raises(SignatureError, match="no webhook secret"):
        verify_signature(body(), "anything", "")


def test_parsed_body_is_refused() -> None:
    # Signing is over exact bytes. A dict has already lost them, and
    # re-serialising it produces intermittent mismatches that look like nothing.
    with pytest.raises(TypeError, match="raw request body as bytes"):
        verify_signature({"event": "payment.failed"}, "sig", SECRET)  # type: ignore[arg-type]


def test_reserialising_changes_the_bytes_and_breaks_the_signature() -> None:
    # Demonstrates why the rule above exists, rather than only asserting it.
    raw = body()
    signature = compute_signature(raw, SECRET)
    round_tripped = json.dumps(json.loads(raw), indent=2).encode("utf-8")
    assert round_tripped != raw
    with pytest.raises(SignatureError):
        verify_signature(round_tripped, signature, SECRET)


# --- de-duplication --------------------------------------------------------


def test_replayed_delivery_is_dropped() -> None:
    # At-least-once delivery means the same event arrives twice. Processing a
    # duplicate payment.captured would close a case twice and double-count the
    # money in the batch metrics.
    receiver = WebhookReceiver(secret=SECRET)
    raw = body("payment.captured")
    sig = compute_signature(raw, SECRET)

    first = receiver.receive(raw, sig, delivery_id="evt_1")
    second = receiver.receive(raw, sig, delivery_id="evt_1")

    assert first is not None
    assert second is None
    assert receiver.accepted == 1
    assert receiver.duplicates == 1


def test_distinct_events_for_one_payment_all_pass() -> None:
    # One payment legitimately produces several events. Keying dedupe on the
    # delivery id rather than the entity id is what keeps them.
    receiver = WebhookReceiver(secret=SECRET)
    for index, event in enumerate(("payment.failed", "payment.authorized", "payment.captured")):
        raw = body(event)
        got = receiver.receive(raw, compute_signature(raw, SECRET), delivery_id=f"evt_{index}")
        assert got is not None
    assert receiver.accepted == 3
    assert receiver.duplicates == 0


def test_forged_webhook_is_counted_and_not_admitted() -> None:
    receiver = WebhookReceiver(secret=SECRET)
    with pytest.raises(SignatureError):
        receiver.receive(body(), "deadbeef", delivery_id="evt_x")
    assert receiver.rejected == 1
    assert receiver.accepted == 0


def test_unhandled_event_is_admitted_but_flagged() -> None:
    receiver = WebhookReceiver(secret=SECRET)
    raw = body("payout.processed")
    got = receiver.receive(raw, compute_signature(raw, SECRET), delivery_id="evt_y")
    assert got is not None
    assert got.handled is False


def test_entity_is_extracted_without_assuming_the_wrapper_name() -> None:
    receiver = WebhookReceiver(secret=SECRET)
    raw = body()
    got = receiver.receive(raw, compute_signature(raw, SECRET), delivery_id="evt_z")
    assert got is not None
    assert got.entity["error_reason"] == "insufficient_funds"
    assert got.entity["amount"] == 49900


# --- downtime interpretation ----------------------------------------------


def _downtime(**kw: object) -> Downtime:
    base = {
        "id": "down_1",
        "method": "netbanking",
        "status": "started",
        "severity": "high",
        "scheduled": False,
        "instrument": {"bank": "SBIN"},
    }
    base.update(kw)
    return Downtime(**base)  # type: ignore[arg-type]


class _FakeGateway:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self._items = items
        self.calls = 0

    def get(self, path: str, **_: object) -> dict[str, object]:
        self.calls += 1
        return {"items": self._items}


def test_resolved_downtime_does_not_block() -> None:
    assert not _downtime(status="resolved").blocking


def test_low_severity_does_not_block() -> None:
    # Razorpay marks severity low when the cause is unknown and impact minimal.
    # Withholding a retry for that would cost more recovery than it saves.
    assert not _downtime(severity="low").blocking
    assert _downtime(severity="medium").blocking


def test_feed_matches_the_named_instrument_only() -> None:
    feed = DowntimeFeed(
        gateway=_FakeGateway(  # type: ignore[arg-type]
            [
                {
                    "id": "d1",
                    "method": "netbanking",
                    "status": "started",
                    "severity": "high",
                    "instrument": {"bank": "SBIN"},
                }
            ]
        )
    )
    assert feed.is_down(method="netbanking", instrument="SBIN")
    assert feed.is_down(method="netbanking", instrument="sbin")  # case-insensitive
    assert not feed.is_down(method="netbanking", instrument="HDFC")
    assert not feed.is_down(method="card", instrument="SBIN")
    assert feed.is_down(method="netbanking")  # method-wide question


def test_feed_caches_within_its_ttl() -> None:
    # The batch consults this per case; an HTTP call per decision would make
    # the runner's latency a function of the provider's.
    gateway = _FakeGateway([])
    feed = DowntimeFeed(gateway=gateway, ttl_seconds=60)  # type: ignore[arg-type]
    for _ in range(5):
        feed.current()
    assert gateway.calls == 1


# --- gateway guards --------------------------------------------------------


def test_missing_credentials_are_named(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    with pytest.raises(RazorpayError, match="RAZORPAY_KEY_ID"):
        RazorpayGateway.from_env()


def test_test_mode_is_detected_from_the_key_prefix() -> None:
    assert RazorpayGateway(key_id="rzp_test_abc", key_secret="x").is_test_mode
    assert not RazorpayGateway(key_id="rzp_live_abc", key_secret="x").is_test_mode


def test_mandate_debit_refuses_rather_than_substituting_a_link() -> None:
    # Subscriptions is not enabled on this account. Quietly sending a payment
    # link instead would report a customer-authenticated payment as an
    # automatic debit, which makes the recovery numbers mean something else.
    gateway = RazorpayGateway(key_id="rzp_test_abc", key_secret="x")
    with pytest.raises(RazorpayError, match="Subscriptions API"):
        gateway.charge(case_id="c", amount=paise(100), idempotency_key="k", at=None)


def test_outbound_messaging_refuses_until_templates_are_registered() -> None:
    gateway = RazorpayGateway(key_id="rzp_test_abc", key_secret="x")
    with pytest.raises(RazorpayError, match="DLT-registered"):
        gateway.send_message(case_id="c")
