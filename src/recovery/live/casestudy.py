"""One case, all the way through, in the loop's own vocabulary.

Track 3 names five stages -- Detect, Diagnose, Intervene, Recover, Measure --
and the jury asks for one full case walked from the failure that started it to
the money that came back. This is that walk, built from a real run rather than
narrated over a diagram.

The five stages here are the rubric's, not the architecture's. They do not map
one to one: UNDERSTAND covers Detect and Diagnose, and GOVERN has no rubric
equivalent because "do not do that" is not a stage of a recovery loop -- it is
the reason the loop is safe to run. So GOVERN appears inside Intervene, where
the refusals actually happen, and the count of gates that ran is carried
alongside.

Selection is by outcome, never by hand: the case must be **attributed**, not
organic. A customer who paid unprompted proves nothing about the agent, and
picking one would be claiming someone else's work.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any

from recovery.batch.runner import run_case
from recovery.detect import Detector, delivery_for, sign_delivery
from recovery.domain.events import AuditEvent, EventKind, InMemoryLedger, Ledger
from recovery.domain.failure import DeclineClass
from recovery.domain.money import format_inr, paise
from recovery.planner.rules import DeclineConditionalPlanner
from recovery.policy.engine import PolicyEngine
from recovery.providers.webhooks import WebhookReceiver
from recovery.sim.generator import generate
from recovery.sim.provider import SimulatedProvider

STUDY_SEED = 20260824
STUDY_SIZE = 120
STUDY_SECRET = "recoup_casestudy_webhook_secret"

_STAGES: tuple[tuple[str, str, tuple[EventKind, ...]], ...] = (
    (
        "DETECT",
        "A signed provider delivery, verified and deduped before a case exists.",
        (EventKind.DELIVERY_RECEIVED, EventKind.CASE_DETECTED),
    ),
    (
        "INTERVENE",
        "What was proposed, which gates ran, and what they permitted or refused.",
        (
            EventKind.ACTIONS_PROPOSED,
            EventKind.POLICY_EVALUATED,
            EventKind.ACTION_REFUSED,
            EventKind.STATE_CHANGED,
        ),
    ),
    (
        "RECOVER",
        "The actions that actually reached the customer and the instrument.",
        (
            EventKind.NOTICE_SENT,
            EventKind.ACTION_EXECUTED,
            EventKind.ACTION_SCHEDULED,
            EventKind.ACTION_DEDUPED,
        ),
    ),
    (
        "MEASURE",
        "The outcome, and whether it is attributable to what we did.",
        (EventKind.OUTCOME_RECORDED, EventKind.CASE_STOPPED),
    ),
)

_PERMITS: dict[DeclineClass, str] = {
    DeclineClass.SOFT: "a retry can succeed; the only question is when",
    DeclineClass.HARD: "no debit can ever succeed; instrument repair is the only route",
    DeclineClass.DOWNTIME: "wait for the resolve signal, not a timer",
    DeclineClass.UNKNOWN: "no debit; an unnamed failure does not get an attempt",
}


@dataclass(frozen=True, slots=True)
class Step:
    day: int
    at: str
    actor: str
    kind: str
    summary: str
    gates_run: int
    gates_refused: int


@dataclass(frozen=True, slots=True)
class Stage:
    name: str
    blurb: str
    steps: tuple[Step, ...]


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """The stage between Detect and Intervene, which is a conclusion not an event."""

    reason: str
    decline_class: str
    permits: str
    retry_allowed: bool


@dataclass(frozen=True, slots=True)
class CaseStudy:
    case_id: str
    amount: str
    delivery_id: str
    diagnosis: Diagnosis
    stages: tuple[Stage, ...]
    recovered: bool
    attributed: bool
    recovered_amount: str
    attempts: int
    messages: int
    gates_run: int
    gates_refused: int
    days: int

    def payload(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "amount": self.amount,
            "delivery_id": self.delivery_id,
            "diagnosis": {
                "reason": self.diagnosis.reason,
                "decline_class": self.diagnosis.decline_class,
                "permits": self.diagnosis.permits,
                "retry_allowed": self.diagnosis.retry_allowed,
            },
            "stages": [
                {
                    "name": s.name,
                    "blurb": s.blurb,
                    "steps": [
                        {
                            "day": t.day,
                            "at": t.at,
                            "actor": t.actor,
                            "kind": t.kind,
                            "summary": t.summary,
                            "gates_run": t.gates_run,
                            "gates_refused": t.gates_refused,
                        }
                        for t in s.steps
                    ],
                }
                for s in self.stages
            ],
            "recovered": self.recovered,
            "attributed": self.attributed,
            "recovered_amount": self.recovered_amount,
            "attempts": self.attempts,
            "messages": self.messages,
            "gates_run": self.gates_run,
            "gates_refused": self.gates_refused,
            "days": self.days,
        }


@lru_cache(maxsize=4)
def build_case_study(*, seed: int = STUDY_SEED, size: int = STUDY_SIZE) -> CaseStudy | None:
    """Run cases until one recovers by attribution, then narrate it.

    Returns ``None`` rather than a substitute if no case in the batch recovers
    attributably -- a worked example of a case that did not work is not the
    thing that was asked for, and inventing one would be worse.
    """
    batch = generate(name="case-study", size=size, seed=seed)
    engine = PolicyEngine()
    candidates: list[tuple[bool, int, str, CaseStudy]] = []

    for sim in batch.cases:
        replica = copy.deepcopy(sim)
        replica.case.tail_subtype = None
        ledger = Ledger(InMemoryLedger())
        detector = Detector(receiver=WebhookReceiver(secret=STUDY_SECRET), ledger=ledger)

        body = delivery_for(
            case_id=replica.case.case_id,
            amount_paise=int(replica.case.amount),
            decline_reason=str(replica.case.decline_reason),
            method=replica.case.method.value,
        )
        detection = detector.deliver(
            body, sign_delivery(body, STUDY_SECRET), delivery_id=f"evt_{replica.case.case_id}"
        )

        provider = SimulatedProvider(truths={replica.case.case_id: replica.truth})
        outcome = run_case(replica, DeclineConditionalPlanner(), provider, engine, ledger, 0)

        events = ledger.history(replica.case.case_id)
        if not _is_worked_example(outcome, events):
            continue

        study = _narrate(replica, detection.delivery_id or "", events, outcome)
        # Prefer a case where the policy engine actually spoke. A walk with no
        # refusal shows the loop but not the reason it is safe to run, and
        # GOVERN is the half of this system worth explaining.
        candidates.append(
            (study.gates_refused > 0, int(replica.case.amount), replica.case.case_id, study)
        )

    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)
    return candidates[0][3]


def _is_worked_example(outcome: Any, events: list[AuditEvent]) -> bool:
    """Recovered, and recovered *because of* what we did.

    Organic recovery -- the customer paying unprompted -- is excluded on
    purpose. It is the baseline the measured lift is taken against, and
    presenting one as a worked example would be claiming someone else's work.
    """
    if not outcome.recovered:
        return False
    return any(e.kind is EventKind.OUTCOME_RECORDED and "attributed" in e.summary for e in events)


def _narrate(sim: Any, delivery_id: str, events: list[AuditEvent], outcome: Any) -> CaseStudy:
    failed_at = sim.failed_at
    steps = {name: [] for name, _, _ in _STAGES}  # type: dict[str, list[Step]]
    gates_run = gates_refused = 0

    for event in events:
        gates = event.payload.get("gates") or []
        refused = sum(1 for g in gates if not g.get("passed"))
        gates_run += len(gates)
        gates_refused += refused

        at = event.payload.get("at")
        moment = datetime.fromisoformat(str(at)) if at else failed_at
        step = Step(
            day=max((moment - failed_at).days, 0),
            at=moment.isoformat(),
            actor=event.actor.value,
            kind=event.kind.value,
            summary=event.summary,
            gates_run=len(gates),
            gates_refused=refused,
        )
        for name, _blurb, kinds in _STAGES:
            if event.kind in kinds:
                steps[name].append(step)
                break

    decline = sim.case.decline_class
    last = steps["MEASURE"][-1] if steps["MEASURE"] else None
    return CaseStudy(
        case_id=sim.case.case_id,
        amount=format_inr(sim.case.amount),
        delivery_id=delivery_id,
        diagnosis=Diagnosis(
            reason=str(sim.case.decline_reason),
            decline_class=decline.value,
            permits=_PERMITS[decline],
            retry_allowed=decline.allows_debit_retry,
        ),
        stages=tuple(
            Stage(name=name, blurb=blurb, steps=tuple(steps[name])) for name, blurb, _ in _STAGES
        ),
        recovered=bool(outcome.recovered),
        attributed=True,
        recovered_amount=format_inr(paise(int(outcome.recovered_amount))),
        attempts=sim.case.attempt_count,
        messages=sum(1 for e in events if e.summary.startswith("sent ")),
        gates_run=gates_run,
        gates_refused=gates_refused,
        days=last.day if last else 0,
    )
