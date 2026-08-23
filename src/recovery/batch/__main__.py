"""Run the pre-registered batches.

    python -m recovery.batch --population 4000 --tail 1600
    python -m recovery.batch --agent scripted        # exercises the loop offline
    python -m recovery.batch --agent live            # real model calls, costs money

Batch A answers R1: does the system beat the platform default. Batch B carries
R2: does the agent tail beat the deterministic fallback on the cases it is
actually applied to.

``--agent off`` runs both batches with the deterministic planners only. In that
mode Batch B is a baseline, not an ablation, and is labelled as such -- there is
no model in the loop to ablate. Reported separately, never blended.
"""

from __future__ import annotations

import argparse
from collections import Counter
from typing import Any

from recovery.agent.client import BudgetedClient, ScriptedClient
from recovery.agent.planner import AgentPlanner
from recovery.agent.router import AgentTailArms, DeterministicArms
from recovery.batch.metrics import (
    ablation_by_subtype,
    ablation_overall,
    compose_tail_contribution,
    summarise,
)
from recovery.batch.runner import CaseOutcome, run_batch
from recovery.domain.money import format_inr, format_signed_inr
from recovery.planner.rules import DeclineConditionalPlanner, PlatformDefaultPlanner
from recovery.sim.generator import generate

# A minimal but coherent two-step script: notice, then debit once it matures.
# Used by --agent scripted so the loop, schema validation, gating and routing can
# be demonstrated without spending money. It is deliberately naive -- it ignores
# decline class entirely -- so a run in this mode shows the harness measuring a
# weak agent as weak rather than flattering whatever is in the loop.
_SCRIPTED_PROPOSALS = [
    {
        "action": "send_predebit_notice",
        "channel": "sms",
        "template_id": "RP_PREDEBIT_01",
        "delay_hours": 0,
        "diagnosis": "Charge failed; a notice is required before any retry.",
        "confidence": 0.6,
        "rationale": "Notice first so a debit can be scheduled 24h later.",
    },
    {
        "action": "retry_debit",
        "channel": "none",
        "template_id": None,
        "delay_hours": 25,
        "diagnosis": "Notice has matured; attempt the debit.",
        "confidence": 0.55,
        "rationale": "Retry once the 24h notice period has elapsed.",
    },
]


def _build_client(args: argparse.Namespace) -> Any | None:
    """Construct the model client for the requested mode and provider.

    Provider SDKs import inside their adapters, so nothing is required unless a
    live run is actually asked for.
    """
    if args.agent == "scripted":
        return ScriptedClient(_SCRIPTED_PROPOSALS)
    if args.agent != "live":
        return None

    if args.provider == "openai":
        from recovery.agent.openai_client import DEFAULT_MODEL, OpenAIClient

        client: Any = OpenAIClient(model=args.model or DEFAULT_MODEL)
    else:
        from recovery.agent.anthropic_client import DEFAULT_MODEL, AnthropicClient

        client = AnthropicClient(model=args.model or DEFAULT_MODEL)

    if args.token_budget > 0:
        return BudgetedClient(client, max_total_tokens=args.token_budget)
    return client


def _build_router(args: argparse.Namespace) -> Any:
    """Assemble the arm router for the requested agent mode."""
    rules = DeclineConditionalPlanner()
    control = PlatformDefaultPlanner()
    client = _build_client(args)
    if client is None:
        return DeterministicArms(treatment=rules, control=control)
    agent = AgentPlanner(client=client, fallback=rules)
    return AgentTailArms(rules=rules, control=control, agent=agent, seed=args.seed)


def _report_ablation(
    tail_outcomes: list[CaseOutcome], pop_outcomes: list[CaseOutcome], mode: str
) -> None:
    """Print R2, or say plainly why there is no R2 to print."""
    if mode == "off":
        print(
            "\n  R2 ABLATION: not run. --agent off means no model was in the loop,\n"
            "  so Batch B above is a deterministic baseline, not an ablation."
        )
        return

    overall = ablation_overall(tail_outcomes)
    print(f"\n  R2 ablation, agent loop vs deterministic fallback (--agent {mode}):")
    print(
        f"    overall                {overall.describe()}  "
        f"n={overall.treatment.total}/{overall.control.total}"
    )

    per_subtype = ablation_by_subtype(tail_outcomes)
    for name, lift in per_subtype.items():
        print(f"    {name:22s} {lift.describe()}  n={lift.treatment.total}/{lift.control.total}")

    rate, money = compose_tail_contribution(tail_outcomes, pop_outcomes, per_subtype)
    print("\n  composed to population (subtype-weighted; rate and money separately):")
    print(f"    rate contribution   {rate:+.4f}")
    print(f"    money contribution  {format_signed_inr(int(money))} per treated case")


def _report_model_usage(router: Any) -> None:
    """Model usage, in tokens first.

    Tokens lead because that is what a free daily allowance meters; price is
    reported only where the provider's rates are known, and its absence is
    stated rather than shown as zero.
    """
    agent = getattr(router, "_agent", None)
    telemetry = getattr(agent, "telemetry", None)
    if telemetry is None or not telemetry.calls:
        return
    print("\n  model usage:")
    print(f"    calls              {telemetry.calls}")
    print(
        f"    tokens             {telemetry.total_tokens:,} "
        f"({telemetry.input_tokens:,} in / {telemetry.output_tokens:,} out)"
    )
    if telemetry.cost_micros:
        print(f"    cost               ${telemetry.cost_micros / 1_000_000:.4f}")
    else:
        print("    cost               unpriced for this model; read tokens above")
    print(f"    mean latency       {telemetry.latency_ms // max(telemetry.calls, 1)} ms")
    print(f"    re-plans           {telemetry.replans}")
    print(f"    fallbacks          {telemetry.fallbacks}")
    print(f"    invalid proposals  {telemetry.invalid_proposals}")
    if telemetry.errors:
        top = Counter(e.split(":")[0] for e in telemetry.errors).most_common(5)
        print(f"    error kinds        {dict(top)}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="recovery.batch")
    parser.add_argument("--population", type=int, default=4000, help="Batch A size")
    parser.add_argument("--tail", type=int, default=1600, help="Batch B size (tail-enriched)")
    parser.add_argument("--seed", type=int, default=20260823)
    parser.add_argument(
        "--agent",
        choices=("off", "scripted", "live"),
        default="off",
        help="off: rules only. scripted: canned proposals, no spend. live: real model calls.",
    )
    parser.add_argument(
        "--provider",
        choices=("openai", "anthropic"),
        default="openai",
        help="Which model provider to use when --agent live.",
    )
    parser.add_argument(
        "--model", default=None, help="Model id; defaults to the provider's default."
    )
    parser.add_argument(
        "--token-budget",
        type=int,
        default=0,
        help=(
            "Stop calling the model after roughly this many tokens and fall back to "
            "rules. 0 disables. Set below a hard daily quota, not at it."
        ),
    )
    args = parser.parse_args()

    population = generate(name="population", size=args.population, seed=args.seed)
    pop_outcomes, pop_provider, _ = run_batch(population, _build_router(args))
    print(summarise(pop_outcomes, label=f"BATCH A - population (seed {args.seed})"))
    print(
        f"\n  provider calls: {pop_provider.charge_calls} charges, "
        f"{pop_provider.message_calls} messages"
    )
    print(f"  message spend:  {format_inr(pop_provider.total_message_cost)}")

    enriched = generate(name="tail_enriched", size=args.tail, seed=args.seed + 1, enriched=True)
    tail_router = _build_router(args)
    tail_outcomes, _, _ = run_batch(enriched, tail_router)

    suffix = (
        "DETERMINISTIC BASELINE (no model in the loop)"
        if args.agent == "off"
        else f"R2 ablation, --agent {args.agent}"
    )
    print()
    print(
        summarise(tail_outcomes, label=f"BATCH B - tail-enriched, {suffix} (seed {args.seed + 1})")
    )

    _report_ablation(tail_outcomes, pop_outcomes, args.agent)
    _report_model_usage(tail_router)

    print(
        "\nNOTE: these are simulation results. The world model encodes the "
        "hypothesis that\nretry timing matters, so it cannot be used to confirm "
        "that hypothesis -- only to\nshow the policy exploits the structure it is "
        "given. See src/recovery/sim/world.py."
    )


if __name__ == "__main__":
    main()
