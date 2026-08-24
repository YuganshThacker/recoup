"""Agent planner: the model proposes, the policy engine disposes.

The loop is the point of this module. The model proposes an action; the policy
engine evaluates it *before* it can execute; a refusal is handed back as a
structured object -- code, explanation, when it clears, and which action would
unblock it -- and the model re-plans against it. Bounded to a small number of
turns, after which the deterministic planner takes over.

This is what "bounded and gated" means concretely. The model never widens its
own permissions, and every path out of this function has been through the same
eight gates as a rules-proposed action:

* a permitted proposal is returned
* a refused proposal is re-planned, then falls back
* a malformed proposal falls back
* a model error or outage falls back

The fallback is not an error path. It is the system's floor, and the ablation
measures whether the model clears it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from recovery.agent.client import LLMClient, ModelReply
from recovery.agent.schema import AgentProposal, InvalidProposal, proposal_schema, validate
from recovery.domain.case import RecoveryCase, StopReason
from recovery.domain.events import Actor
from recovery.domain.money import format_inr
from recovery.planner.base import Planner
from recovery.planner.rules import Plan, PlannedStep, PlannerFacts, StopPlan
from recovery.policy import constants as K
from recovery.policy.actions import ActionKind, ProposedAction
from recovery.policy.decision import Decision
from recovery.policy.engine import PolicyEngine
from recovery.templates import REGISTERED, bind_variables

MAX_REPLANS = 2

SYSTEM_PROMPT = """\
You plan the next step in an Indian subscription payment recovery workflow.

You propose; a deterministic policy engine decides. You cannot execute anything,
and you cannot widen your own permissions. If your proposal is refused you will
be told exactly why, and you should re-plan within those bounds rather than
repeat yourself.

What you must know about the domain:

- HARD declines (expired, blocked, or inactive instruments) can never succeed on
  retry. The only route is asking the customer for a new instrument.
- DOWNTIME declines are the issuer's fault, not the customer's. Waiting is
  better than spending an attempt into an outage.
- SOFT declines are worth retrying, and timing is what matters. Indian payroll
  clusters at month end and the first days of the month, so an account short of
  funds mid-month is usually worth waiting for rather than retrying tomorrow.
- Each mandate debit needs a pre-debit notice at least 24 hours ahead, so a
  debit is always scheduled, never immediate.
- Contact is only permitted between 08:00 and 19:00 IST.

You never write message copy and you never state an amount. You choose a
registered template by id; the system fills in every value from its own ledger.
"""


@dataclass(slots=True)
class AgentTelemetry:
    """Per-run model usage, so the ablation can price the model honestly."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0
    latency_ms: int = 0
    replans: int = 0
    fallbacks: int = 0
    invalid_proposals: int = 0
    errors: list[str] = field(default_factory=list)

    def record(self, reply: ModelReply) -> None:
        self.calls += 1
        self.input_tokens += reply.input_tokens
        self.output_tokens += reply.output_tokens
        self.cost_micros += reply.cost_micros
        self.latency_ms += reply.latency_ms

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _describe_refusal(decision: Decision) -> str:
    """Render a refusal for the model to re-plan against."""
    lines = []
    for result in decision.refusals:
        code = result.code.value if result.code else "refused"
        line = f"- [{result.gate.value}] {code}: {result.explanation}"
        if result.retry_after is not None:
            line += f" (permissible from {result.retry_after.isoformat()})"
        if result.remediation is not None:
            line += f" (unblocked by: {result.remediation.value})"
        lines.append(line)
    return "\n".join(lines)


def _local(moment: datetime) -> str:
    """Render a time in the customer's timezone.

    The model is being asked to reason about an 08:00-19:00 IST contact window,
    so handing it UTC and expecting the conversion is a defect in the prompt
    rather than a weakness in the model. Observed before this change: a proposal
    whose rationale asserted that 10:30 IST was outside 08:00-19:00.
    """
    return moment.astimezone(K.CUSTOMER_TIMEZONE).strftime("%Y-%m-%d %H:%M IST (%a)")


def build_prompt(case: RecoveryCase, facts: PlannerFacts, refusals: list[Decision]) -> str:
    """Everything the model may see about this case.

    Amounts appear as formatted text for judgement only -- the model has no
    field in which to return one.
    """
    notice = _local(facts.notice_sent_at) if facts.notice_sent_at else "none sent for this attempt"
    local_now = facts.now.astimezone(K.CUSTOMER_TIMEZONE)
    in_window = K.CONTACT_WINDOW_OPEN_HOUR <= local_now.hour < K.CONTACT_WINDOW_CLOSE_HOUR
    parts = [
        "Case:",
        f"  failure reason: {case.decline_reason}",
        f"  decline class:  {case.decline_class.value}",
        f"  amount:         {format_inr(case.amount)}",
        f"  attempts used:  {case.attempt_count} of {K.INTERNAL_MAX_ATTEMPTS_PER_CASE} permitted",
        f"  now:            {_local(facts.now)}",
        f"  inside the 08:00-19:00 contact window right now: {in_window}",
        f"  window closes:  {_local(facts.window_closes_at)}",
        f"  pre-debit notice: {notice}",
        f"  provider outage in progress: {facts.downtime_active}",
        f"  instrument update requested: {facts.instrument_repair_requested}",
        f"  instrument replaced: {facts.instrument_repaired}",
        "",
        "Registered templates:",
    ]
    parts.extend(
        f"  {template.template_id} ({template.channel.value})" for template in REGISTERED.values()
    )

    for index, decision in enumerate(refusals, start=1):
        parts.extend(
            [
                "",
                f"Your proposal #{index} ({decision.action.describe()}) was REFUSED:",
                _describe_refusal(decision),
            ]
        )
    if refusals:
        parts.append("\nPropose something permitted under those constraints.")
    return "\n".join(parts)


def _to_plan(proposal: AgentProposal, facts: PlannerFacts) -> Plan:
    """Turn a validated proposal into a plan.

    Variables are bound from the ledger by template id. The model's influence
    over what a customer reads is limited to which approved template it names.
    """
    at = facts.now + timedelta(hours=proposal.delay_hours)

    if proposal.action is ActionKind.STOP:
        return StopPlan(StopReason.MAX_ESCALATION_REACHED, proposal.rationale)

    variables = bind_variables(proposal.template_id) if proposal.template_id else {}
    action = ProposedAction(
        kind=proposal.action,
        channel=proposal.channel,
        template_id=proposal.template_id,
        variables=variables,
        proposed_by=Actor.AGENT,
        rationale=proposal.rationale,
    )
    return PlannedStep(action=action, at=at, rationale=proposal.rationale)


class AgentPlanner:
    """Model-in-the-loop planner with a deterministic floor."""

    def __init__(
        self,
        client: LLMClient,
        fallback: Planner,
        engine: PolicyEngine | None = None,
        *,
        max_replans: int = MAX_REPLANS,
    ) -> None:
        self._client = client
        self._fallback = fallback
        self._engine = engine or PolicyEngine()
        self._max_replans = max_replans
        self.telemetry = AgentTelemetry()

    def next_step(self, case: RecoveryCase, facts: PlannerFacts) -> Plan:
        """Propose, gate, re-plan on refusal, fall back if that runs out."""
        schema = proposal_schema(sorted(REGISTERED))
        known = frozenset(REGISTERED)
        refusals: list[Decision] = []

        for attempt in range(self._max_replans + 1):
            reply = self._client.propose(
                system=SYSTEM_PROMPT,
                prompt=build_prompt(case, facts, refusals),
                schema=schema,
            )
            self.telemetry.record(reply)

            if not reply.ok or reply.payload is None:
                self.telemetry.errors.append(reply.error or "unknown")
                break

            try:
                proposal = validate(reply.payload, known_templates=known)
            except InvalidProposal as exc:
                self.telemetry.invalid_proposals += 1
                self.telemetry.errors.append(f"invalid_proposal:{exc}")
                break

            plan = _to_plan(proposal, facts)
            if isinstance(plan, StopPlan):
                return plan

            if facts.policy_context is None:
                return plan
            decision = self._engine.evaluate(plan.action, facts.policy_context)
            if decision.permitted:
                return plan

            refusals.append(decision)
            if attempt < self._max_replans:
                self.telemetry.replans += 1

        self.telemetry.fallbacks += 1
        return self._fallback.next_step(case, facts)
