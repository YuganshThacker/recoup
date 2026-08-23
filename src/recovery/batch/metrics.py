"""Batch metrics, computed to the pre-registered plan.

The hierarchy in docs/analysis-plan.md is enforced here rather than left to
discipline at reporting time: one primary metric, one secondary, and everything
else marked descriptive. Descriptive slices are computed without confidence
intervals precisely so they cannot be quoted as findings -- a long list of
slices with no declared hierarchy is p-hacking through the side door.

Every interval is a 95% normal-approximation interval on a difference of two
proportions. Where an interval straddles zero, that is the result.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from recovery.batch.runner import CaseOutcome
from recovery.domain.case import ExperimentArm, TailSubtype
from recovery.domain.money import Paise, format_inr, paise

Z_95 = 1.959963985


@dataclass(frozen=True, slots=True)
class Proportion:
    """A rate with its denominator, so n is never lost on the way to a report."""

    successes: int
    total: int

    @property
    def rate(self) -> float:
        return self.successes / self.total if self.total else 0.0

    @property
    def variance_term(self) -> float:
        if not self.total:
            return 0.0
        p = self.rate
        return p * (1 - p) / self.total


@dataclass(frozen=True, slots=True)
class Lift:
    """A difference of two proportions with a 95% interval."""

    treatment: Proportion
    control: Proportion

    @property
    def value(self) -> float:
        return self.treatment.rate - self.control.rate

    @property
    def half_width(self) -> float:
        return Z_95 * math.sqrt(self.treatment.variance_term + self.control.variance_term)

    @property
    def interval(self) -> tuple[float, float]:
        return (self.value - self.half_width, self.value + self.half_width)

    @property
    def straddles_zero(self) -> bool:
        low, high = self.interval
        return low <= 0.0 <= high

    def describe(self) -> str:
        low, high = self.interval
        verdict = "  [straddles zero]" if self.straddles_zero else ""
        return f"{self.value:+.4f}  95% CI [{low:+.4f}, {high:+.4f}]{verdict}"


def _split(outcomes: list[CaseOutcome]) -> tuple[list[CaseOutcome], list[CaseOutcome]]:
    treatment = [o for o in outcomes if o.arm is ExperimentArm.TREATMENT]
    control = [o for o in outcomes if o.arm is ExperimentArm.CONTROL]
    return treatment, control


def _proportion(rows: list[CaseOutcome]) -> Proportion:
    return Proportion(successes=sum(1 for o in rows if o.recovered), total=len(rows))


def recovery_lift(outcomes: list[CaseOutcome]) -> Lift:
    """PRIMARY input: recovery rate, treatment against holdout."""
    treatment, control = _split(outcomes)
    return Lift(treatment=_proportion(treatment), control=_proportion(control))


def incremental_paise(outcomes: list[CaseOutcome]) -> Paise:
    """PRIMARY: incremental rupees recovered, in paise.

    Lift times treated cases times the mean recovered amount. Gross recovered
    would count money the customer would have paid anyway; this does not.
    """
    lift = recovery_lift(outcomes)
    treatment, _ = _split(outcomes)
    recovered = [o for o in treatment if o.recovered]
    if not recovered or lift.value <= 0:
        return paise(0)
    mean_amount = Decimal(sum(int(o.amount) for o in recovered)) / len(recovered)
    return paise(int(Decimal(str(lift.value)) * len(treatment) * mean_amount))


def gross_recovered_paise(outcomes: list[CaseOutcome]) -> Paise:
    """DESCRIPTIVE: everything recovered in the treatment arm, incremental or not."""
    treatment, _ = _split(outcomes)
    return paise(sum(int(o.amount) for o in treatment if o.recovered))


def action_cost_paise(outcomes: list[CaseOutcome]) -> Paise:
    """DESCRIPTIVE: what outreach cost across the treated arm."""
    treatment, _ = _split(outcomes)
    return paise(sum(int(o.action_cost) for o in treatment))


def lift_by_decline_class(outcomes: list[CaseOutcome]) -> dict[str, Lift]:
    """DESCRIPTIVE: the slice that validates the taxonomy.

    Near-zero lift on HARD is the expected and correct result -- it means
    attempts were withheld from instruments that could not succeed, not that
    the policy failed.
    """
    classes = sorted({o.decline_class.value for o in outcomes})
    return {
        name: Lift(
            treatment=_proportion(
                [
                    o
                    for o in outcomes
                    if o.decline_class.value == name and o.arm is ExperimentArm.TREATMENT
                ]
            ),
            control=_proportion(
                [
                    o
                    for o in outcomes
                    if o.decline_class.value == name and o.arm is ExperimentArm.CONTROL
                ]
            ),
        )
        for name in classes
    }


def tail_lift_by_subtype(outcomes: list[CaseOutcome]) -> dict[str, Lift]:
    """SECONDARY input: lift within each tail subtype.

    Computed per subtype because Batch B oversamples the tail; an aggregate
    would average over the enriched mix rather than the natural one. Composition
    to population level uses :func:`compose_tail_contribution`.
    """
    result: dict[str, Lift] = {}
    for subtype in TailSubtype:
        rows = [o for o in outcomes if o.tail_subtype is subtype]
        if rows:
            result[subtype.value] = Lift(
                treatment=_proportion([o for o in rows if o.arm is ExperimentArm.TREATMENT]),
                control=_proportion([o for o in rows if o.arm is ExperimentArm.CONTROL]),
            )
    return result


def natural_subtype_weights(population: list[CaseOutcome]) -> dict[str, float]:
    """Subtype prevalence among tail cases in the *unenriched* population.

    Measured from Batch A, and required to reweight Batch B's enriched lift.
    """
    tail = [o for o in population if o.tail_subtype is not None]
    if not tail:
        return {}
    counts = Counter(o.tail_subtype.value for o in tail if o.tail_subtype)
    return {name: count / len(tail) for name, count in counts.items()}


def compose_tail_contribution(
    enriched: list[CaseOutcome], population: list[CaseOutcome]
) -> tuple[float, float]:
    """Compose enriched tail lift back to the population.

    Returns ``(rate_contribution, money_contribution_paise)``.

    Rate and money are composed separately and deliberately. High-value
    enrichment makes them diverge: the same rate lift concentrated in expensive
    cases is worth more money than one spread evenly, so a single number cannot
    honestly stand for both.
    """
    weights = natural_subtype_weights(population)
    per_subtype = tail_lift_by_subtype(enriched)
    tail_prevalence = sum(1 for o in population if o.tail_subtype is not None) / max(
        len(population), 1
    )

    rate_sum = 0.0
    money_sum = 0.0
    for name, weight in weights.items():
        lift = per_subtype.get(name)
        if lift is None:
            continue
        rate_sum += weight * lift.value
        recovered = [
            o
            for o in enriched
            if o.tail_subtype
            and o.tail_subtype.value == name
            and o.recovered
            and o.arm is ExperimentArm.TREATMENT
        ]
        mean_amount = sum(int(o.amount) for o in recovered) / len(recovered) if recovered else 0.0
        money_sum += weight * lift.value * mean_amount

    return rate_sum * tail_prevalence, money_sum * tail_prevalence


def stop_reason_counts(outcomes: list[CaseOutcome]) -> dict[str, int]:
    """DESCRIPTIVE: which stopping rule fired, and how often."""
    return dict(Counter(o.stop_reason.value for o in outcomes if o.stop_reason).most_common())


def refusal_counts(outcomes: list[CaseOutcome]) -> dict[str, int]:
    """DESCRIPTIVE: policy refusals by code.

    A zero here would mean the compliance engine is decorative rather than
    load-bearing, which the analysis plan lists as a falsifier.
    """
    counter: Counter[str] = Counter()
    for outcome in outcomes:
        counter.update(outcome.refusal_codes)
    return dict(counter.most_common())


def outcome_source_counts(outcomes: list[CaseOutcome]) -> dict[str, dict[str, int]]:
    """DESCRIPTIVE: attributed / organic / none, per arm.

    The organic column is the self-cure baseline made visible. If it differed
    materially between arms, the comparison would be broken.
    """
    return {
        arm.value: dict(Counter(o.outcome_source for o in outcomes if o.arm is arm).most_common())
        for arm in ExperimentArm
    }


def exception_list(outcomes: list[CaseOutcome]) -> dict[str, int]:
    """The honest exception list: cases the system declined to pursue.

    Volunteered rather than buried. Withheld attempts on unrecoverable
    instruments are a correct outcome, not a failure, but they belong in the
    report either way.
    """
    return {
        "hard_decline_withheld": sum(
            1
            for o in outcomes
            if o.stop_reason and o.stop_reason.value == "hard_decline_unrecoverable"
        ),
        "unmapped_escalated": sum(
            1 for o in outcomes if o.stop_reason and o.stop_reason.value == "max_escalation_reached"
        ),
        "window_closed_undecided": sum(
            1
            for o in outcomes
            if o.stop_reason and o.stop_reason.value == "observation_window_closed"
        ),
        "budget_exhausted": sum(
            1
            for o in outcomes
            if o.stop_reason and o.stop_reason.value == "attempt_budget_exhausted"
        ),
    }


def cost_per_rupee_recovered(outcomes: list[CaseOutcome]) -> str:
    """DESCRIPTIVE: outreach spend against incremental recovery."""
    incremental = int(incremental_paise(outcomes))
    if incremental <= 0:
        return "n/a (no incremental recovery)"
    return f"{int(action_cost_paise(outcomes)) / incremental:.4f}"


def summarise(outcomes: list[CaseOutcome], *, label: str) -> str:
    """Render the pre-registered report for one batch."""
    lift = recovery_lift(outcomes)
    lines = [
        f"=== {label} ===",
        f"n = {len(outcomes)}  (treatment {lift.treatment.total}, control {lift.control.total})",
        "",
        "PRIMARY",
        f"  recovery rate   treatment {lift.treatment.rate:.4f}  control {lift.control.rate:.4f}",
        f"  lift            {lift.describe()}",
        f"  incremental     {format_inr(incremental_paise(outcomes))}",
        "",
        "DESCRIPTIVE (no significance claims)",
        f"  gross recovered (treated)  {format_inr(gross_recovered_paise(outcomes))}",
        f"  outreach cost (treated)    {format_inr(action_cost_paise(outcomes))}",
        f"  cost per rupee incremental {cost_per_rupee_recovered(outcomes)}",
        "",
        "  recovery by decline class:",
    ]
    for name, class_lift in lift_by_decline_class(outcomes).items():
        lines.append(
            f"    {name:9s} t={class_lift.treatment.rate:.3f} "
            f"c={class_lift.control.rate:.3f} delta={class_lift.value:+.3f}  "
            f"n={class_lift.treatment.total}/{class_lift.control.total}"
        )

    lines.append("")
    lines.append("  outcome source by arm:")
    for arm, counts in outcome_source_counts(outcomes).items():
        lines.append(f"    {arm:10s} {counts}")

    lines.append("")
    lines.append("  stopping rules fired:")
    for reason, count in stop_reason_counts(outcomes).items():
        lines.append(f"    {reason:32s} {count}")

    lines.append("")
    lines.append("  policy refusals by code:")
    for code, count in refusal_counts(outcomes).items():
        lines.append(f"    {code:32s} {count}")

    lines.append("")
    lines.append("  exception list:")
    for name, count in exception_list(outcomes).items():
        lines.append(f"    {name:32s} {count}")

    return "\n".join(lines)
