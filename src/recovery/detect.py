"""Detect: turning a provider delivery into a case.

The recovery loop begins here, and this is where the system is easiest to fool.
A forged delivery, a replayed one, an event that means the opposite of what we
are looking for -- each has to be refused *before* a case exists, because a case
is the thing that goes on to move money.

So detection is not a parser with a database behind it. It answers one question
-- may this delivery open a case -- and records the answer either way. The audit
trail starts at the front door rather than after it, which means the first line
of any case's history is the delivery that caused it rather than a case that
appeared by fiat.

Three refusals, in order of how badly they would end:

* **forged** -- the HMAC does not verify. Raises; nothing is recorded against a
  case that does not exist.
* **duplicate** -- the same delivery id arrived twice. Razorpay redelivers, and
  the same payment legitimately produces several events, so this keys on the
  delivery rather than the entity.
* **not an opener** -- the event is one we handle but not one that opens a
  recovery case. ``order.paid`` closes cases; opening one on a successful
  payment would be the worst detection bug available.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from recovery.domain.events import Actor, EventKind, Ledger
from recovery.domain.failure import DeclineClass, PaymentMethod, classify
from recovery.providers.webhooks import WebhookEvent, WebhookReceiver

OPENING_EVENTS: frozenset[str] = frozenset({"payment.failed", "subscription.halted"})
"""Events that mean revenue is at risk. Everything else we handle -- captures,
paid orders, downtime notices -- updates a case or affects routing; it does not
create one."""


def sign_delivery(raw_body: bytes, secret: str) -> str:
    """Sign a body the way Razorpay signs a webhook.

    Signing is over exact bytes. The console synthesises deliveries because
    Razorpay has no public URL to call here, and signs them properly so they go
    through the same verification a real delivery would rather than around it.
    """
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class Detection:
    """What one delivery was allowed to do."""

    accepted: bool
    reason: str
    """``opened``, ``duplicate``, ``unhandled_event`` or ``not_an_opener``."""

    delivery_id: str
    event: str | None = None
    case_id: str | None = None
    decline_reason: str | None = None
    decline_class: DeclineClass | None = None
    amount_paise: int | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "delivery_id": self.delivery_id,
            "event": self.event,
            "case_id": self.case_id,
            "decline_reason": self.decline_reason,
            "decline_class": self.decline_class.value if self.decline_class else None,
            "amount_paise": self.amount_paise,
        }


@dataclass
class Detector:
    """Admits deliveries and opens the cases they justify."""

    receiver: WebhookReceiver
    ledger: Ledger
    opened: int = 0
    duplicates: int = 0
    ignored: int = 0

    def deliver(self, raw_body: bytes, signature: str, *, delivery_id: str) -> Detection:
        """Verify, dedupe, and open a case if the event calls for one.

        Raises :class:`~recovery.providers.webhooks.SignatureError` on a forged
        body, deliberately: a delivery that fails verification is not a case
        with a problem, it is not a case.
        """
        event = self.receiver.receive(raw_body, signature, delivery_id)
        if event is None:
            self.duplicates += 1
            return Detection(accepted=False, reason="duplicate", delivery_id=delivery_id)

        if not event.handled:
            self.ignored += 1
            return Detection(
                accepted=False,
                reason="unhandled_event",
                delivery_id=delivery_id,
                event=event.event,
            )

        if event.event not in OPENING_EVENTS:
            self.ignored += 1
            return Detection(
                accepted=False,
                reason="not_an_opener",
                delivery_id=delivery_id,
                event=event.event,
            )

        return self._open(event, delivery_id)

    def _open(self, event: WebhookEvent, delivery_id: str) -> Detection:
        entity = event.entity
        case_id = _case_id(entity)
        reason = entity.get("error_reason")
        method = _method(entity)
        decline = classify(str(reason) if reason else None, method)
        amount = entity.get("amount")

        self.ledger.record(
            case_id,
            EventKind.DELIVERY_RECEIVED,
            Actor.WEBHOOK,
            f"{event.event} verified, delivery {delivery_id}",
            {
                "delivery_id": delivery_id,
                "event": event.event,
                "payment_id": entity.get("id"),
                "signature": "verified",
            },
        )
        self.opened += 1
        return Detection(
            accepted=True,
            reason="opened",
            delivery_id=delivery_id,
            event=event.event,
            case_id=case_id,
            decline_reason=str(reason) if reason else None,
            decline_class=decline.decline_class,
            amount_paise=int(amount) if isinstance(amount, int) else None,
        )


def _case_id(entity: dict[str, Any]) -> str:
    """The case this delivery belongs to.

    Razorpay's order id is the stable handle across a payment's retries, so it
    is what a case is keyed on. Falls back to the payment id when an event
    carries no order.
    """
    for key in ("order_id", "subscription_id", "id"):
        value = entity.get(key)
        if value:
            return str(value).replace("order_", "").replace("pay_", "")
    return "unknown"


def _method(entity: dict[str, Any]) -> PaymentMethod:
    try:
        return PaymentMethod(str(entity.get("method", "unknown")))
    except ValueError:
        return PaymentMethod.UNKNOWN


def delivery_for(
    *, case_id: str, amount_paise: int, decline_reason: str, method: str = "card"
) -> bytes:
    """A ``payment.failed`` body shaped the way Razorpay sends one.

    Used by the console to drive its own cases through real verification. The
    delivery is synthesised -- there is no public URL for Razorpay to call --
    and the console says so; what is not synthesised is the verification it
    then goes through.
    """
    return json.dumps(
        {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": f"pay_{case_id}",
                        "order_id": f"order_{case_id}",
                        "amount": amount_paise,
                        "error_reason": decline_reason,
                        "method": method,
                    }
                }
            },
        }
    ).encode()
