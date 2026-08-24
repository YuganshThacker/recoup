"""Assemble what the audit report needs from a finished batch.

Kept separate from rendering so the shape of the evidence is decided here and
the HTML is only presentation. Everything in the payload is derived from the
run -- there are no illustrative figures, and a number that was not measured
does not appear.

The ledger is the source for per-case timelines. Embedding every event for
every case would produce a file too large to open, so timelines are included
for a bounded sample chosen to over-represent the cases worth reading: those
where a gate refused something, where the model was involved, or where the
outcome is instructive. Which cases were included, and on what basis, is
recorded in the payload rather than left implicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from recovery.batch.metrics import (
    Lift,
    ablation_by_subtype,
    ablation_overall,
    action_cost_paise,
    exception_list,
    gross_recovered_paise,
    incremental_paise,
    lift_by_decline_class,
    outcome_source_counts,
    recovery_lift,
    refusal_counts,
    stop_reason_counts,
)
from recovery.batch.runner import CaseOutcome
from recovery.domain.events import EventKind, Ledger
from recovery.domain.money import format_inr

DEFAULT_TIMELINE_SAMPLE = 240


def _lift_payload(lift: Lift) -> dict[str, Any]:
    low, high = lift.interval
    return {
        "treatment_rate": round(lift.treatment.rate, 4),
        "control_rate": round(lift.control.rate, 4),
        "treatment_n": lift.treatment.total,
        "control_n": lift.control.total,
        "value": round(lift.value, 4),
        "ci_low": round(low, 4),
        "ci_high": round(high, 4),
        "straddles_zero": lift.straddles_zero,
    }


def _interest_score(outcome: CaseOutcome) -> tuple[int, str]:
    """Rank cases by how much a reader learns from the timeline.

    Refusals first: a gate blocking a money action is the single most
    informative thing in the log, and it is what the audit trail exists to
    show.
    """
    score = 0
    if outcome.refusal_codes:
        score += 4
    if outcome.tail_arm is not None:
        score += 3
    if outcome.recovered and outcome.outcome_source == "attributed":
        score += 2
    if outcome.stop_reason is not None:
        score += 1
    # Negated so a stable sort puts high scores first, id keeps it deterministic.
    return (-score, outcome.case_id)


@dataclass(frozen=True, slots=True)
class ReportInputs:
    """One batch's worth of evidence."""

    label: str
    outcomes: list[CaseOutcome]
    ledger: Ledger
    seed: int
    agent_mode: str
    model: str | None
    void_reason: str | None = None


def _case_row(outcome: CaseOutcome) -> dict[str, Any]:
    return {
        "id": outcome.case_id,
        "arm": outcome.arm.value,
        "tail_arm": outcome.tail_arm.value if outcome.tail_arm else None,
        "subtype": outcome.tail_subtype.value if outcome.tail_subtype else None,
        "klass": outcome.decline_class.value,
        "reason": outcome.decline_reason,
        "amount_paise": int(outcome.amount),
        "amount": format_inr(outcome.amount),
        "recovered": outcome.recovered,
        "source": outcome.outcome_source,
        "attempts": outcome.attempts,
        "messages": outcome.messages,
        "cost": format_inr(outcome.action_cost),
        "stop_reason": outcome.stop_reason.value if outcome.stop_reason else None,
        "hours": outcome.hours_to_recovery,
        "refusals": list(outcome.refusal_codes),
    }


def _gate(gate: Any) -> dict[str, Any]:
    """Trim a gate result to what the report actually renders."""
    if gate.get("passed"):
        return {"gate": gate["gate"], "passed": True}
    return {
        "gate": gate["gate"],
        "passed": False,
        "code": gate.get("code"),
        "explanation": gate.get("explanation"),
        "remediation": gate.get("remediation"),
    }


def _timeline(ledger: Ledger, case_id: str) -> list[dict[str, Any]]:
    """One case's full history, in the order it happened."""
    events = []
    for event in ledger.history(case_id):
        entry: dict[str, Any] = {
            "seq": event.seq,
            "at": event.occurred_at.isoformat(timespec="seconds"),
            "kind": event.kind.value,
            "actor": event.actor.value,
            "summary": event.summary,
        }
        # The model's own reasoning, where it produced any. Without this the
        # drill-down shows what was decided but not what the model thought, and
        # the refusal below it has nothing to argue with.
        if event.kind is EventKind.ACTIONS_PROPOSED:
            entry["proposal"] = {
                k: event.payload.get(k)
                for k in ("diagnosis", "rationale", "confidence", "delay_hours", "tokens")
                if event.payload.get(k) is not None
            }

        gates = event.payload.get("gates")
        if isinstance(gates, list):
            # Passing gates keep their name only. That a gate ran and allowed the
            # action is the audit-relevant fact; its prose is not, and carrying it
            # for eight gates on every event triples the file for no added
            # evidence. Refusals keep everything -- they are the interesting rows.
            entry["gates"] = [_gate(g) for g in gates]
        events.append(entry)
    return events


def build(
    inputs: ReportInputs, *, timeline_sample: int = DEFAULT_TIMELINE_SAMPLE
) -> dict[str, Any]:
    """Assemble the full evidence payload for one batch."""
    outcomes = inputs.outcomes
    lift = recovery_lift(outcomes)

    ranked = sorted(outcomes, key=_interest_score)
    sampled = ranked[:timeline_sample]
    timelines = {c.case_id: _timeline(inputs.ledger, c.case_id) for c in sampled}

    return {
        "label": inputs.label,
        "seed": inputs.seed,
        "agent_mode": inputs.agent_mode,
        "model": inputs.model,
        "void_reason": inputs.void_reason,
        "n": len(outcomes),
        "primary": {
            "lift": _lift_payload(lift),
            "incremental": format_inr(incremental_paise(outcomes)),
            "incremental_paise": int(incremental_paise(outcomes)),
            "gross": format_inr(gross_recovered_paise(outcomes)),
            "outreach_cost": format_inr(action_cost_paise(outcomes)),
        },
        "by_class": {k: _lift_payload(v) for k, v in lift_by_decline_class(outcomes).items()},
        "ablation": {
            "overall": _lift_payload(ablation_overall(outcomes)),
            "by_subtype": {k: _lift_payload(v) for k, v in ablation_by_subtype(outcomes).items()},
        },
        "refusals": refusal_counts(outcomes),
        "stops": stop_reason_counts(outcomes),
        "sources": outcome_source_counts(outcomes),
        "exceptions": exception_list(outcomes),
        "cases": [_case_row(c) for c in outcomes],
        "timelines": timelines,
        "timeline_note": (
            f"Full event timelines are embedded for {len(sampled)} of {len(outcomes)} cases, "
            "ranked to over-represent cases where a gate refused an action, where the model "
            "was in the loop, or which reached a terminal state. Summary rows are present for "
            "every case."
        ),
    }
