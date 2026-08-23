"""Structured refusals.

A refusal is not an error string. It is a machine-readable object carrying why
the action was blocked, when it might become permissible, and which action
would unblock it. That shape is what lets the agent re-plan within bounds
instead of guessing, and it is what makes the demo legible: the model proposes a
debit that violates the notice rule, the gate refuses with a remediation, the
model re-plans to send the notice first.

The engine runs **every** gate rather than short-circuiting on the first
refusal. Two reasons, both deliberate:

* An agent handed one violation at a time plays whack-a-mole across several
  turns. Handed all of them at once, it re-plans in one.
* A compliance engine that stops at the first failure cannot demonstrate that
  the remaining gates were ever evaluated. Recording passes is what turns the
  ledger into evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from recovery.policy.actions import ActionKind, ProposedAction


class GateName(StrEnum):
    """The eight gates. Every action clears all of them or it does not execute."""

    CONSENT = "consent"
    SUPPRESSION = "suppression"
    MANDATE = "mandate"
    ATTEMPT_BUDGET = "attempt_budget"
    QUIET_HOURS = "quiet_hours"
    COOLDOWN = "cooldown"
    TEMPLATE = "template"
    CHANNEL_ECONOMICS = "channel_economics"


class RefusalCode(StrEnum):
    """Why a gate refused. Stable identifiers, safe to branch on and to count."""

    # consent
    NO_CONSENT = "no_consent"
    CONSENT_PURPOSE_MISMATCH = "consent_purpose_mismatch"

    # suppression
    SUPPRESSED_DISPUTE = "suppressed_dispute"
    SUPPRESSED_PROMISE_TO_PAY = "suppressed_promise_to_pay"
    SUPPRESSED_DO_NOT_CONTACT = "suppressed_do_not_contact"
    SUBSCRIPTION_CANCELLED = "subscription_cancelled"

    # mandate
    PREDEBIT_NOTICE_REQUIRED = "predebit_notice_required"
    PREDEBIT_NOTICE_NOT_MATURE = "predebit_notice_not_mature"
    PREDEBIT_NOTICE_STALE = "predebit_notice_stale"
    PREDEBIT_OPTED_OUT = "predebit_opted_out"
    AFA_REQUIRED_ABOVE_THRESHOLD = "afa_required_above_threshold"
    MANDATE_REVOKED = "mandate_revoked"
    HARD_DECLINE_NO_RETRY = "hard_decline_no_retry"
    UNCLASSIFIED_DECLINE_NO_RETRY = "unclassified_decline_no_retry"
    DOWNTIME_ACTIVE = "downtime_active"

    # attempt budget
    INTERNAL_ATTEMPT_CAP = "internal_attempt_cap"
    NETWORK_ATTEMPT_CAP = "network_attempt_cap"

    # quiet hours
    OUTSIDE_CONTACT_HOURS = "outside_contact_hours"

    # cooldown
    COOLDOWN_ACTIVE = "cooldown_active"

    # template
    TEMPLATE_REQUIRED = "template_required"
    TEMPLATE_NOT_REGISTERED = "template_not_registered"
    TEMPLATE_VARIABLE_MISMATCH = "template_variable_mismatch"

    # economics
    NEGATIVE_EXPECTED_VALUE = "negative_expected_value"


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's verdict on one proposed action."""

    gate: GateName
    passed: bool
    code: RefusalCode | None = None
    explanation: str = ""

    retry_after: datetime | None = None
    """When this gate would stop refusing, if waiting is enough."""

    remediation: ActionKind | None = None
    """An action that would unblock this one, if any exists."""

    @staticmethod
    def allow(gate: GateName, explanation: str = "") -> GateResult:
        return GateResult(gate=gate, passed=True, explanation=explanation)

    @staticmethod
    def refuse(
        gate: GateName,
        code: RefusalCode,
        explanation: str,
        *,
        retry_after: datetime | None = None,
        remediation: ActionKind | None = None,
    ) -> GateResult:
        return GateResult(
            gate=gate,
            passed=False,
            code=code,
            explanation=explanation,
            retry_after=retry_after,
            remediation=remediation,
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """The engine's verdict, carrying every gate's result.

    Passing gates are retained, not discarded. The audit trail's job is to show
    that the compliance engine ran, and a record of refusals alone cannot do
    that.
    """

    action: ProposedAction
    results: tuple[GateResult, ...]

    @property
    def permitted(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def refusals(self) -> tuple[GateResult, ...]:
        return tuple(r for r in self.results if not r.passed)

    @property
    def codes(self) -> tuple[RefusalCode, ...]:
        return tuple(r.code for r in self.refusals if r.code is not None)

    @property
    def earliest_permissible_at(self) -> datetime | None:
        """When every time-based refusal would have cleared.

        ``None`` means either nothing is blocking, or something is blocking that
        waiting will not fix -- distinguished by :attr:`blocked_permanently`.
        """
        times = [r.retry_after for r in self.refusals if r.retry_after is not None]
        return max(times) if times else None

    @property
    def blocked_permanently(self) -> bool:
        """True when at least one refusal cannot be cleared by waiting or by
        taking a remediating action."""
        return any(r.retry_after is None and r.remediation is None for r in self.refusals)

    @property
    def remediations(self) -> tuple[ActionKind, ...]:
        """Actions that would unblock this one, de-duplicated, order preserved."""
        seen: dict[ActionKind, None] = {}
        for r in self.refusals:
            if r.remediation is not None:
                seen[r.remediation] = None
        return tuple(seen)

    def to_payload(self) -> dict[str, object]:
        """Ledger-ready. Every gate, pass or fail."""
        return {
            "action": self.action.describe(),
            "proposed_by": self.action.proposed_by.value,
            "permitted": self.permitted,
            "gates": [
                {
                    "gate": r.gate.value,
                    "passed": r.passed,
                    "code": r.code.value if r.code else None,
                    "explanation": r.explanation,
                    "retry_after": r.retry_after.isoformat() if r.retry_after else None,
                    "remediation": r.remediation.value if r.remediation else None,
                }
                for r in self.results
            ],
        }

    def explain(self) -> str:
        """Human-readable summary for logs and the drill-down view."""
        if self.permitted:
            return f"permitted: {self.action.describe()}"
        parts = [f"{r.gate.value}={r.code.value if r.code else '?'}" for r in self.refusals]
        return f"refused: {self.action.describe()} ({', '.join(parts)})"
