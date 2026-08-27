"""Case runner: drives one case through its lifecycle, then a whole batch.

Time is simulated and advances per case. Cases are independent, so each runs to
a terminal state before the next begins -- which keeps the loop simple and makes
any single case replayable in isolation from its ledger.

Three properties the runner must preserve, because the measurement depends on
them:

* **Self-cure is checked on every clock advance, in both arms.** A customer who
  would have paid anyway pays anyway. If the treatment arm counted those as its
  own recoveries and the control arm did not get the same chance, the lift would
  be an artefact of bookkeeping rather than an effect.
* **The observation window is identical in both arms**, measured from detection
  rather than from first action -- the control arm acts later, and anchoring on
  first action would quietly shorten the holdout's window.
* **Every action is gated.** The planner proposes; the policy engine decides. A
  planner that proposes something impermissible is refused like any other
  caller, which is what keeps the compliance envelope load-bearing rather than
  advisory.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from recovery.domain.case import (
    CaseState,
    ExperimentArm,
    RecoveryCase,
    StopReason,
    TailArm,
    TailSubtype,
)
from recovery.domain.events import Actor, EventKind, InMemoryLedger, Ledger
from recovery.domain.failure import DeclineClass
from recovery.domain.money import Paise, paise
from recovery.planner.base import Planner
from recovery.planner.rules import PlannedStep, PlannerFacts, StopPlan
from recovery.policy.actions import ActionKind, Channel
from recovery.policy.decision import Decision, RefusalCode
from recovery.policy.engine import PolicyEngine
from recovery.policy.gates import PolicyContext
from recovery.sim.generator import Batch, SimCase
from recovery.sim.provider import SimulatedProvider
from recovery.templates import REGISTERED

# Hold after a failure before acting. payment.failed followed by
# payment.captured on the same order is normal on UPI, so acting immediately
# means contacting people who have already paid.
COOLING_DELAY = timedelta(minutes=30)

# Guard against a planner and a gate disagreeing indefinitely.
MAX_STEPS_PER_CASE = 40

# Refusals that end a case rather than merely delay it.
_TERMINAL_REFUSALS: dict[RefusalCode, StopReason] = {
    RefusalCode.PREDEBIT_OPTED_OUT: StopReason.CUSTOMER_OPTED_OUT,
    RefusalCode.MANDATE_REVOKED: StopReason.MANDATE_REVOKED,
    RefusalCode.SUBSCRIPTION_CANCELLED: StopReason.SUBSCRIPTION_CANCELLED,
    RefusalCode.SUPPRESSED_DISPUTE: StopReason.DISPUTE_OPEN,
    RefusalCode.SUPPRESSED_DO_NOT_CONTACT: StopReason.CUSTOMER_OPTED_OUT,
    RefusalCode.NO_CONSENT: StopReason.NO_CONSENT,
    RefusalCode.INTERNAL_ATTEMPT_CAP: StopReason.ATTEMPT_BUDGET_EXHAUSTED,
    RefusalCode.NETWORK_ATTEMPT_CAP: StopReason.ATTEMPT_BUDGET_EXHAUSTED,
    RefusalCode.HARD_DECLINE_NO_RETRY: StopReason.HARD_DECLINE_UNRECOVERABLE,
    RefusalCode.UNCLASSIFIED_DECLINE_NO_RETRY: StopReason.MAX_ESCALATION_REACHED,
    RefusalCode.NEGATIVE_EXPECTED_VALUE: StopReason.NEGATIVE_EXPECTED_VALUE,
}


@dataclass(slots=True)
class CaseOutcome:
    """What happened to one case. One row of the results table."""

    case_id: str
    arm: ExperimentArm
    decline_class: DeclineClass
    decline_reason: str
    amount: Paise
    recovered: bool
    outcome_source: str
    """``attributed`` (recovered after one of our actions), ``organic`` (paid
    unprompted), or ``none``."""

    attempts: int
    messages: int
    action_cost: Paise
    stop_reason: StopReason | None
    hours_to_recovery: float | None
    tail_subtype: TailSubtype | None
    tail_arm: TailArm | None
    refusal_codes: tuple[str, ...]

    @property
    def recovered_amount(self) -> Paise:
        return self.amount if self.recovered else paise(0)


@dataclass(slots=True)
class _RunState:
    """Mutable bookkeeping for a single case run."""

    now: datetime
    closes_at: datetime
    notice_sent_at: datetime | None = None
    repair_requested: bool = False
    repaired: bool = False
    messages: int = 0
    cost: int = 0
    refusals: list[str] = field(default_factory=list)


def _tail_subtype(sim: SimCase, high_value_floor: int) -> TailSubtype | None:
    """Why this case would route to the agent tail, if it would."""
    if sim.case.decline_class is DeclineClass.UNKNOWN:
        return TailSubtype.AMBIGUOUS_DIAGNOSIS
    if int(sim.case.amount) >= high_value_floor:
        return TailSubtype.HIGH_VALUE
    if sim.truth.sends_inbound_reply:
        return TailSubtype.INBOUND_FREETEXT
    return None


def _downtime_active(sim: SimCase, now: datetime) -> bool:
    """Whether the platform is currently reporting an outage for this case.

    Razorpay publishes downtime, so this is an observable signal rather than a
    peek at ground truth -- the case's outage window happens to be where the
    simulator stores it.
    """
    ends = sim.truth.downtime_ends_at
    return ends is not None and now < ends


def _context(sim: SimCase, state: _RunState) -> PolicyContext:
    """Assemble what the gates need."""
    return PolicyContext(
        case=sim.case,
        now=state.now,
        consented_channels=frozenset({Channel.SMS}),
        consented_purposes=frozenset({"payment_recovery"}),
        templates=REGISTERED,
        predebit_notice_sent_at=state.notice_sent_at,
        downtime_active=_downtime_active(sim, state.now),
        downtime_expected_end=sim.truth.downtime_ends_at,
        mandate_active=True,
        attempts_last_30d=sim.case.attempt_count,
        card_network="visa",
    )


def _self_cured(sim: SimCase, now: datetime) -> bool:
    """Did the customer pay unprompted by now?"""
    cure_at = sim.truth.self_cure_at
    return cure_at is not None and now >= cure_at


def run_case(
    sim: SimCase,
    planner: Planner,
    provider: SimulatedProvider,
    engine: PolicyEngine,
    ledger: Ledger,
    high_value_floor: int,
) -> CaseOutcome:
    """Drive one case to a terminal state."""
    case = sim.case
    closes_at = case.observation_closes_at or (sim.failed_at + timedelta(days=21))
    state = _RunState(now=sim.failed_at, closes_at=closes_at)
    if case.tail_subtype is None:
        case.tail_subtype = _tail_subtype(sim, high_value_floor)

    ledger.record(
        case.case_id,
        EventKind.CASE_DETECTED,
        Actor.WEBHOOK,
        f"charge failed: {case.decline_reason} ({case.decline_class.value})",
        {"amount_paise": int(case.amount), "arm": case.arm.value},
    )

    case.transition_to(CaseState.COOLING)
    state.now += COOLING_DELAY

    for _ in range(MAX_STEPS_PER_CASE):
        if state.now >= closes_at:
            return _stop(sim, state, StopReason.OBSERVATION_WINDOW_CLOSED, ledger)

        case.transition_to(CaseState.DIAGNOSED)
        if _self_cured(sim, state.now):
            return _finish(sim, state, "organic", ledger)

        plan = planner.next_step(case, _facts(sim, state, ledger))
        if isinstance(plan, StopPlan):
            return _stop(sim, state, plan.reason, ledger)

        if plan.at > state.now:
            state.now = plan.at
            if _self_cured(sim, state.now):
                return _finish(sim, state, "organic", ledger)

        outcome = _execute_step(sim, state, plan, provider, engine, ledger, closes_at)
        if outcome is not None:
            return outcome

    return _stop(sim, state, StopReason.MAX_ESCALATION_REACHED, ledger)


def _facts(sim: SimCase, state: _RunState, ledger: Ledger) -> PlannerFacts:
    return PlannerFacts(
        now=state.now,
        window_closes_at=state.closes_at,
        notice_sent_at=state.notice_sent_at,
        downtime_active=_downtime_active(sim, state.now),
        instrument_repair_requested=state.repair_requested,
        instrument_repaired=state.repaired,
        policy_context=_context(sim, state),
        audit=ledger,
    )


def _execute_step(
    sim: SimCase,
    state: _RunState,
    plan: PlannedStep,
    provider: SimulatedProvider,
    engine: PolicyEngine,
    ledger: Ledger,
    closes_at: datetime,
) -> CaseOutcome | None:
    """Gate and perform one planned step. Returns an outcome if the case ends."""
    case = sim.case
    case.transition_to(CaseState.PLANNED)

    decision = engine.evaluate_and_record(plan.action, _context(sim, state), ledger)
    if not decision.permitted:
        return _handle_refusal(sim, state, decision, closes_at, ledger)

    case.transition_to(CaseState.SCHEDULED)
    case.transition_to(CaseState.EXECUTING)
    _perform(sim, state, plan, provider, ledger)

    if plan.action.kind is ActionKind.RETRY_DEBIT:
        key = f"{case.case_id}:debit:{case.attempt_count}"
        result = provider.charge(
            case_id=case.case_id, amount=case.amount, idempotency_key=key, at=state.now
        )
        case.attempt_count += 1
        ledger.record(
            case.case_id,
            EventKind.ACTION_EXECUTED,
            Actor.SYSTEM,
            f"debit {'succeeded' if result.succeeded else 'failed'}",
            {"payment_id": result.payment_id, "reason": result.reason},
        )
        case.transition_to(CaseState.AWAITING_OUTCOME)
        if result.succeeded:
            return _finish(sim, state, "attributed", ledger)
        # Our conservative reading of the e-mandate rule: a fresh notice is
        # required before each attempt, so the previous one does not carry over.
        state.notice_sent_at = None
        return None

    case.transition_to(CaseState.AWAITING_OUTCOME)
    return None


def _perform(
    sim: SimCase,
    state: _RunState,
    plan: PlannedStep,
    provider: SimulatedProvider,
    ledger: Ledger,
) -> None:
    """Side effects for non-debit actions."""
    case = sim.case
    kind = plan.action.kind
    if kind is ActionKind.WAIT:
        return

    if kind.contacts_customer:
        key = f"{case.case_id}:{kind.value}:{state.messages}"
        receipt = provider.send_message(
            case_id=case.case_id,
            channel=plan.action.channel,
            template_id=plan.action.template_id,
            idempotency_key=key,
            at=state.now,
        )
        state.messages += 1
        state.cost += int(receipt.cost)
        ledger.record(
            case.case_id,
            EventKind.NOTICE_SENT
            if kind is ActionKind.SEND_PREDEBIT_NOTICE
            else EventKind.ACTION_EXECUTED,
            Actor.SYSTEM,
            f"sent {plan.action.template_id} via {plan.action.channel.value}",
            {"message_id": receipt.message_id, "cost_paise": int(receipt.cost)},
        )

    if kind is ActionKind.SEND_PREDEBIT_NOTICE:
        state.notice_sent_at = state.now
    elif kind is ActionKind.REQUEST_INSTRUMENT_UPDATE:
        state.repair_requested = True
        state.repaired = provider.accept_instrument_repair(case.case_id)


def _handle_refusal(
    sim: SimCase,
    state: _RunState,
    decision: Decision,
    closes_at: datetime,
    ledger: Ledger,
) -> CaseOutcome | None:
    """Respond to a policy refusal: wait it out, or stop."""
    state.refusals.extend(c.value for c in decision.codes)

    for code in decision.codes:
        if code in _TERMINAL_REFUSALS:
            return _stop(sim, state, _TERMINAL_REFUSALS[code], ledger)

    resume_at = decision.earliest_permissible_at
    if resume_at is None or resume_at >= closes_at:
        return _stop(sim, state, StopReason.OBSERVATION_WINDOW_CLOSED, ledger)

    # Park in a waiting state until the blocking clock clears, then re-plan.
    waiting = (
        CaseState.NOTICE_PENDING
        if RefusalCode.PREDEBIT_NOTICE_NOT_MATURE in decision.codes
        else CaseState.SCHEDULED
    )
    sim.case.transition_to(waiting)
    state.now = resume_at
    return None


def _finish(sim: SimCase, state: _RunState, source: str, ledger: Ledger) -> CaseOutcome:
    """Terminal: recovered."""
    case = sim.case
    case.transition_to(CaseState.RECOVERED)
    hours = (state.now - sim.failed_at).total_seconds() / 3600
    ledger.record(
        case.case_id,
        EventKind.OUTCOME_RECORDED,
        Actor.SYSTEM,
        f"recovered ({source})",
        {"hours_to_recovery": round(hours, 2)},
    )
    return _outcome(sim, state, recovered=True, source=source, stop_reason=None, hours=hours)


def _stop(sim: SimCase, state: _RunState, reason: StopReason, ledger: Ledger) -> CaseOutcome:
    """Terminal: we stop acting, with the rule that fired.

    The lifecycle records what the *system* did; the outcome records what
    happened to the *money*. Those are different facts, and a case can
    legitimately stop while the invoice is still paid afterwards -- our ceasing
    to act does not stop a customer paying.

    So a self-cure anywhere inside the observation window is credited here, on
    a case that is nonetheless STOPPED with its real reason. This matters for
    the measurement rather than for tidiness: the arms stop at different times,
    because the control arm exhausts its attempts sooner. Crediting organic
    payments only while a case was still active would hand the treatment arm
    more opportunity to observe them than the holdout, and inflate the measured
    lift for a purely bookkeeping reason.
    """
    case = sim.case
    if not case.state.is_terminal:
        case.transition_to(CaseState.STOPPED, stop_reason=reason)
    ledger.record(
        case.case_id,
        EventKind.CASE_STOPPED,
        Actor.SYSTEM,
        f"stopped: {reason.value}",
        {"attempts": case.attempt_count, "messages": state.messages},
    )

    cure_at = sim.truth.self_cure_at
    if cure_at is not None and cure_at <= state.closes_at:
        hours = (cure_at - sim.failed_at).total_seconds() / 3600
        ledger.record(
            case.case_id,
            EventKind.OUTCOME_RECORDED,
            Actor.SYSTEM,
            "recovered (organic) after the case stopped",
            {"hours_to_recovery": round(hours, 2)},
        )
        return _outcome(
            sim, state, recovered=True, source="organic", stop_reason=reason, hours=hours
        )

    return _outcome(sim, state, recovered=False, source="none", stop_reason=reason, hours=None)


def _outcome(
    sim: SimCase,
    state: _RunState,
    *,
    recovered: bool,
    source: str,
    stop_reason: StopReason | None,
    hours: float | None,
) -> CaseOutcome:
    case = sim.case
    return CaseOutcome(
        case_id=case.case_id,
        arm=case.arm,
        decline_class=case.decline_class,
        decline_reason=case.decline_reason or "unspecified",
        amount=case.amount,
        recovered=recovered,
        outcome_source=source,
        attempts=case.attempt_count,
        messages=state.messages,
        action_cost=paise(state.cost),
        stop_reason=stop_reason,
        hours_to_recovery=round(hours, 2) if hours is not None else None,
        tail_subtype=case.tail_subtype,
        tail_arm=case.tail_arm,
        refusal_codes=tuple(state.refusals),
    )


class ArmRouter(Protocol):
    """Selects the planner for a case. See :mod:`recovery.agent.router`."""

    def planner_for(self, case: RecoveryCase) -> Planner: ...


def run_batch(
    batch: Batch,
    router: ArmRouter,
    *,
    workers: int = 1,
    ledger: Ledger | None = None,
) -> tuple[list[CaseOutcome], SimulatedProvider, Ledger]:
    """Run every case in a batch through the planner its router selects.

    The router owns arm assignment, so the runner stays ignorant of which
    experiment a case belongs to -- it just drives whatever planner it is
    handed. See :mod:`recovery.agent.router`.

    ``ledger`` exists so the live console can watch a run through a
    :class:`~recovery.live.broadcast.BroadcastLedger` without the runner
    knowing anyone is watching. Omitting it reproduces the previous behaviour
    exactly -- a fresh in-memory ledger per batch -- which is what keeps a
    measured run independent of whether a browser tab was open.
    """
    provider = SimulatedProvider(truths={c.case.case_id: c.truth for c in batch.cases})
    ledger = ledger if ledger is not None else Ledger(InMemoryLedger())
    engine = PolicyEngine()

    amounts = sorted(int(c.case.amount) for c in batch.cases)
    high_value_floor = amounts[int(len(amounts) * 0.9)] if amounts else 0

    # Routing happens up front and single-threaded. Arm assignment is what the
    # whole experiment rests on, so it is kept off the concurrent path entirely.
    pairs: list[tuple[SimCase, Planner]] = []
    for sim in batch.cases:
        sim.case.tail_subtype = _tail_subtype(sim, high_value_floor)
        pairs.append((sim, router.planner_for(sim.case)))

    def run_one(pair: tuple[SimCase, Planner]) -> CaseOutcome:
        sim, planner = pair
        return run_case(sim, planner, provider, engine, ledger, high_value_floor)

    if workers <= 1:
        return [run_one(p) for p in pairs], provider, ledger

    # Cases are independent: each owns its RecoveryCase, and its outcome is a
    # function of its own ground truth and the times it acts. The shared pieces
    # -- provider ledgers, audit ledger, model telemetry, token budget -- are
    # individually locked. pool.map preserves submission order, so a concurrent
    # run yields the same rows in the same order as a serial one.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(run_one, pairs))
    return outcomes, provider, ledger
