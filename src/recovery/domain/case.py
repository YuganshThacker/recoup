"""Recovery case lifecycle.

A *case* is one unit of revenue at risk -- here, one failed e-mandate charge
against one invoice. It is the object the whole system reasons about, measures,
and audits.

The lifecycle is a loop, not a line. Real dunning looks like
``notice -> retry -> fail -> re-diagnose -> link -> paid``, so
:data:`CaseState.AWAITING_OUTCOME` cycles back to
:data:`CaseState.DIAGNOSED` for the next attempt until a stopping rule fires.

Two states exist purely to make regulation visible in the state machine:

``NOTICE_PENDING``
    RBI's e-mandate framework requires a pre-debit notification at least 24
    hours before *each* debit, carrying merchant name, amount, debit date/time,
    mandate reference, and reason -- with a per-debit opt-out. A recovery
    attempt on a mandate is therefore a *scheduled* action with a hard lead
    time, never an immediate retry. A case sits here while that clock runs.

    Known unknown, handled conservatively: the sources consulted do not settle
    whether a retry of a *failed* e-mandate debit needs a fresh notice or is
    covered by the original. We assume fresh-notice-per-attempt, which is both
    the safer reading and the safer product behaviour.

``COOLING``
    ``payment.failed`` followed by ``payment.captured`` on the same order is
    documented-normal on UPI -- customers retry inside the session. Acting
    immediately means nagging people who already paid. We hold, then re-check
    payment status before doing anything.

    This applies to the control arm too. Asymmetric cooling would let the
    holdout accrue spurious self-cures and would shrink measured lift for
    reasons unrelated to the agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from recovery.domain.failure import DeclineClass, PaymentMethod
from recovery.domain.money import Paise


class CaseState(StrEnum):
    DETECTED = "detected"
    COOLING = "cooling"
    DIAGNOSED = "diagnosed"
    PLANNED = "planned"
    NOTICE_PENDING = "notice_pending"
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    AWAITING_OUTCOME = "awaiting_outcome"
    RECOVERED = "recovered"
    STOPPED = "stopped"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES


_TERMINAL_STATES: frozenset[CaseState] = frozenset(
    {CaseState.RECOVERED, CaseState.STOPPED},
)


class StopReason(StrEnum):
    """Why a case stopped.

    The track's bar asks for stopping rules; showing *which* rule fired is the
    point. Every non-recovered terminal case carries exactly one of these, and
    the distribution is reported.
    """

    HARD_DECLINE_UNRECOVERABLE = "hard_decline_unrecoverable"
    ATTEMPT_BUDGET_EXHAUSTED = "attempt_budget_exhausted"
    MAX_ESCALATION_REACHED = "max_escalation_reached"
    CUSTOMER_OPTED_OUT = "customer_opted_out"
    MANDATE_REVOKED = "mandate_revoked"
    SUBSCRIPTION_CANCELLED = "subscription_cancelled"
    DISPUTE_OPEN = "dispute_open"
    PROMISE_TO_PAY_PENDING = "promise_to_pay_pending"
    NO_CONSENT = "no_consent"
    OBSERVATION_WINDOW_CLOSED = "observation_window_closed"
    NEGATIVE_EXPECTED_VALUE = "negative_expected_value"


class ExperimentArm(StrEnum):
    """R1: does the agent beat the platform default?"""

    TREATMENT = "treatment"
    CONTROL = "control"


class TailArm(StrEnum):
    """R2: does the LLM pay for itself, on cases where it is applied?

    Assigned only once a case is marked tail-eligible, and only within the R1
    treatment arm. Randomising *within* the tail is what keeps the ablation
    honest -- cases reach the tail because they are hard, so comparing the
    agent against the easy rule-handled population would measure the routing,
    not the model.
    """

    AGENT_LOOP = "agent_loop"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"


class TailSubtype(StrEnum):
    """Why a case was routed to the tail.

    Recorded per case so R2's lift can be computed within subtype and
    reweighted to natural prevalence. Batch B oversamples these, so an
    unstratified aggregate would average over the wrong distribution.
    """

    AMBIGUOUS_DIAGNOSIS = "ambiguous_diagnosis"
    HIGH_VALUE = "high_value"
    INBOUND_FREETEXT = "inbound_freetext"


# A case may only move along these edges. Anything else is a bug, and we would
# rather crash in a test than silently corrupt the measurement.
ALLOWED_TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    CaseState.DETECTED: frozenset({CaseState.COOLING, CaseState.STOPPED}),
    CaseState.COOLING: frozenset(
        {CaseState.DIAGNOSED, CaseState.RECOVERED, CaseState.STOPPED},
    ),
    # RECOVERED is reachable here because diagnosis re-checks payment status
    # before acting, which is where a self-cured invoice is discovered.
    CaseState.DIAGNOSED: frozenset(
        {CaseState.PLANNED, CaseState.RECOVERED, CaseState.STOPPED},
    ),
    CaseState.PLANNED: frozenset(
        {CaseState.NOTICE_PENDING, CaseState.SCHEDULED, CaseState.STOPPED},
    ),
    # Waiting states return to diagnosis: once the clock advances, the case
    # is re-planned rather than resumed blindly.
    CaseState.NOTICE_PENDING: frozenset(
        {CaseState.SCHEDULED, CaseState.DIAGNOSED, CaseState.STOPPED},
    ),
    CaseState.SCHEDULED: frozenset(
        {CaseState.EXECUTING, CaseState.DIAGNOSED, CaseState.STOPPED},
    ),
    CaseState.EXECUTING: frozenset({CaseState.AWAITING_OUTCOME, CaseState.STOPPED}),
    CaseState.AWAITING_OUTCOME: frozenset(
        # The loop: a failed attempt re-enters diagnosis for the next round.
        {CaseState.RECOVERED, CaseState.DIAGNOSED, CaseState.STOPPED},
    ),
    CaseState.RECOVERED: frozenset(),
    CaseState.STOPPED: frozenset(),
}


class IllegalTransition(Exception):
    """Raised when code attempts a lifecycle edge that does not exist."""


def assert_transition(case_id: str, current: CaseState, target: CaseState) -> None:
    """Validate a lifecycle edge, naming the case so failures are traceable."""
    permitted = ALLOWED_TRANSITIONS[current]
    if target not in permitted:
        allowed = ", ".join(sorted(s.value for s in permitted)) or "none (terminal)"
        raise IllegalTransition(
            f"case {case_id}: cannot move {current.value} -> {target.value}; allowed: {allowed}"
        )


@dataclass(slots=True)
class RecoveryCase:
    """One unit of revenue at risk.

    Money is :data:`~recovery.domain.money.Paise` throughout -- integer minor
    units, never float.
    """

    case_id: str
    subscription_id: str
    invoice_id: str
    customer_id: str
    amount: Paise
    method: PaymentMethod

    state: CaseState = CaseState.DETECTED
    stop_reason: StopReason | None = None

    decline_reason: str | None = None
    decline_class: DeclineClass = DeclineClass.UNKNOWN

    # Experiment assignment, fixed at detection time from a seeded PRNG.
    arm: ExperimentArm = ExperimentArm.TREATMENT
    tail_arm: TailArm | None = None
    tail_subtype: TailSubtype | None = None

    attempt_count: int = 0
    detected_at: datetime | None = None
    observation_closes_at: datetime | None = None
    recovered_at: datetime | None = None
    recovered_amount: Paise | None = None

    notes: dict[str, str] = field(default_factory=dict)

    @property
    def is_tail(self) -> bool:
        return self.tail_subtype is not None

    def transition_to(self, target: CaseState, *, stop_reason: StopReason | None = None) -> None:
        """Move to ``target``, validating the edge.

        Reaching :data:`CaseState.STOPPED` requires a reason: an unexplained
        stop is indistinguishable from a dropped case in the audit trail.
        """
        assert_transition(self.case_id, self.state, target)
        if target is CaseState.STOPPED and stop_reason is None:
            raise ValueError(f"case {self.case_id}: STOPPED requires a StopReason")
        if target is not CaseState.STOPPED and stop_reason is not None:
            raise ValueError(
                f"case {self.case_id}: stop_reason is only meaningful for STOPPED, "
                f"got it with {target.value}"
            )
        self.state = target
        self.stop_reason = stop_reason
