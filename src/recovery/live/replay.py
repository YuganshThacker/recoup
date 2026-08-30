"""The counterfactual race: one case, two planners, one world.

R1 says the system beats the platform default by +23.5 points. That number comes
from randomising *between* cases and relies on the arms being balanced. This
module makes the same comparison a different way: take one case and run it twice
against **identical ground truth**, varying only the planner. There is nothing to
balance, because nothing differs.

    Nothing about the customer changed. Nothing about the world changed.
    We changed only the planner.

**The treatment arm is the rules planner, never the agent.** R1 is *system vs
platform default*, and the system's treatment arm is
:class:`~recovery.planner.rules.DeclineConditionalPlanner`. Routing it through
the model would quietly turn this into an R2-flavoured comparison and the race
would be illustrating a claim the batch does not make. This is the single
easiest way to get the feature wrong.

**The hero case is discovered, never hard-coded.** Candidates are ranked by
whether the system wins, then by whether the mechanism is the one the batch
result attributes the lift to, then by how legible the timeline is. Whatever
that produces is what the demo shows.

**What this is evidence about.** The comparison is rigorous; the world it
compares in is simulated, and ``sim/world.py`` encodes the hypothesis that retry
timing matters. The race cannot confirm that hypothesis, and the view says so.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any

from recovery.batch.runner import run_case
from recovery.domain.events import InMemoryLedger, Ledger
from recovery.planner.base import Planner
from recovery.planner.rules import DeclineConditionalPlanner, PlatformDefaultPlanner
from recovery.policy.engine import PolicyEngine
from recovery.sim.generator import Batch, SimCase, generate
from recovery.sim.provider import SimulatedProvider

RACE_SEED = 20260824
"""The seed R1 was measured at, so the race and the headline describe the same
population."""

RACE_SIZE = 120
"""Enough to quote an honest divergence rate without making the view slow."""

SIMULATION_NOTE = (
    "Simulated. Both arms run against the same world model, so the comparison "
    "is exact \u2014 but what a real customer would have done is not something "
    "this can establish."
)

_HERO_MECHANISM = "insufficient_funds"
"""The decline reason RESULTS.md attributes the lift to. Preferred for the hero
case so the race illustrates the mechanism the batch actually measured."""


@dataclass(frozen=True, slots=True)
class ArmRun:
    """One case under one planner."""

    label: str
    planner: str
    case_id: str
    failed_at: datetime
    recovered: bool
    recovered_paise: int
    attempts: int
    messages: int
    events: tuple[dict[str, Any], ...]

    def payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "planner": self.planner,
            "recovered": self.recovered,
            "recovered_paise": self.recovered_paise,
            "attempts": self.attempts,
            "messages": self.messages,
            "events": list(self.events),
        }


@dataclass(frozen=True, slots=True)
class Race:
    """Both arms of one case, and the world they shared."""

    case_id: str
    amount_paise: int
    decline_reason: str
    decline_class: str
    recoverable_from_day: int | None
    """When a debit would first have succeeded, as a day offset. Ground truth --
    never visible to the system, shown here because it is the reason one arm
    wins and the other does not."""

    self_cure_day: int | None
    default: ArmRun
    recoup: ArmRun
    diverged: int
    total: int

    @property
    def rate(self) -> float:
        return self.diverged / self.total if self.total else 0.0

    def payload(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "amount_paise": self.amount_paise,
            "decline_reason": self.decline_reason,
            "decline_class": self.decline_class,
            "recoverable_from_day": self.recoverable_from_day,
            "self_cure_day": self.self_cure_day,
            "default": self.default.payload(),
            "recoup": self.recoup.payload(),
            "diverged": self.diverged,
            "total": self.total,
            "rate": round(self.rate, 4),
            "note": SIMULATION_NOTE,
        }


@dataclass(frozen=True, slots=True)
class DivergenceReport:
    """How often the two policies actually reach different outcomes."""

    total: int
    diverged: int
    case_ids: tuple[str, ...]
    hero: str | None

    @property
    def rate(self) -> float:
        return self.diverged / self.total if self.total else 0.0


# --- running one arm --------------------------------------------------------


def _arm(sim: SimCase, planner: Planner, *, label: str) -> ArmRun:
    """Run one case under one planner, in a world of its own.

    Each arm gets its own provider and ledger. Sharing a provider would let the
    first arm's idempotency keys deduplicate the second arm's debits, which
    would hand one side a different world and silently void the comparison.
    """
    replica = copy.deepcopy(sim)
    replica.case.tail_subtype = None
    provider = SimulatedProvider(truths={replica.case.case_id: replica.truth})
    ledger = Ledger(InMemoryLedger())

    outcome = run_case(replica, planner, provider, PolicyEngine(), ledger, 0)

    return ArmRun(
        label=label,
        planner=type(planner).__name__,
        case_id=replica.case.case_id,
        failed_at=sim.failed_at,
        recovered=outcome.recovered,
        recovered_paise=int(outcome.recovered_amount) if outcome.recovered else 0,
        attempts=replica.case.attempt_count,
        messages=sum(1 for e in ledger.history(replica.case.case_id) if "sent " in e.summary),
        events=_timeline(ledger, replica.case.case_id, sim.failed_at),
    )


def _timeline(ledger: Ledger, case_id: str, failed_at: datetime) -> tuple[dict[str, Any], ...]:
    """Events with the day offset the view lays them out on."""
    rows = []
    for event in ledger.history(case_id):
        at = event.payload.get("at")
        moment = datetime.fromisoformat(str(at)) if at else failed_at
        rows.append(
            {
                "seq": event.seq,
                "at": moment.isoformat(),
                "day": max((moment - failed_at).days, 0),
                "actor": event.actor.value,
                "kind": event.kind.value,
                "summary": event.summary,
            }
        )
    return tuple(rows)


def _day(moment: datetime | None, failed_at: datetime) -> int | None:
    return None if moment is None else max((moment - failed_at).days, 0)


# --- the race ---------------------------------------------------------------


@lru_cache(maxsize=8)
def _batch(seed: int, size: int) -> Batch:
    """Deterministic by seed, so a replay is the same replay every time."""
    return generate(name="race", size=size, seed=seed)


def replay_case(case_id: str | None, *, seed: int = RACE_SEED, size: int = RACE_SIZE) -> Race:
    """Run one case under both planners and return the comparison."""
    batch = _batch(seed, size)
    sim = next((c for c in batch.cases if c.case.case_id == case_id), None)
    if sim is None:
        raise KeyError(f"'{case_id}' is not in this batch")

    report = find_divergent_cases(seed=seed, size=size)
    return Race(
        case_id=sim.case.case_id,
        amount_paise=int(sim.case.amount),
        decline_reason=str(sim.case.decline_reason),
        decline_class=sim.case.decline_class.value,
        recoverable_from_day=_day(sim.truth.recoverable_from, sim.failed_at),
        self_cure_day=_day(sim.truth.self_cure_at, sim.failed_at),
        default=_arm(sim, PlatformDefaultPlanner(), label="Platform default"),
        recoup=_arm(sim, DeclineConditionalPlanner(), label="Recoup"),
        diverged=report.diverged,
        total=report.total,
    )


@lru_cache(maxsize=8)
def find_divergent_cases(*, seed: int = RACE_SEED, size: int = RACE_SIZE) -> DivergenceReport:
    """Which cases the two policies disagree about, and which one to show.

    The rate is quoted alongside the race because without it a single winning
    case is indistinguishable from a cherry-picked one.
    """
    batch = _batch(seed, size)
    diverging, ranked = [], []

    for sim in batch.cases:
        default = _arm(sim, PlatformDefaultPlanner(), label="Platform default")
        recoup = _arm(sim, DeclineConditionalPlanner(), label="Recoup")
        if default.recovered == recoup.recovered:
            continue

        diverging.append(sim.case.case_id)
        if recoup.recovered and not default.recovered:
            ranked.append(
                (
                    sim.case.decline_reason == _HERO_MECHANISM,
                    -len(recoup.events),
                    int(sim.case.amount),
                    sim.case.case_id,
                )
            )

    ranked.sort(reverse=True)
    return DivergenceReport(
        total=len(batch.cases),
        diverged=len(diverging),
        case_ids=tuple(diverging),
        hero=ranked[0][3] if ranked else None,
    )
