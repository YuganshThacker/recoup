"""Razorpay webhook verification and de-duplication.

Two independent jobs, both of which must be right before a webhook is allowed
to move a case forward.

**Verification.** Razorpay signs every webhook with ``X-Razorpay-Signature``:
an HMAC-SHA256, hex-encoded, over the *raw request body*, keyed with the
webhook secret set in the dashboard. That secret is not the API key secret, and
it differs between test and live mode.

The raw body matters more than it looks. Parsing JSON and re-serialising it
changes bytes -- key order, float formatting, whitespace -- and the signature
will intermittently fail for reasons that look like nothing. So this module
takes ``bytes`` and never a parsed object, and comparison is constant-time:
a byte-at-a-time comparison leaks how much of a forged signature was correct,
which is enough to construct one.

**De-duplication.** Webhook delivery is at-least-once and out of order, and the
payload is a snapshot of the entity at the time of the event rather than its
current state. A duplicate ``payment.captured`` that gets processed twice would
close a case twice and double-count recovered money in the batch metrics, so
events are keyed on their delivery id and replays are dropped.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from dataclasses import dataclass, field
from typing import Any

# Events this system acts on. Anything else is acknowledged and ignored --
# Razorpay will deliver whatever the account is subscribed to, and silently
# processing an unexpected event type is worse than ignoring it.
HANDLED_EVENTS: frozenset[str] = frozenset(
    {
        "payment.failed",
        "payment.authorized",
        "payment.captured",
        "order.paid",
        "payment.downtime.started",
        "payment.downtime.updated",
        "payment.downtime.resolved",
        "invoice.paid",
        "payment_link.paid",
        "subscription.charged",
        "subscription.pending",
        "subscription.halted",
    }
)


class SignatureError(Exception):
    """The webhook did not come from Razorpay, or was tampered with."""


def compute_signature(raw_body: bytes, secret: str) -> str:
    """HMAC-SHA256 of the raw body, hex-encoded."""
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, signature: str, secret: str) -> None:
    """Raise :class:`SignatureError` unless the signature is valid.

    Takes bytes deliberately. A caller holding a parsed dict has already lost
    the exact bytes that were signed.
    """
    if not isinstance(raw_body, bytes | bytearray):
        raise TypeError(
            "verify_signature needs the raw request body as bytes; a parsed "
            "object has already lost the bytes that were signed"
        )
    if not secret:
        raise SignatureError("no webhook secret configured")
    expected = compute_signature(bytes(raw_body), secret)
    if not hmac.compare_digest(expected, signature or ""):
        raise SignatureError("signature mismatch")


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """A verified, parsed webhook."""

    delivery_id: str
    event: str
    payload: dict[str, Any]
    handled: bool

    @property
    def entity(self) -> dict[str, Any]:
        """The primary entity this event is about, if we can find one.

        Razorpay nests entities as ``payload.<type>.entity``. The type varies
        by event, so this returns the first one rather than assuming a shape.
        """
        for wrapper in self.payload.get("payload", {}).values():
            if isinstance(wrapper, dict):
                entity = wrapper.get("entity")
                if isinstance(entity, dict):
                    narrowed: dict[str, Any] = entity
                    return narrowed
        return {}


@dataclass
class WebhookReceiver:
    """Verifies, parses, and de-duplicates incoming webhooks."""

    secret: str
    _seen: set[str] = field(default_factory=set, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0

    def receive(self, raw_body: bytes, signature: str, delivery_id: str) -> WebhookEvent | None:
        """Verify and admit one webhook, or ``None`` if it is a replay.

        ``delivery_id`` is Razorpay's ``X-Razorpay-Event-Id`` header. Keying on
        it rather than on the entity id is what makes this correct: the same
        payment legitimately produces several events, and only a redelivery of
        the *same* event should be dropped.
        """
        try:
            verify_signature(raw_body, signature, self.secret)
        except SignatureError:
            self.rejected += 1
            raise

        with self._lock:
            if delivery_id in self._seen:
                self.duplicates += 1
                return None
            self._seen.add(delivery_id)
            self.accepted += 1

        body = json.loads(raw_body)
        name = str(body.get("event", ""))
        return WebhookEvent(
            delivery_id=delivery_id,
            event=name,
            payload=body,
            handled=name in HANDLED_EVENTS,
        )
