"""Append-only audit ledger.

The track's bar asks for an audit trail, and asks that every money action be
explainable. That means one thing concretely: for any case, you can replay
``trigger -> diagnosis -> candidate actions -> policy checks -> execution ->
outcome`` and see why each step happened.

Design rules, all load-bearing:

* **Append only.** No updates, no deletes. Correcting the record means
  appending a correction event. A ledger you can edit is not evidence.
* **Monotonic ``seq`` per case.** Webhooks arrive out of order and more than
  once; the ledger records what *we* did, in the order we did it, independent
  of provider delivery order.
* **Every actor is named.** A human, a rule, and the model all write to the
  same ledger through the same interface, so "who decided this" is always
  answerable.
* **Gate results are recorded even when they pass.** A compliance engine that
  only logs refusals cannot prove it ran.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol


class Actor(StrEnum):
    """Who caused this event."""

    WEBHOOK = "webhook"
    RULES = "rules"
    AGENT = "agent"
    SCHEDULER = "scheduler"
    OPERATOR = "operator"
    SYSTEM = "system"


class EventKind(StrEnum):
    CASE_DETECTED = "case_detected"
    ARM_ASSIGNED = "arm_assigned"
    STATE_CHANGED = "state_changed"
    DIAGNOSIS_PRODUCED = "diagnosis_produced"
    ACTIONS_PROPOSED = "actions_proposed"
    POLICY_EVALUATED = "policy_evaluated"
    ACTION_REFUSED = "action_refused"
    NOTICE_SENT = "notice_sent"
    ACTION_SCHEDULED = "action_scheduled"
    ACTION_EXECUTED = "action_executed"
    ACTION_DEDUPED = "action_deduped"
    PROVIDER_CALLBACK = "provider_callback"
    OUTCOME_RECORDED = "outcome_recorded"
    CASE_STOPPED = "case_stopped"
    CORRECTION = "correction"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One immutable line in a case's history."""

    event_id: str
    case_id: str
    seq: int
    occurred_at: datetime
    kind: EventKind
    actor: Actor
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)

    # Set when the event was produced by a model call, so LLM contribution and
    # cost can be attributed per case without a second bookkeeping system.
    model: str | None = None
    cost_micros: int | None = None
    latency_ms: int | None = None

    def to_json(self) -> str:
        """Serialise for storage. Integers stay integers; no float ever."""
        return json.dumps(
            {
                "event_id": self.event_id,
                "case_id": self.case_id,
                "seq": self.seq,
                "occurred_at": self.occurred_at.isoformat(),
                "kind": self.kind.value,
                "actor": self.actor.value,
                "summary": self.summary,
                "payload": self.payload,
                "model": self.model,
                "cost_micros": self.cost_micros,
                "latency_ms": self.latency_ms,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


class LedgerStore(Protocol):
    """Persistence boundary. Kept narrow so SQLite, Postgres, and the in-memory
    test double are interchangeable."""

    def append(self, event: AuditEvent) -> None: ...

    def read_case(self, case_id: str) -> list[AuditEvent]: ...

    def next_seq(self, case_id: str) -> int: ...


class InMemoryLedger:
    """Test double and batch-run store.

    A synthetic batch of a few thousand cases fits in memory comfortably, and
    keeping the batch runner off a database makes runs reproducible from a seed
    with no cleanup step between them.
    """

    def __init__(self) -> None:
        self._events: dict[str, list[AuditEvent]] = {}
        self._lock = threading.Lock()

    def append(self, event: AuditEvent) -> None:
        with self._lock:
            self._append_locked(event)

    def _append_locked(self, event: AuditEvent) -> None:
        history = self._events.setdefault(event.case_id, [])
        expected = len(history) + 1
        if event.seq != expected:
            raise ValueError(
                f"case {event.case_id}: ledger seq must be monotonic, "
                f"expected {expected}, got {event.seq}"
            )
        history.append(event)

    def read_case(self, case_id: str) -> list[AuditEvent]:
        return list(self._events.get(case_id, []))

    def next_seq(self, case_id: str) -> int:
        return len(self._events.get(case_id, [])) + 1

    def all_cases(self) -> list[str]:
        return list(self._events)


class Ledger:
    """Convenience writer that assigns ids and sequence numbers.

    Sequence assignment and append are one atomic step. Cases run concurrently,
    and a read-then-write across threads would interleave into duplicate
    sequence numbers -- corrupting the one structure the audit trail depends on
    being ordered.
    """

    def __init__(self, store: LedgerStore) -> None:
        self._store = store
        self._lock = threading.Lock()

    def record(
        self,
        case_id: str,
        kind: EventKind,
        actor: Actor,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Append an event. Returns it so callers can attach it to a response."""
        with self._lock:
            event = AuditEvent(
                event_id=str(uuid.uuid4()),
                case_id=case_id,
                seq=self._store.next_seq(case_id),
                occurred_at=datetime.now(UTC),
                kind=kind,
                actor=actor,
                summary=summary,
                payload=payload or {},
            )
            self._store.append(event)
        return event

    def history(self, case_id: str) -> list[AuditEvent]:
        """Full replayable history for the drill-down view."""
        return self._store.read_case(case_id)
