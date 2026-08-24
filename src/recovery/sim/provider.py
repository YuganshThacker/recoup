"""Simulated payment provider.

Answers debit attempts from the case's hidden ground truth. A charge succeeds
if and only if it is attempted at or after the moment that case becomes
recoverable -- so success is a function of *when*, never of who asked or which
arm they belong to.

Idempotency is enforced here rather than trusted upstream. A repeated key
returns the original outcome with ``deduplicated=True`` and does not attempt a
second debit, which is the property the double-fire test exercises.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from recovery.domain.failure import PaymentMethod
from recovery.domain.money import Paise, paise
from recovery.policy import constants as K
from recovery.policy.actions import Channel
from recovery.providers.base import ChargeOutcome, MessageReceipt
from recovery.sim.world import GroundTruth


@dataclass
class SimulatedProvider:
    """In-memory provider backed by per-case ground truth."""

    truths: dict[str, GroundTruth]
    charge_log: dict[str, ChargeOutcome] = field(default_factory=dict)
    message_log: dict[str, MessageReceipt] = field(default_factory=dict)
    charge_calls: int = 0
    message_calls: int = 0
    repaired: set[str] = field(default_factory=set)
    # Cases run concurrently and share these ledgers and counters.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def charge(
        self, *, case_id: str, amount: Paise, idempotency_key: str, at: datetime
    ) -> ChargeOutcome:
        """Attempt a debit. Idempotent on ``idempotency_key``."""
        with self._lock:
            return self._charge_locked(
                case_id=case_id, amount=amount, idempotency_key=idempotency_key, at=at
            )

    def _charge_locked(
        self, *, case_id: str, amount: Paise, idempotency_key: str, at: datetime
    ) -> ChargeOutcome:
        seen = self.charge_log.get(idempotency_key)
        if seen is not None:
            # Same key, same answer, and crucially no second debit.
            return ChargeOutcome(
                succeeded=seen.succeeded,
                payment_id=seen.payment_id,
                amount=seen.amount,
                reason=seen.reason,
                deduplicated=True,
            )

        self.charge_calls += 1
        truth = self.truths[case_id]
        recoverable_from = truth.recoverable_from
        if case_id in self.repaired and recoverable_from is None:
            recoverable_from = at  # a replaced instrument works immediately

        succeeded = recoverable_from is not None and at >= recoverable_from
        outcome = ChargeOutcome(
            succeeded=succeeded,
            payment_id=f"pay_{idempotency_key[:18]}",
            amount=amount,
            reason=None if succeeded else self._failure_reason(truth, at),
            method=PaymentMethod.EMANDATE,
        )
        self.charge_log[idempotency_key] = outcome
        return outcome

    @staticmethod
    def _failure_reason(truth: GroundTruth, at: datetime) -> str:
        """Why this attempt failed, from the world's point of view."""
        if truth.downtime_ends_at is not None and at < truth.downtime_ends_at:
            return "bank_technical_error"
        if truth.recoverable_from is None:
            return "card_expired"
        return "insufficient_funds"

    def send_message(
        self,
        *,
        case_id: str,
        channel: Channel,
        template_id: str | None,
        idempotency_key: str,
        at: datetime,
    ) -> MessageReceipt:
        """Deliver a registered template. Idempotent on ``idempotency_key``."""
        with self._lock:
            return self._send_locked(channel=channel, idempotency_key=idempotency_key)

    def _send_locked(self, *, channel: Channel, idempotency_key: str) -> MessageReceipt:
        seen = self.message_log.get(idempotency_key)
        if seen is not None:
            return MessageReceipt(
                delivered=seen.delivered,
                message_id=seen.message_id,
                channel=seen.channel,
                cost=seen.cost,
                deduplicated=True,
            )

        self.message_calls += 1
        receipt = MessageReceipt(
            delivered=True,
            message_id=f"msg_{idempotency_key[:18]}",
            channel=channel,
            cost=K.CHANNEL_COST.get(channel.value, paise(0)),
        )
        self.message_log[idempotency_key] = receipt
        return receipt

    def accept_instrument_repair(self, case_id: str) -> bool:
        """Customer responds to an instrument-update request, if they would.

        The only route out of a hard decline, and the reason the policy engine
        offers REQUEST_INSTRUMENT_UPDATE as the remediation for one.
        """
        with self._lock:
            if self.truths[case_id].repairable:
                self.repaired.add(case_id)
                return True
            return False

    @property
    def total_message_cost(self) -> Paise:
        """What outreach cost across the batch."""
        return paise(sum(int(r.cost) for r in self.message_log.values()))
