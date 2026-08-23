"""Payment provider boundary.

Razorpay sits behind this interface so a simulator can stand in on the same
contract. That is not only a testing convenience -- it is forced by the shape
of the platform. Test-mode manual charges are driven by a dashboard button, one
case at a time, so the real path cannot produce a batch of the size statistics
require. Batch numbers come from the simulator; the real adapter proves the
integration. Two paths, two claims, never conflated.

Every money-moving call takes an idempotency key. Webhooks arrive more than
once and schedulers double-fire; a debit that executes twice is the worst bug
this system could have.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from recovery.domain.failure import PaymentMethod
from recovery.domain.money import Paise
from recovery.policy.actions import Channel


@dataclass(frozen=True, slots=True)
class ChargeOutcome:
    """Result of a debit attempt."""

    succeeded: bool
    payment_id: str
    amount: Paise
    reason: str | None = None
    """Razorpay ``error_reason`` when the charge failed."""

    method: PaymentMethod = PaymentMethod.EMANDATE
    deduplicated: bool = False
    """True when the idempotency key matched an earlier call and no new debit
    was attempted. The caller must treat this as "already done", not "do again"."""


@dataclass(frozen=True, slots=True)
class MessageReceipt:
    """Result of an outbound message."""

    delivered: bool
    message_id: str
    channel: Channel
    cost: Paise
    deduplicated: bool = False


class PaymentProvider(Protocol):
    """What the recovery system needs from a payment platform."""

    def charge(
        self, *, case_id: str, amount: Paise, idempotency_key: str, at: datetime
    ) -> ChargeOutcome:
        """Attempt a debit on the case's mandate."""
        ...

    def send_message(
        self,
        *,
        case_id: str,
        channel: Channel,
        template_id: str | None,
        idempotency_key: str,
        at: datetime,
    ) -> MessageReceipt:
        """Send a registered template on a channel."""
        ...
