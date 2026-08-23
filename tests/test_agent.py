"""Agent tests.

The model is the least predictable component in the system, so what is asserted
here is not that it behaves well -- it is that the system behaves well
regardless of what the model returns. Every test below is a claim about a
failure mode being contained.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from recovery.agent.client import (
    BudgetedClient,
    ModelReply,
    ScriptedClient,
    UnavailableClient,
)
from recovery.agent.planner import AgentPlanner, build_prompt
from recovery.agent.router import AgentTailArms, DeterministicArms
from recovery.agent.schema import (
    PROPOSABLE_ACTIONS,
    InvalidProposal,
    proposal_schema,
    validate,
)
from recovery.domain.case import (
    ExperimentArm,
    RecoveryCase,
    StopReason,
    TailArm,
    TailSubtype,
)
from recovery.domain.failure import DeclineClass, PaymentMethod
from recovery.domain.money import paise
from recovery.planner.rules import DeclineConditionalPlanner, PlannedStep, PlannerFacts, StopPlan
from recovery.policy.actions import ActionKind, Channel
from recovery.policy.gates import PolicyContext
from recovery.templates import REGISTERED

NOW = datetime(2026, 9, 10, 5, 0, tzinfo=UTC)  # 10:30 IST, inside contact hours
KNOWN = frozenset(REGISTERED)

NOTICE = {
    "action": "send_predebit_notice",
    "channel": "sms",
    "template_id": "RP_PREDEBIT_01",
    "delay_hours": 0,
    "diagnosis": "Funds short at attempt time.",
    "confidence": 0.7,
    "rationale": "Notice must precede any debit by 24h.",
}
DEBIT_NOW = {
    "action": "retry_debit",
    "channel": "none",
    "template_id": None,
    "delay_hours": 0,
    "diagnosis": "Funds short at attempt time.",
    "confidence": 0.7,
    "rationale": "Try again immediately.",
}


def make_case(
    *,
    decline_class: DeclineClass = DeclineClass.SOFT,
    reason: str = "insufficient_funds",
    attempts: int = 0,
    amount: int = 49900,
) -> RecoveryCase:
    return RecoveryCase(
        case_id="case_1",
        subscription_id="sub_1",
        invoice_id="inv_1",
        customer_id="cust_1",
        amount=paise(amount),
        method=PaymentMethod.EMANDATE,
        decline_reason=reason,
        decline_class=decline_class,
        attempt_count=attempts,
    )


def make_facts(case: RecoveryCase, *, notice_sent_at: datetime | None = None) -> PlannerFacts:
    context = PolicyContext(
        case=case,
        now=NOW,
        consented_channels=frozenset({Channel.SMS}),
        consented_purposes=frozenset({"payment_recovery"}),
        templates=REGISTERED,
        predebit_notice_sent_at=notice_sent_at,
        card_network="visa",
    )
    return PlannerFacts(
        now=NOW,
        window_closes_at=NOW + timedelta(days=21),
        notice_sent_at=notice_sent_at,
        downtime_active=False,
        instrument_repair_requested=False,
        instrument_repaired=False,
        policy_context=context,
    )


# --- schema: what the model structurally cannot do -------------------------


def test_schema_has_no_amount_field() -> None:
    # The central safety property. A hallucinated figure cannot reach a debit
    # because there is no field for one to travel in.
    schema = proposal_schema(sorted(REGISTERED))
    assert "amount" not in schema["properties"]
    assert schema["additionalProperties"] is False


def test_schema_has_no_free_text_message_field() -> None:
    # Copy on DLT-registered channels is not ours to write, so the model names a
    # template and never composes a message.
    schema = proposal_schema(sorted(REGISTERED))
    properties = set(schema["properties"])
    assert not properties & {"message", "body", "text", "variables"}


def test_an_injected_amount_is_ignored_rather_than_honoured() -> None:
    payload = {**NOTICE, "amount": 999_999_00, "message": "PAY NOW OR ELSE"}
    proposal = validate(payload, known_templates=KNOWN)
    # Validation succeeds, and neither smuggled field appears on the result.
    assert not hasattr(proposal, "amount")
    assert not hasattr(proposal, "message")
    assert proposal.template_id == "RP_PREDEBIT_01"


def test_schema_is_valid_under_strict_mode_rules() -> None:
    # Strict structured-output modes require every property listed in `required`
    # and additionalProperties disabled. A schema rejected at the API boundary
    # would silently lose the constraint it exists to provide.
    schema = proposal_schema(sorted(REGISTERED))
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_nullable_template_id_uses_anyof_not_a_null_bearing_enum() -> None:
    # A union type whose enum also contains null is ambiguous under some strict
    # modes; anyOf is unambiguous everywhere.
    field = proposal_schema(sorted(REGISTERED))["properties"]["template_id"]
    assert "anyOf" in field
    assert {"type": "null"} in field["anyOf"]
    assert "enum" not in field


def test_every_proposable_action_is_a_real_action_kind() -> None:
    assert all(isinstance(a, ActionKind) for a in PROPOSABLE_ACTIONS)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"action": "wire_transfer_to_self"}, "not a known action"),
        ({"action": "send_reminder", "template_id": None}, "named no template"),
        ({"channel": "carrier_pigeon"}, "not a known channel"),
        ({"template_id": "NOT_REGISTERED"}, "not a registered template"),
        ({"delay_hours": -1}, "out of range"),
        ({"delay_hours": 10**9}, "out of range"),
        ({"delay_hours": "soon"}, "must be an integer"),
        ({"confidence": 1.7}, "out of range"),
        ({"confidence": "high"}, "must be a number"),
    ],
)
def test_validate_rejects_malformed_proposals(mutation: dict[str, Any], match: str) -> None:
    payload = {**NOTICE, **mutation}
    with pytest.raises(InvalidProposal, match=match):
        validate(payload, known_templates=KNOWN)


def test_validate_rejects_missing_fields() -> None:
    payload = {k: v for k, v in NOTICE.items() if k != "diagnosis"}
    with pytest.raises(InvalidProposal, match="missing required field"):
        validate(payload, known_templates=KNOWN)


def test_validate_rejects_non_object() -> None:
    with pytest.raises(InvalidProposal, match="expected an object"):
        validate(["not", "an", "object"], known_templates=KNOWN)  # type: ignore[arg-type]


def test_long_text_is_truncated_not_rejected() -> None:
    proposal = validate({**NOTICE, "diagnosis": "x" * 5000}, known_templates=KNOWN)
    assert len(proposal.diagnosis) <= 600


# --- planner: the loop and its floor ---------------------------------------


def test_permitted_proposal_is_returned_and_attributed_to_the_agent() -> None:
    case = make_case()
    planner = AgentPlanner(ScriptedClient([NOTICE]), DeclineConditionalPlanner())
    plan = planner.next_step(case, make_facts(case))
    assert isinstance(plan, PlannedStep)
    assert plan.action.kind is ActionKind.SEND_PREDEBIT_NOTICE
    assert plan.action.proposed_by.value == "agent"
    assert planner.telemetry.fallbacks == 0


def test_variables_are_bound_by_the_system_not_the_model() -> None:
    case = make_case()
    planner = AgentPlanner(ScriptedClient([NOTICE]), DeclineConditionalPlanner())
    plan = planner.next_step(case, make_facts(case))
    assert isinstance(plan, PlannedStep)
    # Exactly the template's required variables, sourced from the registry.
    assert set(plan.action.variables) == set(REGISTERED["RP_PREDEBIT_01"].required_variables)


def test_refused_proposal_triggers_a_replan_with_the_refusal_in_the_prompt() -> None:
    # The demo moment: a debit proposed with no notice is refused, and the model
    # is told which action would unblock it.
    case = make_case()
    client = ScriptedClient([DEBIT_NOW, NOTICE])
    planner = AgentPlanner(client, DeclineConditionalPlanner())

    plan = planner.next_step(case, make_facts(case, notice_sent_at=None))

    assert isinstance(plan, PlannedStep)
    assert plan.action.kind is ActionKind.SEND_PREDEBIT_NOTICE
    assert planner.telemetry.replans == 1
    assert planner.telemetry.fallbacks == 0
    second_prompt = client.prompts[1]
    assert "REFUSED" in second_prompt
    assert "predebit_notice_required" in second_prompt
    assert "send_predebit_notice" in second_prompt


def test_exhausted_replans_fall_back_to_the_deterministic_planner() -> None:
    # A model that keeps proposing the same refused action must not stall the
    # case. The rules path is the floor.
    case = make_case()
    planner = AgentPlanner(ScriptedClient([DEBIT_NOW]), DeclineConditionalPlanner(), max_replans=2)
    plan = planner.next_step(case, make_facts(case, notice_sent_at=None))
    assert planner.telemetry.fallbacks == 1
    assert isinstance(plan, PlannedStep)
    assert plan.action.proposed_by.value == "rules"


def test_model_outage_falls_back_without_raising() -> None:
    case = make_case()
    client = UnavailableClient()
    planner = AgentPlanner(client, DeclineConditionalPlanner())
    plan = planner.next_step(case, make_facts(case))
    assert isinstance(plan, PlannedStep)
    assert planner.telemetry.fallbacks == 1
    assert planner.telemetry.errors == ["model_unavailable"]
    assert client.calls == 1  # an outage is not retried inside the planner


def test_invalid_proposal_falls_back_and_is_counted() -> None:
    case = make_case()
    planner = AgentPlanner(
        ScriptedClient([{**NOTICE, "action": "drain_the_account"}]),
        DeclineConditionalPlanner(),
    )
    plan = planner.next_step(case, make_facts(case))
    assert isinstance(plan, PlannedStep)
    assert planner.telemetry.invalid_proposals == 1
    assert planner.telemetry.fallbacks == 1


def test_model_may_stop_a_case_and_the_reason_is_recorded() -> None:
    case = make_case()
    stop = {**NOTICE, "action": "stop", "channel": "none", "template_id": None}
    planner = AgentPlanner(ScriptedClient([stop]), DeclineConditionalPlanner())
    plan = planner.next_step(case, make_facts(case))
    assert isinstance(plan, StopPlan)
    assert plan.reason is StopReason.MAX_ESCALATION_REACHED


def test_telemetry_accumulates_cost_across_calls() -> None:
    case = make_case()
    planner = AgentPlanner(
        ScriptedClient([DEBIT_NOW], cost_micros=1000), DeclineConditionalPlanner()
    )
    planner.next_step(case, make_facts(case, notice_sent_at=None))
    # Three attempts (initial + two re-plans) before falling back.
    assert planner.telemetry.calls == 3
    assert planner.telemetry.cost_micros == 3000


def test_prompt_never_asks_the_model_for_an_amount() -> None:
    case = make_case()
    prompt = build_prompt(case, make_facts(case), [])
    assert "Rs 499.00" in prompt  # shown for judgement
    assert "RP_PREDEBIT_01" in prompt


# --- cost accounting -------------------------------------------------------


def test_model_reply_failure_is_free() -> None:
    reply = ModelReply.failure("boom", model="m", latency_ms=5)
    assert not reply.ok
    assert reply.cost_micros == 0
    assert reply.total_tokens == 0
    assert reply.payload is None


def test_budgeted_client_stops_calling_once_the_ceiling_is_hit() -> None:
    # A daily free-token allowance is a real constraint. Exhausting it must drop
    # the planner onto its rules floor, not fail the batch. The ceiling is
    # checked before a call against tokens already spent, so it overshoots by at
    # most one call -- asserted here rather than left as a surprise.
    inner = ScriptedClient([NOTICE])
    budgeted = BudgetedClient(inner, max_total_tokens=400)
    kwargs = {"system": "s", "prompt": "p", "schema": {}}

    first = budgeted.propose(**kwargs)
    assert first.ok
    assert budgeted.tokens_used == 420  # 300 in + 120 out, overshooting 400

    second = budgeted.propose(**kwargs)
    assert not second.ok
    assert second.error == "token_budget_exhausted"
    assert inner.calls == 1  # the second never reached the provider
    assert budgeted.refused_for_budget == 1


def test_budget_exhaustion_falls_back_to_rules() -> None:
    case = make_case()
    budgeted = BudgetedClient(ScriptedClient([NOTICE]), max_total_tokens=0)
    planner = AgentPlanner(budgeted, DeclineConditionalPlanner())
    plan = planner.next_step(case, make_facts(case))
    assert isinstance(plan, PlannedStep)
    assert plan.action.proposed_by.value == "rules"
    assert planner.telemetry.fallbacks == 1


def test_telemetry_tracks_tokens_not_only_cost() -> None:
    case = make_case()
    planner = AgentPlanner(ScriptedClient([NOTICE]), DeclineConditionalPlanner())
    planner.next_step(case, make_facts(case))
    assert planner.telemetry.input_tokens == 300
    assert planner.telemetry.output_tokens == 120
    assert planner.telemetry.total_tokens == 420


# --- routing: keeping R2 honest --------------------------------------------


def _tail_case(case_id: str) -> RecoveryCase:
    case = make_case()
    case.case_id = case_id
    case.arm = ExperimentArm.TREATMENT
    case.tail_subtype = TailSubtype.HIGH_VALUE
    return case


def _arms() -> AgentTailArms:
    return AgentTailArms(
        rules=DeclineConditionalPlanner(),
        control=DeclineConditionalPlanner(),
        agent=AgentPlanner(ScriptedClient([NOTICE]), DeclineConditionalPlanner()),
        seed=7,
    )


def test_control_arm_never_reaches_the_agent() -> None:
    # R1 measures the system against the platform default. If the holdout saw
    # the agent, it would not be a holdout.
    arms = _arms()
    case = _tail_case("case_control")
    case.arm = ExperimentArm.CONTROL
    planner = arms.planner_for(case)
    assert not isinstance(planner, AgentPlanner)
    assert case.tail_arm is None


def test_non_tail_treatment_cases_take_the_rules_path() -> None:
    arms = _arms()
    case = _tail_case("case_plain")
    case.tail_subtype = None
    planner = arms.planner_for(case)
    assert not isinstance(planner, AgentPlanner)
    assert case.tail_arm is None


def test_tail_cases_split_roughly_evenly_between_agent_and_fallback() -> None:
    arms = _arms()
    agent_count = 0
    total = 2000
    for index in range(total):
        case = _tail_case(f"case_{index:05d}")
        arms.planner_for(case)
        assert case.tail_arm in (TailArm.AGENT_LOOP, TailArm.DETERMINISTIC_FALLBACK)
        agent_count += case.tail_arm is TailArm.AGENT_LOOP
    assert abs(agent_count / total - 0.5) < 0.05


def test_tail_assignment_is_stable_for_a_given_case() -> None:
    # Hash-derived rather than drawn from a shared PRNG, so adding a case to a
    # batch cannot reshuffle every case after it.
    first, second = _arms(), _arms()
    for index in range(50):
        a, b = _tail_case(f"case_{index}"), _tail_case(f"case_{index}")
        first.planner_for(a)
        second.planner_for(b)
        assert a.tail_arm is b.tail_arm


def test_deterministic_arms_routes_purely_on_experiment_arm() -> None:
    rules, control = DeclineConditionalPlanner(), DeclineConditionalPlanner()
    arms = DeterministicArms(treatment=rules, control=control)
    treated = make_case()
    treated.arm = ExperimentArm.TREATMENT
    held_out = make_case()
    held_out.arm = ExperimentArm.CONTROL
    assert arms.planner_for(treated) is rules
    assert arms.planner_for(held_out) is control
