"""Run the pre-registered batches.

    python -m recovery.batch --population 4000 --tail 1600 --seed 20260823

Batch A answers R1: does the system beat the platform default.

Batch B is the vehicle for R2 -- does the agent tail beat the deterministic
fallback on the cases it is actually applied to. **The agent loop does not exist
yet**, so today Batch B compares the same two deterministic planners on
tail-enriched cases. That is a useful baseline and it exercises the composition
maths, but it is not the ablation, and it is labelled accordingly until there is
a model in the loop to ablate.

The batches are reported separately and never blended.
"""

from __future__ import annotations

import argparse

from recovery.batch.metrics import compose_tail_contribution, summarise, tail_lift_by_subtype
from recovery.batch.runner import run_batch
from recovery.domain.money import format_inr, format_signed_inr
from recovery.planner.rules import DeclineConditionalPlanner, PlatformDefaultPlanner
from recovery.sim.generator import generate


def main() -> None:
    parser = argparse.ArgumentParser(prog="recovery.batch")
    parser.add_argument("--population", type=int, default=4000, help="Batch A size")
    parser.add_argument("--tail", type=int, default=1600, help="Batch B size (tail-enriched)")
    parser.add_argument("--seed", type=int, default=20260823)
    args = parser.parse_args()

    population = generate(name="population", size=args.population, seed=args.seed)
    pop_outcomes, pop_provider, _ = run_batch(
        population, DeclineConditionalPlanner(), PlatformDefaultPlanner()
    )
    print(summarise(pop_outcomes, label=f"BATCH A - population (seed {args.seed})"))
    print(
        f"\n  provider calls: {pop_provider.charge_calls} charges, "
        f"{pop_provider.message_calls} messages"
    )
    print(f"  message spend:  {format_inr(pop_provider.total_message_cost)}")

    enriched = generate(name="tail_enriched", size=args.tail, seed=args.seed + 1, enriched=True)
    tail_outcomes, _, _ = run_batch(enriched, DeclineConditionalPlanner(), PlatformDefaultPlanner())
    print()
    print(
        summarise(
            tail_outcomes,
            label=f"BATCH B - tail-enriched, DETERMINISTIC BASELINE "
            f"(not yet the LLM ablation) (seed {args.seed + 1})",
        )
    )

    print("\n  lift within tail subtype:")
    for name, lift in tail_lift_by_subtype(tail_outcomes).items():
        print(f"    {name:22s} {lift.describe()}  n={lift.treatment.total}/{lift.control.total}")

    rate_contrib, money_contrib = compose_tail_contribution(tail_outcomes, pop_outcomes)
    print("\n  composed to population (subtype-weighted, rate and money separately):")
    print(f"    rate contribution   {rate_contrib:+.4f}")
    print(f"    money contribution  {format_signed_inr(int(money_contrib))} per treated case")

    print(
        "\nNOTE: these are simulation results. The world model encodes the "
        "hypothesis that\nretry timing matters, so it cannot be used to confirm "
        "that hypothesis -- only to\nshow the policy exploits the structure it is "
        "given. See src/recovery/sim/world.py."
    )


if __name__ == "__main__":
    main()
