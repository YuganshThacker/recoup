"""Batch generator.

Produces the two batches described in docs/analysis-plan.md:

* **population** -- the published decline mix, for the system-level holdout (R1)
* **tail_enriched** -- deliberately oversampled ambiguous and high-value cases,
  for the model ablation (R2)

Batch B is this same generator with different sampling weights, not a second
codebase. That matters for the composition step: because we choose the tail's
composition, we can stratify by subtype and reweight to natural prevalence.
A tail we merely stumbled into could not be reweighted, because we would not
know what we had.

Assignment to arms is stratified by decline class and amount decile so the arms
are comparable, and derived from a recorded seed so any run reproduces exactly.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from recovery.domain.case import ExperimentArm, RecoveryCase
from recovery.domain.failure import PaymentMethod, classify
from recovery.domain.money import paise
from recovery.sim.world import GroundTruth, draw_amount, draw_decline_reason, make_ground_truth

DEFAULT_OBSERVATION_WINDOW = timedelta(days=21)

# Failures arrive continuously. Clustering them into a few days would make
# every case's distance to the next payday nearly identical, which would
# turn timing -- the variable under test -- into a constant.
ARRIVAL_SPREAD = timedelta(days=35)

# Batch B weights. Unmapped codes drive the ambiguous-diagnosis subtype and the
# larger price points drive high-value, so enrichment is a matter of tilting
# these two distributions rather than inventing new case shapes.
_TAIL_UNMAPPED_RATE = 0.34
_TAIL_HIGH_VALUE_RATE = 0.40
_TAIL_REPLY_RATE = 0.30

_HIGH_VALUE_POINTS = (4_999_00, 14_999_00, 24_999_00, 49_999_00)


@dataclass(frozen=True, slots=True)
class SimCase:
    """A generated case: what the system sees, plus what is actually true."""

    case: RecoveryCase
    truth: GroundTruth
    failed_at: datetime


@dataclass(frozen=True, slots=True)
class Batch:
    """A generated batch and the parameters that produced it."""

    name: str
    seed: int
    cases: tuple[SimCase, ...]
    observation_window: timedelta

    @property
    def size(self) -> int:
        return len(self.cases)

    def by_arm(self, arm: ExperimentArm) -> tuple[SimCase, ...]:
        return tuple(c for c in self.cases if c.case.arm is arm)


def _amount_decile(amount: int, boundaries: list[int]) -> int:
    """Which amount decile this case falls in, for stratified assignment."""
    for index, boundary in enumerate(boundaries):
        if amount <= boundary:
            return index
    return len(boundaries)


def _assign_arms(cases: list[SimCase], rng: random.Random) -> None:
    """Stratified 50/50 assignment.

    Balancing on decline class and amount decile keeps the arms comparable on
    the two variables that most affect whether a case recovers -- so an
    observed difference is more plausibly the policy and less plausibly a
    lopsided draw.
    """
    amounts = sorted(int(c.case.amount) for c in cases)
    boundaries = [amounts[int(len(amounts) * q / 10)] for q in range(1, 10)] if amounts else []

    strata: dict[tuple[str, int], list[SimCase]] = defaultdict(list)
    for sim in cases:
        key = (sim.case.decline_class.value, _amount_decile(int(sim.case.amount), boundaries))
        strata[key].append(sim)

    for stratum in strata.values():
        rng.shuffle(stratum)
        for index, sim in enumerate(stratum):
            sim.case.arm = ExperimentArm.TREATMENT if index % 2 == 0 else ExperimentArm.CONTROL


def _draw_case(
    rng: random.Random, index: int, started_at: datetime, window: timedelta, enriched: bool
) -> SimCase:
    """One generated case, observable half and hidden half."""
    if enriched and rng.random() < _TAIL_UNMAPPED_RATE:
        reason = "unmapped_provider_code"
    else:
        reason = draw_decline_reason(rng)

    if enriched and rng.random() < _TAIL_HIGH_VALUE_RATE:
        amount = rng.choice(_HIGH_VALUE_POINTS)
    else:
        amount = int(draw_amount(rng))

    failed_at = started_at + timedelta(
        minutes=rng.randint(0, int(ARRIVAL_SPREAD.total_seconds() // 60))
    )
    truth = make_ground_truth(rng, reason, failed_at, window)
    if enriched and rng.random() < _TAIL_REPLY_RATE:
        truth = GroundTruth(
            recoverable_from=truth.recoverable_from,
            self_cure_at=truth.self_cure_at,
            downtime_ends_at=truth.downtime_ends_at,
            repairable=truth.repairable,
            sends_inbound_reply=True,
        )

    case = RecoveryCase(
        case_id=f"case_{index:06d}",
        subscription_id=f"sub_{index:06d}",
        invoice_id=f"inv_{index:06d}",
        customer_id=f"cust_{index % 9973:05d}",
        amount=paise(amount),
        method=PaymentMethod.EMANDATE,
        decline_reason=reason,
        decline_class=classify(reason, PaymentMethod.EMANDATE).decline_class,
        detected_at=failed_at,
        observation_closes_at=failed_at + window,
    )
    return SimCase(case=case, truth=truth, failed_at=failed_at)


def generate(
    *,
    name: str,
    size: int,
    seed: int,
    enriched: bool = False,
    started_at: datetime | None = None,
    window: timedelta = DEFAULT_OBSERVATION_WINDOW,
) -> Batch:
    """Generate a reproducible batch.

    The same ``seed`` and ``size`` always produce the same batch, so a run can
    be repeated exactly after a bug fix -- which the analysis plan requires,
    since both the original and corrected results must be reported.
    """
    rng = random.Random(seed)
    origin = started_at or datetime.fromisoformat("2026-09-01T00:00:00+00:00")
    cases = [_draw_case(rng, i, origin, window, enriched) for i in range(size)]
    _assign_arms(cases, rng)
    return Batch(name=name, seed=seed, cases=tuple(cases), observation_window=window)
