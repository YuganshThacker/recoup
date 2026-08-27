"""The control room: run lifecycle, routes, and the drill-down view.

Everything here reads. The one write is starting a demo batch, and that batch
goes through the same :func:`~recovery.batch.runner.run_batch`, the same arm
router and the same :class:`~recovery.policy.engine.PolicyEngine` that produced
the numbers in ``docs/RESULTS.md``. The only substitution is the ledger store,
which is what lets the console watch without being part of what it watches.
"""

from __future__ import annotations

import threading
from typing import Any

from recovery.agent.planner import AgentPlanner
from recovery.agent.router import AgentTailArms
from recovery.batch.runner import CaseOutcome, run_batch
from recovery.domain.events import Ledger
from recovery.live.broadcast import BroadcastLedger
from recovery.live.console import render_console
from recovery.live.demo import DEMO_SEED, DemoClient
from recovery.live.redteam import UnknownAttack, catalogue, run_attack
from recovery.live.server import (
    Request,
    Response,
    Router,
    event_stream,
    html_response,
    json_response,
)
from recovery.planner.rules import DeclineConditionalPlanner, PlatformDefaultPlanner
from recovery.sim.generator import generate

DEFAULT_DEMO_CASES = 24
"""Enough to fill the screen and finish inside a demo beat. The measured
batches are 900 and 1,600; this is a stage, not an experiment."""

STREAM_REPLAY = 200
"""Events a joining viewer is given, so refreshing mid-run is not a blank
screen."""


class ControlRoom:
    """Owns the ledger every view reads from, and the one run that writes it."""

    def __init__(self, *, cases: int = DEFAULT_DEMO_CASES, seed: int = DEMO_SEED) -> None:
        self.store = BroadcastLedger()
        self.ledger = Ledger(self.store)
        self.cases = cases
        self.seed = seed
        self._lock = threading.Lock()
        self._status = "idle"
        self._finished = threading.Event()
        self._finished.set()
        self._outcomes: list[CaseOutcome] = []
        self._error: str | None = None

    # --- lifecycle ---------------------------------------------------------

    def start_run(self) -> bool:
        """Begin a demo batch. Returns ``False`` if one is already in flight.

        Refusing rather than queueing is deliberate: two concurrent runs would
        interleave two populations into one stream, and the console would be
        showing a blend of two experiments while implying it was one.
        """
        with self._lock:
            if self._status == "running":
                return False
            self._status = "running"
            self._error = None
            self._outcomes = []
            self._finished.clear()

        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self) -> None:
        try:
            # The tail-enriched shape, which is the population the agent
            # actually works on: unmapped codes and high-value cases, per
            # docs/analysis-plan.md. The natural mix is ~1% unknown, so at demo
            # size the console would show a batch the agent barely touches.
            batch = generate(name="control-room", size=self.cases, seed=self.seed, enriched=True)
            rules = DeclineConditionalPlanner()
            router = AgentTailArms(
                rules=rules,
                control=PlatformDefaultPlanner(),
                agent=AgentPlanner(client=DemoClient(), fallback=rules),
                seed=self.seed,
            )
            outcomes, _provider, _ledger = run_batch(batch, router, workers=1, ledger=self.ledger)
        except Exception as exc:  # surfaced to the console as a failed run, not swallowed
            with self._lock:
                self._status = "failed"
                self._error = f"{type(exc).__name__}: {exc}"
        else:
            with self._lock:
                self._status = "finished"
                self._outcomes = outcomes
        finally:
            self._finished.set()

    def wait(self, *, timeout: float) -> bool:
        """Block until the current run settles. For tests and the CLI."""
        return self._finished.wait(timeout=timeout)

    # --- views -------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """Run status only.

        The console derives every counter -- money, refusals, gate outcomes --
        from the audit events themselves rather than from a server-side tally.
        That is the point: a number on screen with no event behind it would be
        a number this project cannot source.
        """
        with self._lock:
            return {
                "status": self._status,
                "cases_total": self.cases,
                "cases_seen": len(self.store.all_cases()),
                "seed": self.seed,
                "agent": "demo-stand-in",
                "error": self._error,
                "viewers": self.store.subscriber_count,
            }

    def timeline(self, case_id: str) -> list[dict[str, Any]]:
        """Full replayable history for one case."""
        return [
            {
                "seq": event.seq,
                "at": event.occurred_at.isoformat(),
                "kind": event.kind.value,
                "actor": event.actor.value,
                "summary": event.summary,
                "payload": event.payload,
            }
            for event in self.store.read_case(case_id)
        ]


def build_router(room: ControlRoom) -> Router:
    """Wire the control room's routes."""
    router = Router()

    def console(_request: Request, **_params: str) -> Response:
        return html_response(render_console())

    def state(_request: Request, **_params: str) -> Response:
        return json_response(room.state())

    def events(_request: Request, **_params: str) -> Response:
        return Response(
            content_type="text/event-stream",
            stream=event_stream(room.store, replay=STREAM_REPLAY),
        )

    def run(_request: Request, **_params: str) -> Response:
        if room.start_run():
            return json_response({"started": True})
        return json_response({"started": False, "error": "a run is already in flight"}, status=409)

    def redteam_list(_request: Request, **_params: str) -> Response:
        return json_response({"attacks": catalogue()})

    def redteam_run(_request: Request, **params: str) -> Response:
        """Fire one attack for real, into the room's own ledger.

        Recording into the live ledger is the point: the refusal appears in
        the GOVERN lane and lights the gate matrix behind the panel, so the
        audience sees the defence in the same place they have been watching
        the system work.
        """
        try:
            result = run_attack(params["slug"], ledger=room.ledger)
        except UnknownAttack as exc:
            return json_response({"error": str(exc)}, status=404)
        return json_response(
            {
                "slug": result.slug,
                "title": result.title,
                "claim": result.claim,
                "held": result.held,
                "verdict": result.verdict,
                "evidence": list(result.evidence),
                "defended_by": result.defended_by,
                "test": result.test,
                "case_id": result.case_id,
            }
        )

    def case(_request: Request, **params: str) -> Response:
        events_ = room.timeline(params["case_id"])
        if not events_:
            return json_response({"error": "unknown case"}, status=404)
        return json_response({"case_id": params["case_id"], "events": events_})

    router.get("/", console)
    router.get("/api/state", state)
    router.get("/api/events", events)
    router.post("/api/run", run)
    router.get("/api/case/<case_id>", case)
    router.get("/api/redteam", redteam_list)
    router.post("/api/redteam/<slug>", redteam_run)
    return router
