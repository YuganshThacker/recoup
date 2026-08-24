"""Batch harness tests.

The properties here are the ones the headline number depends on. If any of them
break, the measurement is wrong in a way that would not be visible by reading
the report -- which is precisely why they are asserted rather than assumed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from recovery.agent.router import DeterministicArms
from recovery.batch.metrics import (
    Lift,
    Proportion,
    gross_recovered_paise,
    incremental_paise,
    natural_subtype_weights,
    recovery_lift,
    refusal_counts,
    stop_reason_counts,
)
from recovery.batch.runner import run_batch
from recovery.domain.case import ExperimentArm
from recovery.domain.failure import DeclineClass
from recovery.domain.money import paise
from recovery.planner.rules import DeclineConditionalPlanner, PlatformDefaultPlanner
from recovery.policy.actions import Channel
from recovery.sim.generator import generate
from recovery.sim.provider import SimulatedProvider
from recovery.sim.world import GroundTruth

NOW = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)


def _run(size: int = 600, seed: int = 7, enriched: bool = False):
    batch = generate(name="t", size=size, seed=seed, enriched=enriched)
    outcomes, provider, ledger = run_batch(
        batch,
        DeterministicArms(treatment=DeclineConditionalPlanner(), control=PlatformDefaultPlanner()),
    )
    return batch, outcomes, provider, ledger


# --- idempotency: the named "one failure handled gracefully" deliverable ---


def _truth(recoverable_from: datetime | None) -> GroundTruth:
    return GroundTruth(
        recoverable_from=recoverable_from,
        self_cure_at=None,
        downtime_ends_at=None,
        repairable=False,
        sends_inbound_reply=False,
    )


def test_double_fired_debit_does_not_debit_twice() -> None:
    # The failure this system most needs to survive: a scheduler or a redelivered
    # webhook firing the same recovery action twice. The second call must be
    # absorbed, not honoured.
    provider = SimulatedProvider(truths={"case_1": _truth(NOW)})
    key = "case_1:debit:0"

    first = provider.charge(case_id="case_1", amount=paise(49900), idempotency_key=key, at=NOW)
    second = provider.charge(case_id="case_1", amount=paise(49900), idempotency_key=key, at=NOW)

    assert first.succeeded and not first.deduplicated
    assert second.succeeded and second.deduplicated
    assert second.payment_id == first.payment_id
    # The money moved once.
    assert provider.charge_calls == 1


def test_double_fired_message_sends_once() -> None:
    provider = SimulatedProvider(truths={"case_1": _truth(None)})
    key = "case_1:send_reminder:0"
    kwargs = {
        "case_id": "case_1",
        "channel": Channel.SMS,
        "template_id": "RP_DUNNING_01",
        "idempotency_key": key,
        "at": NOW,
    }
    provider.send_message(**kwargs)  # type: ignore[arg-type]
    repeat = provider.send_message(**kwargs)  # type: ignore[arg-type]
    assert repeat.deduplicated
    assert provider.message_calls == 1


def test_a_different_attempt_gets_a_different_key_and_does_debit() -> None:
    # Idempotency must not swallow a legitimate second attempt.
    provider = SimulatedProvider(truths={"case_1": _truth(NOW)})
    provider.charge(case_id="case_1", amount=paise(100), idempotency_key="case_1:debit:0", at=NOW)
    provider.charge(case_id="case_1", amount=paise(100), idempotency_key="case_1:debit:1", at=NOW)
    assert provider.charge_calls == 2


# --- measurement integrity -------------------------------------------------


def test_self_cure_rate_is_comparable_across_arms() -> None:
    # Organic recoveries come from the world, not from the policy. If the arms
    # saw materially different self-cure rates, the comparison would be broken
    # -- this is the assertion that caught the original bug, where the control
    # arm stopped earlier and so was credited with fewer organic payments.
    _, outcomes, _, _ = _run(size=2000, seed=11)
    rates = {}
    for arm in ExperimentArm:
        rows = [o for o in outcomes if o.arm is arm]
        rates[arm] = sum(1 for o in rows if o.outcome_source == "organic") / len(rows)
    assert abs(rates[ExperimentArm.TREATMENT] - rates[ExperimentArm.CONTROL]) < 0.05


def test_observation_window_is_identical_across_arms() -> None:
    batch, _, _, _ = _run(size=400, seed=3)
    windows = {(sim.case.observation_closes_at - sim.failed_at) for sim in batch.cases}
    assert len(windows) == 1


def test_hard_declines_never_consume_a_debit_attempt() -> None:
    # The taxonomy's whole claim: retrying a dead instrument is waste. If a hard
    # case shows attempts, either the classifier or the gate has leaked.
    _, outcomes, _, _ = _run(size=1500, seed=5)
    hard = [
        o
        for o in outcomes
        if o.decline_class is DeclineClass.HARD and o.arm is ExperimentArm.TREATMENT
    ]
    assert hard, "expected hard declines in the batch"
    assert all(o.attempts == 0 for o in hard)


def test_policy_refusals_actually_fire() -> None:
    # A zero here would mean the compliance engine is decorative. The analysis
    # plan lists that as a falsifier of the design.
    _, outcomes, _, _ = _run(size=800, seed=9)
    assert sum(refusal_counts(outcomes).values()) > 0


def test_every_unrecovered_case_carries_a_stopping_reason() -> None:
    _, outcomes, _, _ = _run(size=800, seed=13)
    unrecovered = [o for o in outcomes if not o.recovered]
    assert unrecovered
    assert all(o.stop_reason is not None for o in unrecovered)
    assert sum(stop_reason_counts(outcomes).values()) >= len(unrecovered)


def test_batches_are_reproducible_from_the_seed() -> None:
    # The analysis plan requires a run to be repeatable after a bug fix so that
    # both the original and corrected results can be reported.
    first = generate(name="a", size=300, seed=99)
    second = generate(name="a", size=300, seed=99)
    assert [c.case.case_id for c in first.cases] == [c.case.case_id for c in second.cases]
    assert [int(c.case.amount) for c in first.cases] == [int(c.case.amount) for c in second.cases]
    assert [c.case.arm for c in first.cases] == [c.case.arm for c in second.cases]


def test_arm_assignment_is_close_to_balanced() -> None:
    batch = generate(name="a", size=2000, seed=21)
    treatment = len(batch.by_arm(ExperimentArm.TREATMENT))
    assert abs(treatment - 1000) < 60


def test_stratification_balances_decline_class_across_arms() -> None:
    batch = generate(name="a", size=2000, seed=23)
    for klass in DeclineClass:
        rows = [c for c in batch.cases if c.case.decline_class is klass]
        if len(rows) < 50:
            continue
        treated = sum(1 for c in rows if c.case.arm is ExperimentArm.TREATMENT)
        assert abs(treated / len(rows) - 0.5) < 0.1


# --- metrics ---------------------------------------------------------------


def test_lift_interval_straddling_zero_is_reported_as_such() -> None:
    flat = Lift(treatment=Proportion(50, 100), control=Proportion(50, 100))
    assert flat.value == 0.0
    assert flat.straddles_zero
    assert "straddles zero" in flat.describe()


def test_clear_difference_does_not_straddle_zero() -> None:
    strong = Lift(treatment=Proportion(800, 1000), control=Proportion(500, 1000))
    assert not strong.straddles_zero


def test_incremental_is_never_greater_than_gross() -> None:
    # Incremental strips out what the customer would have paid anyway, so it
    # must sit below the gross figure. If it did not, the holdout is being
    # mis-subtracted.
    _, outcomes, _, _ = _run(size=1200, seed=17)
    assert int(incremental_paise(outcomes)) <= int(gross_recovered_paise(outcomes))


def test_incremental_is_zero_when_the_agent_does_not_help() -> None:
    # Same planner in both arms: no lift, so no incremental money.
    batch = generate(name="null", size=800, seed=31)
    outcomes, _, _ = run_batch(
        batch,
        DeterministicArms(treatment=PlatformDefaultPlanner(), control=PlatformDefaultPlanner()),
    )
    lift = recovery_lift(outcomes)
    assert abs(lift.value) < 0.08
    if lift.value <= 0:
        assert int(incremental_paise(outcomes)) == 0


def test_subtype_weights_sum_to_one() -> None:
    _, outcomes, _, _ = _run(size=1500, seed=41)
    weights = natural_subtype_weights(outcomes)
    assert weights
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_enriched_batch_has_more_tail_than_population() -> None:
    _, population, _, _ = _run(size=1200, seed=51)
    _, enriched, _, _ = _run(size=1200, seed=51, enriched=True)
    pop_share = sum(1 for o in population if o.tail_subtype) / len(population)
    tail_share = sum(1 for o in enriched if o.tail_subtype) / len(enriched)
    assert tail_share > pop_share


def test_concurrency_does_not_change_the_measurement() -> None:
    # Concurrency touches the measurement path, so identical outcomes are the
    # correctness guarantee: same seed, same rows, same order. Shared state is
    # individually locked and routing happens single-threaded before any worker
    # starts, so the only thing threads change is wall clock.
    def run(workers: int) -> list[tuple[object, ...]]:
        batch = generate(name="det", size=600, seed=4242)
        arms = DeterministicArms(
            treatment=DeclineConditionalPlanner(), control=PlatformDefaultPlanner()
        )
        outcomes, _, _ = run_batch(batch, arms, workers=workers)
        return [
            (
                o.case_id,
                o.arm.value,
                o.recovered,
                o.outcome_source,
                o.attempts,
                o.messages,
                int(o.action_cost),
                o.stop_reason.value if o.stop_reason else None,
                o.hours_to_recovery,
            )
            for o in outcomes
        ]

    assert run(1) == run(8)


def test_concurrent_run_keeps_provider_counters_exact() -> None:
    # Counter increments are the classic lost-update race. If these drift, the
    # idempotency guarantee is not actually being measured.
    def counts(workers: int) -> tuple[int, int]:
        batch = generate(name="det", size=600, seed=99)
        arms = DeterministicArms(
            treatment=DeclineConditionalPlanner(), control=PlatformDefaultPlanner()
        )
        _, provider, _ = run_batch(batch, arms, workers=workers)
        return provider.charge_calls, provider.message_calls

    assert counts(1) == counts(8)


def test_workers_actually_parallelise() -> None:
    """Guard against the concurrency silently not being wired.

    The determinism test above compares serial and concurrent outcomes, but it
    passes trivially if *both* run serially -- which is exactly what happened
    once: a string patch that added the `workers` parameter to the signature
    silently failed to replace the loop body, and the identical-outcomes test
    reported success while the pool was never used.

    So this asserts the property the other test cannot see: with a planner that
    blocks, more workers must finish sooner and touch more than one thread.
    """
    import threading
    import time

    class Blocking:
        def __init__(self, inner: object) -> None:
            self.inner = inner
            self.threads: set[int] = set()

        def next_step(self, case: object, facts: object) -> object:
            self.threads.add(threading.get_ident())
            time.sleep(0.02)
            return self.inner.next_step(case, facts)  # type: ignore[attr-defined]

    def timed(workers: int) -> tuple[float, int]:
        treatment = Blocking(DeclineConditionalPlanner())
        control = Blocking(PlatformDefaultPlanner())
        arms = DeterministicArms(treatment=treatment, control=control)  # type: ignore[arg-type]
        started = time.monotonic()
        run_batch(generate(name="p", size=40, seed=5), arms, workers=workers)
        threads = treatment.threads | control.threads
        return time.monotonic() - started, len(threads)

    serial_s, serial_threads = timed(1)
    parallel_s, parallel_threads = timed(8)

    assert serial_threads == 1
    assert parallel_threads > 1, "pool never spawned a second thread"
    assert parallel_s < serial_s / 2, f"no speedup: {serial_s:.2f}s -> {parallel_s:.2f}s"
