"""Live surface tests: the broadcast ledger that feeds the control room.

The load-bearing property here is *non-interference*. The console is a viewer,
never a participant. A stalled browser, a closed tab, or a subscriber that
raises must not slow, block, or alter a batch run -- because a demo layer that
perturbs the experiment would invalidate the numbers in RESULTS.md.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from recovery.domain.events import Actor, AuditEvent, EventKind, InMemoryLedger, Ledger
from recovery.live.broadcast import BroadcastLedger, Subscriber


def _event(case_id: str = "case_1", seq: int = 1) -> AuditEvent:
    return AuditEvent(
        event_id=f"evt_{case_id}_{seq}",
        case_id=case_id,
        seq=seq,
        occurred_at=datetime(2026, 8, 27, 10, 0, tzinfo=UTC),
        kind=EventKind.CASE_DETECTED,
        actor=Actor.WEBHOOK,
        summary="charge failed",
    )


# --- fan-out ---------------------------------------------------------------


def test_appended_events_reach_a_subscriber() -> None:
    ledger = BroadcastLedger()
    sub = ledger.subscribe()

    ledger.append(_event())

    assert [e.event_id for e in sub.drain(timeout=1.0)] == ["evt_case_1_1"]


def test_every_subscriber_gets_its_own_copy() -> None:
    ledger = BroadcastLedger()
    first, second = ledger.subscribe(), ledger.subscribe()

    ledger.append(_event())

    assert len(first.drain(timeout=1.0)) == 1
    assert len(second.drain(timeout=1.0)) == 1


def test_unsubscribe_stops_delivery() -> None:
    ledger = BroadcastLedger()
    sub = ledger.subscribe()
    ledger.unsubscribe(sub)

    ledger.append(_event())

    assert sub.drain(timeout=0.05) == []


def test_drain_returns_empty_rather_than_blocking_forever() -> None:
    ledger = BroadcastLedger()
    sub = ledger.subscribe()

    assert sub.drain(timeout=0.01) == []


def test_drain_collects_everything_queued_in_one_call() -> None:
    # The SSE loop wakes on one event and should flush the whole backlog, not
    # dribble one event per wakeup.
    ledger = BroadcastLedger()
    sub = ledger.subscribe()

    for seq in (1, 2, 3):
        ledger.append(_event(seq=seq))

    assert len(sub.drain(timeout=1.0)) == 3


# --- non-interference ------------------------------------------------------


def test_a_raising_subscriber_does_not_break_the_write_path() -> None:
    ledger = BroadcastLedger()
    healthy = ledger.subscribe()

    class Exploding(Subscriber):
        def offer(self, event: AuditEvent) -> None:
            raise RuntimeError("subscriber blew up")

    ledger.attach(Exploding(queue_size=8))

    ledger.append(_event())  # must not raise

    assert len(healthy.drain(timeout=1.0)) == 1
    assert ledger.read_case("case_1") != []


def test_a_full_subscriber_drops_events_rather_than_blocking_the_writer() -> None:
    ledger = BroadcastLedger(queue_size=2)
    sub = ledger.subscribe()

    for seq in (1, 2, 3, 4):
        ledger.append(_event(seq=seq))

    assert sub.dropped == 2


def test_a_full_subscriber_keeps_the_newest_events() -> None:
    # A live console cares about now. When the queue overflows the oldest goes,
    # so a viewer that falls behind resumes at the present rather than the past.
    ledger = BroadcastLedger(queue_size=2)
    sub = ledger.subscribe()

    for seq in (1, 2, 3, 4):
        ledger.append(_event(seq=seq))

    assert [e.seq for e in sub.drain(timeout=1.0)] == [3, 4]


def test_dropped_events_are_counted_so_the_console_can_admit_the_gap() -> None:
    # Silently showing a partial timeline would make the console lie. The count
    # is exposed so the view can say how much it missed.
    ledger = BroadcastLedger(queue_size=1)
    sub = ledger.subscribe()

    for seq in (1, 2, 3):
        ledger.append(_event(seq=seq))

    assert sub.dropped == 2
    sub.drain(timeout=1.0)
    assert sub.take_dropped() == 2
    assert sub.take_dropped() == 0, "reading the gap clears it"


# --- pass-through to the wrapped store -------------------------------------


def test_reads_pass_through_to_the_wrapped_store() -> None:
    inner = InMemoryLedger()
    ledger = BroadcastLedger(inner)

    ledger.append(_event())

    assert [e.event_id for e in inner.read_case("case_1")] == ["evt_case_1_1"]
    assert ledger.next_seq("case_1") == 2


def test_the_wrapped_store_still_enforces_monotonic_seq() -> None:
    # Wrapping must not soften the ledger's own invariant.
    ledger = BroadcastLedger()
    ledger.append(_event(seq=1))

    with pytest.raises(ValueError, match="monotonic"):
        ledger.append(_event(seq=7))


def test_a_rejected_append_is_not_broadcast() -> None:
    # The console must never show an event the ledger refused to store.
    ledger = BroadcastLedger()
    sub = ledger.subscribe()
    ledger.append(_event(seq=1))
    sub.drain(timeout=1.0)

    with pytest.raises(ValueError):
        ledger.append(_event(seq=7))

    assert sub.drain(timeout=0.05) == []


def test_it_satisfies_the_ledger_store_protocol() -> None:
    # The whole point: the batch runner takes it without changing a line.
    ledger = BroadcastLedger()
    writer = Ledger(ledger)

    writer.record("case_9", EventKind.CASE_DETECTED, Actor.WEBHOOK, "detected")

    assert [e.summary for e in writer.history("case_9")] == ["detected"]


def test_case_ids_are_tracked_for_the_case_list() -> None:
    ledger = BroadcastLedger()
    ledger.append(_event("case_a", 1))
    ledger.append(_event("case_b", 1))

    assert ledger.all_cases() == ["case_a", "case_b"]


# --- replay ----------------------------------------------------------------


def test_a_late_subscriber_can_replay_recent_events() -> None:
    # Refreshing the console mid-run should not show an empty screen.
    ledger = BroadcastLedger()
    for seq in (1, 2, 3):
        ledger.append(_event(seq=seq))

    sub = ledger.subscribe(replay=2)

    assert [e.seq for e in sub.drain(timeout=1.0)] == [2, 3]


def test_replay_is_off_by_default() -> None:
    ledger = BroadcastLedger()
    ledger.append(_event())

    assert ledger.subscribe().drain(timeout=0.05) == []


def test_the_replay_buffer_is_bounded() -> None:
    ledger = BroadcastLedger(replay_size=3)
    for seq in range(1, 11):
        ledger.append(_event(seq=seq))

    assert [e.seq for e in ledger.recent(limit=100)] == [8, 9, 10]


# --- concurrency -----------------------------------------------------------


def test_concurrent_appends_all_reach_the_subscriber() -> None:
    # Cases run on a thread pool. Every event has to survive the crossing.
    ledger = BroadcastLedger(queue_size=1000)
    sub = ledger.subscribe()

    def write(case: int) -> None:
        for seq in range(1, 11):
            ledger.append(_event(f"case_{case}", seq))

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    received: list[AuditEvent] = []
    while batch := sub.drain(timeout=0.2):
        received.extend(batch)

    assert len(received) == 80
    assert sub.dropped == 0
