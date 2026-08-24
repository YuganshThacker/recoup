"""Deterministic planners.

Two of them, and the difference between them is the hypothesis under test.

:class:`PlatformDefaultPlanner` reproduces what the payment platform does on its
own: a pre-debit notice, then a retry, repeated on consecutive days until the
attempts run out. It is decline-blind -- it makes the same three attempts on a
card that expired as on an account that was briefly short of funds.

:class:`DeclineConditionalPlanner` is the agent's rules path. It reads the
decline class and acts on what that class implies:

* HARD -- no debit will ever succeed, so do not attempt one. Ask for a new
  instrument instead, and stop if the customer does not provide one.
* DOWNTIME -- the failure is the issuer's, not the customer's. Wait for the
  outage to clear rather than spending attempts into it.
* SOFT, insufficient funds -- retry when money is likely to be there. Indian
  payroll clusters at month end and the first days of the month, so the retry
  is aimed at the next such window rather than at tomorrow.
* SOFT, transient -- clears on its own within a day or two; retry shortly.
* UNKNOWN -- unnamed failure mode. Escalate for diagnosis rather than guessing.

The agent's cash-in estimate is deliberately imperfect. It aims at a fixed hour
on the target day, while funds actually land at an unknown time within it, so a
well-timed retry still misses sometimes. Modelling the agent as knowing the
answer exactly would manufacture a result rather than measure one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from recovery.domain.case import RecoveryCase, StopReason
from recovery.domain.events import Ledger
from recovery.domain.failure import DeclineClass
from recovery.policy import constants as K
from recovery.policy.actions import ActionKind, Channel, ProposedAction
from recovery.policy.gates import PolicyContext
from recovery.templates import bind_variables

NOTICE_TEMPLATE = "RP_PREDEBIT_01"
REMINDER_TEMPLATE = "RP_DUNNING_01"
REPAIR_TEMPLATE = "RP_INSTRUMENT_01"

# The hour the agent aims a retry at. Funds land at an unknown time within the
# day, so this is a good guess rather than a correct one.
RETRY_HOUR_IST = 10

# How long the agent waits after an outage clears before trying again.
DOWNTIME_BUFFER = timedelta(hours=2)


@dataclass(frozen=True, slots=True)
class PlannedStep:
    """An action and the moment to take it."""

    action: ProposedAction
    at: datetime
    rationale: str


@dataclass(frozen=True, slots=True)
class StopPlan:
    """No further action; the case terminates."""

    reason: StopReason
    rationale: str


Plan = PlannedStep | StopPlan


@dataclass(frozen=True, slots=True)
class PlannerFacts:
    """What the planner can observe. Deliberately excludes ground truth."""

    now: datetime
    window_closes_at: datetime
    notice_sent_at: datetime | None
    downtime_active: bool
    instrument_repair_requested: bool
    instrument_repaired: bool

    policy_context: PolicyContext | None = None
    """The gate context, so a planner may pre-check its own proposal.

    The deterministic planners ignore this -- they are written to stay inside
    the envelope. The agent planner uses it to gate a proposal before returning
    it, which is what lets a refusal be fed back for re-planning instead of
    surfacing as a dead end in the runner."""

    audit: Ledger | None = None
    """Where a planner records its own reasoning.

    A sink rather than an observation, which sits oddly among facts, but the
    alternative is worse: without it the model's proposals and the refusals it
    re-plans against never reach the ledger, and the sequence the audit trail
    most needs to show -- propose, refuse, re-plan -- is invisible in it."""


def _notice(at: datetime, rationale: str) -> PlannedStep:
    return PlannedStep(
        action=ProposedAction(
            kind=ActionKind.SEND_PREDEBIT_NOTICE,
            channel=Channel.SMS,
            template_id=NOTICE_TEMPLATE,
            variables=bind_variables(NOTICE_TEMPLATE),
            rationale=rationale,
        ),
        at=at,
        rationale=rationale,
    )


def _debit(at: datetime, rationale: str) -> PlannedStep:
    return PlannedStep(
        action=ProposedAction(kind=ActionKind.RETRY_DEBIT, rationale=rationale),
        at=at,
        rationale=rationale,
    )


def _repair_request(at: datetime, rationale: str) -> PlannedStep:
    return PlannedStep(
        action=ProposedAction(
            kind=ActionKind.REQUEST_INSTRUMENT_UPDATE,
            channel=Channel.SMS,
            template_id=REPAIR_TEMPLATE,
            variables=bind_variables(REPAIR_TEMPLATE),
            rationale=rationale,
        ),
        at=at,
        rationale=rationale,
    )


class PlatformDefaultPlanner:
    """Notice, retry, repeat daily. Decline-blind, like the platform default."""

    max_attempts = 3

    def next_step(self, case: RecoveryCase, facts: PlannerFacts) -> Plan:
        if case.attempt_count >= self.max_attempts:
            return StopPlan(
                StopReason.ATTEMPT_BUDGET_EXHAUSTED,
                f"platform default exhausts {self.max_attempts} attempts, then halts",
            )
        if facts.notice_sent_at is None:
            return _notice(facts.now, "notice ahead of the next scheduled attempt")
        matures = facts.notice_sent_at + K.PREDEBIT_NOTICE_LEAD
        return _debit(max(facts.now, matures), "fixed daily retry")


class DeclineConditionalPlanner:
    """The agent's rules path: act on what the decline class implies."""

    max_attempts = K.INTERNAL_MAX_ATTEMPTS_PER_CASE

    def next_step(self, case: RecoveryCase, facts: PlannerFacts) -> Plan:
        if case.attempt_count >= self.max_attempts:
            return StopPlan(StopReason.ATTEMPT_BUDGET_EXHAUSTED, "internal attempt budget spent")

        klass = case.decline_class
        if klass is DeclineClass.HARD:
            return self._plan_hard(facts)
        if klass is DeclineClass.UNKNOWN:
            return StopPlan(
                StopReason.MAX_ESCALATION_REACHED,
                "unmapped decline; escalated for diagnosis rather than retried",
            )
        return self._plan_retryable(case, facts)

    @staticmethod
    def _plan_hard(facts: PlannerFacts) -> Plan:
        """No debit can succeed. The instrument is the only thing that can change."""
        if facts.instrument_repaired:
            return _notice(facts.now, "instrument replaced; notice ahead of retry")
        if facts.instrument_repair_requested:
            return StopPlan(
                StopReason.HARD_DECLINE_UNRECOVERABLE,
                "hard decline and the customer did not replace the instrument",
            )
        return _repair_request(facts.now, "hard decline; only a new instrument can recover this")

    def _plan_retryable(self, case: RecoveryCase, facts: PlannerFacts) -> Plan:
        """SOFT and DOWNTIME: the question is only when."""
        if case.decline_class is DeclineClass.DOWNTIME and facts.downtime_active:
            return PlannedStep(
                action=ProposedAction(kind=ActionKind.WAIT, rationale="outage in progress"),
                at=facts.now + DOWNTIME_BUFFER,
                rationale="waiting out the outage rather than spending an attempt into it",
            )

        target = self._target_debit_time(case, facts)
        if facts.notice_sent_at is None:
            # The notice needs 24h of lead, so it goes out ahead of the target.
            send_at = max(facts.now, target - K.PREDEBIT_NOTICE_LEAD)
            return _notice(send_at, f"notice timed so the debit can land at {target.isoformat()}")

        matures = facts.notice_sent_at + K.PREDEBIT_NOTICE_LEAD
        return _debit(max(target, matures), "retry timed to expected fund availability")

    @staticmethod
    def _target_debit_time(case: RecoveryCase, facts: PlannerFacts) -> datetime:
        """When this case is most likely to succeed, bounded by the horizon.

        An attempt scheduled past the observation window is worth exactly
        nothing, so a best-guess time beyond it is clamped back to the last
        moment that still leaves room for the mandatory 24h notice. Waiting for
        a theoretically better moment that never arrives is strictly worse than
        trying at a worse one that does.
        """
        latest = facts.window_closes_at - K.PREDEBIT_NOTICE_LEAD - timedelta(hours=1)
        if case.decline_reason == "insufficient_funds":
            return min(_next_payday_estimate(facts.now), max(latest, facts.now))
        if case.decline_reason == "transaction_limit_exceeded":
            return facts.now + timedelta(days=1, hours=2)
        if case.decline_class is DeclineClass.DOWNTIME:
            return facts.now + DOWNTIME_BUFFER
        # Transient customer-side failures clear quickly.
        return facts.now + timedelta(hours=18)


def _next_payday_estimate(after: datetime) -> datetime:
    """The agent's guess at when funds return.

    Aims at a fixed hour on the next month-boundary day. Real deposits land at
    an unknown time within that day, so this is frequently a little early --
    which is the point: the estimate is informed, not omniscient.
    """
    candidate = after + timedelta(hours=6)
    for _ in range(40):
        if candidate.day <= 3 or candidate.day >= 28:
            return candidate.replace(hour=RETRY_HOUR_IST, minute=0, second=0, microsecond=0)
        candidate += timedelta(days=1)
    return after + timedelta(days=7)
