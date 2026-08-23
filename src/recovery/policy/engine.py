"""Policy engine: runs every gate, records the verdict, returns a decision.

The engine is the only path to executing an action. Rules, the model, and a
human operator all propose through the same interface and are gated
identically -- so "the AI did something it shouldn't" is not a failure mode the
architecture admits, independent of how the model behaves.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol

from recovery.domain.events import Actor, EventKind, Ledger
from recovery.policy.actions import ActionKind, ProposedAction
from recovery.policy.decision import Decision, GateResult
from recovery.policy.gates import ALL_GATES, PolicyContext


class Gate(Protocol):
    def __call__(self, action: ProposedAction, ctx: PolicyContext) -> GateResult: ...


class PolicyEngine:
    """Evaluates proposed actions against the compliance envelope."""

    def __init__(self, gates: Sequence[Gate] = ALL_GATES) -> None:
        self._gates = tuple(gates)

    def evaluate(self, action: ProposedAction, ctx: PolicyContext) -> Decision:
        """Run every gate. No short-circuit.

        Evaluating all gates costs nothing here -- they are pure in-memory
        checks -- and buys two things: an agent can fix every violation in one
        re-plan, and the ledger can show that the whole envelope was applied
        rather than just the first thing that happened to fail.
        """
        results = tuple(gate(action, ctx) for gate in self._gates)
        return Decision(action=action, results=results)

    def evaluate_and_record(
        self, action: ProposedAction, ctx: PolicyContext, ledger: Ledger
    ) -> Decision:
        """Evaluate and append the full result to the case's audit trail."""
        decision = self.evaluate(action, ctx)
        ledger.record(
            case_id=ctx.case.case_id,
            kind=EventKind.POLICY_EVALUATED if decision.permitted else EventKind.ACTION_REFUSED,
            actor=Actor.RULES,
            summary=decision.explain(),
            payload=decision.to_payload(),
        )
        return decision

    def first_permitted(
        self, candidates: Iterable[ProposedAction], ctx: PolicyContext
    ) -> tuple[Decision | None, list[Decision]]:
        """Evaluate candidates in order, returning the first permitted one.

        Returns ``(chosen, all_decisions)``. Every decision is returned, not
        only the winner, because the refusals are the interesting part of the
        audit trail: they show what the system considered and declined to do.
        """
        decisions: list[Decision] = []
        chosen: Decision | None = None
        for action in candidates:
            decision = self.evaluate(action, ctx)
            decisions.append(decision)
            if decision.permitted and chosen is None:
                chosen = decision
        return chosen, decisions


def remediation_plan(decision: Decision) -> list[ActionKind]:
    """Actions that would unblock a refused decision, if any.

    This is the structured refusal doing its job: the agent is handed a
    concrete next move rather than an error message to interpret. Empty means
    nothing can be done now -- either the block is permanent, or the only
    option is to wait until :attr:`Decision.earliest_permissible_at`.
    """
    if decision.permitted:
        return []
    return list(decision.remediations)
