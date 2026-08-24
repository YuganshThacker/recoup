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
import os
from collections import Counter
from pathlib import Path
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
from recovery.env import CredentialError, describe_credentials, load_dotenv, validate_credential
from recovery.planner.rules import DeclineConditionalPlanner, PlatformDefaultPlanner
from recovery.report.data import ReportInputs, build
from recovery.report.html import render
from recovery.sim.generator import generate

# A minimal but coherent two-step script: notice, then debit once it matures.
# Used by --agent scripted so the loop, schema validation, gating and routing can
# be demonstrated without spending money. It is deliberately naive -- it ignores
# decline class entirely -- so a run in this mode shows the harness measuring a
# weak agent as weak rather than flattering whatever is in the loop.
_SCRIPTED_PROPOSALS: list[dict[str, Any] | None] = [
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


_REQUIRED_KEY = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def _require_credentials(provider: str) -> None:
    """Fail before generating a batch, not after.

    A live run that discovers a missing key on its first model call has already
    spent minutes building cases, and every one of them silently falls back to
    rules -- producing a report that looks like a finished ablation and is not
    one. Checking up front is the difference between an error and a misleading
    result.
    """
    key = _REQUIRED_KEY[provider]
    try:
        validate_credential(key, os.environ.get(key))
    except CredentialError as exc:
        raise SystemExit(
            f"{exc}, so --agent live cannot run.\n"
            f"Put it in .env at the repository root (gitignored), or export it.\n"
            f"Credentials currently visible: {describe_credentials()}"
        ) from exc


# Share of the run's token budget reserved for the tail batch. Batch A runs
# first, and with one shared ceiling it simply consumed everything before Batch
# B started -- starving the very comparison the run exists to make. R2 is the
# scarce, interesting measurement, so it gets the larger explicit reservation.
DEFAULT_TAIL_BUDGET_SHARE = 0.8


def _build_client(args: argparse.Namespace) -> Any | None:
    """Construct the model client for the requested mode and provider.

    Provider SDKs import inside their adapters, so nothing is required unless a
    live run is actually asked for.
    """
    if args.agent == "scripted":
        return ScriptedClient(_SCRIPTED_PROPOSALS)
    if args.agent != "live":
        return None

    _require_credentials(args.provider)
    if args.provider == "openai":
        from recovery.agent.openai_client import DEFAULT_MODEL, OpenAIClient

        client: Any = OpenAIClient(model=args.model or DEFAULT_MODEL)
    else:
        from recovery.agent.anthropic_client import DEFAULT_MODEL, AnthropicClient

        client = AnthropicClient(model=args.model or DEFAULT_MODEL)

    return client


def _allocate(client: Any | None, budget: int, share: float) -> Any | None:
    """Wrap a client with this batch's slice of the run budget."""
    if client is None or budget <= 0:
        return client
    return BudgetedClient(client, max_total_tokens=int(budget * share))


def _build_router(args: argparse.Namespace, client: Any | None) -> Any:
    """Assemble the arm router for the requested agent mode.

    The client is built once by the caller and shared across both batches, so
    --token-budget caps the whole run. A per-batch budget would silently permit
    twice the tokens the operator asked for.
    """
    rules = DeclineConditionalPlanner()
    control = PlatformDefaultPlanner()
    if client is None:
        return DeterministicArms(treatment=rules, control=control)
    agent = AgentPlanner(client=client, fallback=rules)
    return AgentTailArms(rules=rules, control=control, agent=agent, seed=args.seed)


# Above this share of failed model calls, the agent arm is mostly the rules
# path wearing an agent label, and the comparison stops meaning anything.
VOID_ABLATION_FAILURE_RATE = 0.25


def _ablation_is_void(router: Any) -> str | None:
    """Reason the R2 comparison cannot be believed, if there is one.

    A run whose model calls all failed still completes -- by design, since the
    rules path is the floor -- and still prints a lift with a confidence
    interval. That number would be the rules path compared against itself while
    claiming to be an ablation, which is worse than an error because it looks
    like a result. So the failure rate is checked and the finding withdrawn.
    """
    telemetry = getattr(getattr(router, "_agent", None), "telemetry", None)
    if telemetry is None or not telemetry.calls:
        return None
    rate = len(telemetry.errors) / telemetry.calls
    if rate < VOID_ABLATION_FAILURE_RATE:
        return None
    kinds = Counter(e.split(":")[0] for e in telemetry.errors).most_common(3)
    return (
        f"{rate:.0%} of model calls failed ({dict(kinds)}), so the agent arm is "
        "mostly the deterministic fallback. The R2 numbers below are not an "
        "ablation and must not be reported as one."
    )


def _report_ablation(
    tail_outcomes: list[CaseOutcome],
    pop_outcomes: list[CaseOutcome],
    mode: str,
    router: Any,
) -> None:
    """Print R2, or say plainly why there is no R2 to print."""
    if mode == "off":
        print(
            "\n  R2 ABLATION: not run. --agent off means no model was in the loop,\n"
            "  so Batch B above is a deterministic baseline, not an ablation."
        )
        return

    void_reason = _ablation_is_void(router)
    if void_reason:
        print(f"\n  R2 ABLATION VOID: {void_reason}")

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


def _report_budget(label: str, client: Any, router: Any) -> None:
    """Per-batch spend and model usage, so a starved batch is obvious."""
    telemetry = getattr(getattr(router, "_agent", None), "telemetry", None)
    if telemetry is None or not telemetry.calls:
        return
    real = telemetry.calls - len(
        [e for e in telemetry.errors if e.startswith("token_budget_exhausted")]
    )
    per_call = telemetry.total_tokens // max(real, 1)
    line = (
        f"\n  {label}: {telemetry.calls} planner calls ({real} reached the model), "
        f"{telemetry.total_tokens:,} tokens, {telemetry.fallbacks} fallbacks"
        f"\n    ~{per_call} tokens/call; full demand would be "
        f"~{telemetry.calls * per_call:,} tokens"
    )
    if isinstance(client, BudgetedClient):
        line += f"\n    budget {client.tokens_used:,} of {client._max_total_tokens:,}"
        if client.exhausted:
            line += "  [EXHAUSTED]"
    print(line)


def _write_report(
    args: argparse.Namespace,
    pop_outcomes: list[CaseOutcome],
    pop_ledger: Any,
    tail_outcomes: list[CaseOutcome],
    tail_ledger: Any,
    tail_router: Any,
) -> None:
    """Emit the audit report for whichever batch carries the ablation.

    The tail batch is written when the model was in the loop, because that is
    where the refusal-and-re-plan traces live. Otherwise the population batch,
    which is the one carrying the primary result.
    """
    use_tail = args.agent != "off"
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build(
        ReportInputs(
            label="tail-enriched (Batch B)" if use_tail else "population (Batch A)",
            outcomes=tail_outcomes if use_tail else pop_outcomes,
            ledger=tail_ledger if use_tail else pop_ledger,
            seed=args.seed + (1 if use_tail else 0),
            agent_mode=args.agent,
            model=args.model if args.agent == "live" else None,
            void_reason=_ablation_is_void(tail_router) if use_tail else None,
        )
    )
    path.write_text(render(payload), encoding="utf-8")
    print(f"\n  audit report: {path}  ({path.stat().st_size // 1024} KB)")


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
        "--report",
        default=None,
        help=(
            "Write a self-contained HTML audit report here. Opens with no server "
            "and no network, so the audit trail is inspectable without running anything."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Concurrent cases. Live runs are network-bound, so this is the "
            "difference between minutes and an hour. Outcomes are unchanged."
        ),
    )
    parser.add_argument(
        "--tail-budget-share",
        type=float,
        default=DEFAULT_TAIL_BUDGET_SHARE,
        help=(
            "Share of --token-budget reserved for the tail batch. R2 is the "
            "scarce measurement; the population batch runs first and will "
            "otherwise consume the lot."
        ),
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

    loaded = load_dotenv()
    if loaded and args.agent == "live":
        # Key names only. A value must never reach a log or a transcript.
        print(f"loaded from .env: {', '.join(sorted(loaded))}\n")

    base_client = _build_client(args)
    pop_client = _allocate(base_client, args.token_budget, 1 - args.tail_budget_share)
    tail_client = _allocate(base_client, args.token_budget, args.tail_budget_share)
    population = generate(name="population", size=args.population, seed=args.seed)
    pop_router = _build_router(args, pop_client)
    pop_outcomes, pop_provider, pop_ledger = run_batch(population, pop_router, workers=args.workers)
    print(summarise(pop_outcomes, label=f"BATCH A - population (seed {args.seed})"))
    print(
        f"\n  provider calls: {pop_provider.charge_calls} charges, "
        f"{pop_provider.message_calls} messages"
    )
    print(f"  message spend:  {format_inr(pop_provider.total_message_cost)}")

    enriched = generate(name="tail_enriched", size=args.tail, seed=args.seed + 1, enriched=True)
    tail_router = _build_router(args, tail_client)
    tail_outcomes, _, tail_ledger = run_batch(enriched, tail_router, workers=args.workers)

    suffix = (
        "DETERMINISTIC BASELINE (no model in the loop)"
        if args.agent == "off"
        else f"R2 ablation, --agent {args.agent}"
    )
    print()
    print(
        summarise(tail_outcomes, label=f"BATCH B - tail-enriched, {suffix} (seed {args.seed + 1})")
    )

    _report_ablation(tail_outcomes, pop_outcomes, args.agent, tail_router)
    _report_model_usage(tail_router)

    _report_budget("population", pop_client, pop_router)
    _report_budget("tail", tail_client, tail_router)

    if args.report:
        _write_report(args, pop_outcomes, pop_ledger, tail_outcomes, tail_ledger, tail_router)

    print(
        "\nNOTE: these are simulation results. The world model encodes the "
        "hypothesis that\nretry timing matters, so it cannot be used to confirm "
        "that hypothesis -- only to\nshow the policy exploits the structure it is "
        "given. See src/recovery/sim/world.py."
    )


if __name__ == "__main__":
    main()
