"""A ledger store that also fans events out to live viewers.

The control room needs to see events as they happen. The constraint is that
watching must not change what is watched: a stalled browser, a closed tab, or a
subscriber that raises must not slow a case, block a worker, or alter an
outcome. The batch that produces R1 and R2 has to run identically whether or
not anyone is looking.

Three decisions carry that guarantee.

**Delivery is queued, not called back.** Subscribers hand over a bounded queue
and are woken by their own reader thread.

*Rejected:* invoking subscriber callbacks inline inside ``append``. A single
subscriber blocked on a TCP write to a browser that stopped reading would block
the case that produced the event -- turning "is the console open" into a
variable in the experiment.

**Queues are bounded and overflow drops the oldest.** A viewer that falls
behind resumes at the present, which is what a live console is for.

*Rejected:* unbounded queues. A tab closed without a clean disconnect is not
reaped until the next write fails, and until then it would accumulate the
entire batch in memory.

**Drops are counted and reported, not swallowed.** A console showing a partial
timeline while implying it is complete would be lying in exactly the way this
project spends its effort not doing. :meth:`Subscriber.take_dropped` lets the
view say how much it missed.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from collections import deque

from recovery.domain.events import AuditEvent, InMemoryLedger, LedgerStore

DEFAULT_QUEUE_SIZE = 2000
"""Per-viewer backlog. A 300-case run emits ~2,900 events, so a console that
freezes for a few seconds catches up rather than losing the run."""

DEFAULT_REPLAY_SIZE = 400
"""Recent events kept for a viewer that joins mid-run, so refreshing the page
does not show an empty screen."""

_OVERFLOW_ATTEMPTS = 4
"""Bounded retries when making room in a full queue. Concurrent writers can
refill the gap; giving up after a few tries keeps ``append`` non-blocking,
which is the whole point."""


class Subscriber:
    """One live viewer's slice of the event stream."""

    __slots__ = ("_dropped", "_lock", "_queue")

    def __init__(self, *, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue: queue.Queue[AuditEvent] = queue.Queue(maxsize=queue_size)
        self._dropped = 0
        self._lock = threading.Lock()

    def offer(self, event: AuditEvent) -> None:
        """Enqueue without ever blocking the writer.

        On overflow the oldest event is discarded so the viewer stays anchored
        to the present.
        """
        for _ in range(_OVERFLOW_ATTEMPTS):
            try:
                self._queue.put_nowait(event)
                return
            except queue.Full:
                self._discard_oldest()
        self._count_drop()

    def _discard_oldest(self) -> None:
        try:
            self._queue.get_nowait()
        except queue.Empty:
            return
        self._count_drop()

    def _count_drop(self) -> None:
        with self._lock:
            self._dropped += 1

    def drain(self, *, timeout: float) -> list[AuditEvent]:
        """Block up to ``timeout`` for one event, then flush the whole backlog.

        One wakeup per batch rather than per event: an SSE loop that emitted a
        single event per wakeup would fall permanently behind a thread pool.
        """
        try:
            first = self._queue.get(timeout=timeout)
        except queue.Empty:
            return []

        batch = [first]
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                return batch

    @property
    def dropped(self) -> int:
        """Events this viewer never saw."""
        with self._lock:
            return self._dropped

    def take_dropped(self) -> int:
        """Read and clear the gap, for reporting it once to the viewer."""
        with self._lock:
            missed, self._dropped = self._dropped, 0
            return missed


class BroadcastLedger:
    """A ``LedgerStore`` that stores through and fans out.

    Satisfies the protocol structurally, so ``Ledger(BroadcastLedger())`` is a
    drop-in for ``Ledger(InMemoryLedger())`` and the batch runner is unchanged.
    """

    def __init__(
        self,
        inner: LedgerStore | None = None,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        replay_size: int = DEFAULT_REPLAY_SIZE,
    ) -> None:
        self._inner: LedgerStore = inner if inner is not None else InMemoryLedger()
        self._queue_size = queue_size
        self._subscribers: list[Subscriber] = []
        self._recent: deque[AuditEvent] = deque(maxlen=replay_size)
        self._case_ids: dict[str, None] = {}
        self._lock = threading.Lock()

    # --- LedgerStore -------------------------------------------------------

    def append(self, event: AuditEvent) -> None:
        """Store first, broadcast second.

        Ordering matters: a viewer must never see an event the ledger refused,
        so a store that raises on a non-monotonic sequence stops the fan-out
        before it starts.
        """
        self._inner.append(event)

        with self._lock:
            self._recent.append(event)
            self._case_ids[event.case_id] = None
            targets = tuple(self._subscribers)

        for subscriber in targets:
            self._offer_safely(subscriber, event)

    def read_case(self, case_id: str) -> list[AuditEvent]:
        return self._inner.read_case(case_id)

    def next_seq(self, case_id: str) -> int:
        return self._inner.next_seq(case_id)

    # --- viewers -----------------------------------------------------------

    def subscribe(self, *, replay: int = 0) -> Subscriber:
        """Attach a new viewer, optionally seeded with recent history."""
        subscriber = Subscriber(queue_size=self._queue_size)
        if replay > 0:
            for event in self.recent(limit=replay):
                subscriber.offer(event)
        return self.attach(subscriber)

    def attach(self, subscriber: Subscriber) -> Subscriber:
        """Register an already-constructed viewer. Returns it for chaining."""
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    @staticmethod
    def _offer_safely(subscriber: Subscriber, event: AuditEvent) -> None:
        """A broken viewer is the viewer's problem, not the run's.

        Deliberately broad: any exception a subscriber raises is contained
        here, because the alternative is a display bug failing a money workflow.
        """
        with contextlib.suppress(Exception):
            subscriber.offer(event)

    # --- views -------------------------------------------------------------

    def recent(self, *, limit: int) -> list[AuditEvent]:
        """The newest ``limit`` events, oldest first."""
        with self._lock:
            events = list(self._recent)
        return events[-limit:] if limit < len(events) else events

    def all_cases(self) -> list[str]:
        """Case ids in first-seen order.

        Tracked here rather than delegated: ``LedgerStore`` does not promise
        enumeration, and the console needs a case list from whatever store is
        underneath.
        """
        with self._lock:
            return list(self._case_ids)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)
