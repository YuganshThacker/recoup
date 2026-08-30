"""The control room: run lifecycle, routes, and the drill-down view.

Everything here reads. The one write is starting a demo batch, and that batch
goes through the same :func:`~recovery.batch.runner.run_batch`, the same arm
router and the same :class:`~recovery.policy.engine.PolicyEngine` that produced
the numbers in ``docs/RESULTS.md``. The only substitution is the ledger store,
which is what lets the console watch without being part of what it watches.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recovery.agent.planner import AgentPlanner
from recovery.agent.router import AgentTailArms
from recovery.batch.runner import CaseOutcome, run_batch
from recovery.domain.events import Actor, Ledger
from recovery.domain.money import format_inr
from recovery.live.broadcast import BroadcastLedger
from recovery.live.console import render_console
from recovery.live.demo import DEMO_SEED, DemoClient
from recovery.live.downtime import DowntimeSource
from recovery.live.hero import MEDIA_TYPES, find_hero_media, render_hero
from recovery.live.race_page import render_race
from recovery.live.redteam import UnknownAttack, catalogue, run_attack
from recovery.live.replay import find_divergent_cases, replay_case
from recovery.live.server import (
    Request,
    Response,
    Router,
    event_stream,
    html_response,
    json_response,
    media_response,
)
from recovery.live.voice import (
    CallFacts,
    VoiceSession,
    demo_facts,
    keyword_ears,
    model_ears,
)
from recovery.live.xray import build_xray
from recovery.live.xray_page import render_xray
from recovery.planner.rules import DeclineConditionalPlanner, PlatformDefaultPlanner
from recovery.policy.actions import ActionKind, Channel, ProposedAction
from recovery.policy.engine import PolicyEngine
from recovery.sim.generator import generate
from recovery.templates import bind_variables

DEFAULT_DEMO_CASES = 24
"""Enough to fill the screen and finish inside a demo beat. The measured
batches are 900 and 1,600; this is a stage, not an experiment."""

ASSET_DIR = Path(__file__).resolve().parents[3] / "assets"
"""Where a cold-open clip goes. Outside the package: it is a demo asset, not
code, and it should not end up in a wheel."""

STREAM_REPLAY = 200
"""Events a joining viewer is given, so refreshing mid-run is not a blank
screen."""


class NoCallInProgress(Exception):
    """Asked to act on a call that is not open."""


def _gate_payload(result: Any) -> dict[str, Any]:
    return {
        "gate": result.gate.value,
        "passed": result.passed,
        "code": result.code.value if result.code else None,
        "explanation": result.explanation,
        "remediation": result.remediation.value if result.remediation else None,
    }


def _line_payload(line: Any) -> dict[str, Any] | None:
    if line is None:
        return None
    return {"node": line.node.value, "text": line.text, "ends_call": line.ends_call}


def _ears() -> tuple[Any, str]:
    """Model ears when a key is present, the measured baseline otherwise.

    Named rather than hidden. R3's whole result is the gap between these two,
    so a console that quietly ran keywords while the narration said "model"
    would be misreporting the one number this feature rests on.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        return keyword_ears, "keywords (baseline)"
    try:
        from recovery.agent.inbound import InboundExtractor
        from recovery.agent.openai_client import DEFAULT_MODEL, OpenAIClient

        extractor = InboundExtractor(OpenAIClient(model=DEFAULT_MODEL))
    except Exception:  # the SDK is an optional extra; absence is not an error
        return keyword_ears, "keywords (openai extra not installed)"
    return model_ears(extractor, name=DEFAULT_MODEL), DEFAULT_MODEL


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
        self.downtime = DowntimeSource.from_env()
        self.assets = ASSET_DIR
        self._call: VoiceSession | None = None
        self._ended_call: VoiceSession | None = None
        self._call_lock = threading.Lock()

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

    @property
    def _last_call(self) -> VoiceSession | None:
        return self._ended_call

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

    # --- the call ----------------------------------------------------------

    def open_call(self, *, amount_paise: int | None = None) -> dict[str, Any]:
        """Place a recovery call, gates first.

        A new session replaces any previous one. The alternative -- refusing
        while an old call is open -- would strand the demo behind a call
        nobody hung up.
        """
        facts = demo_facts()
        if amount_paise is not None:
            facts = CallFacts(
                merchant=facts.merchant,
                plan=facts.plan,
                amount_paise=amount_paise,
                due_date=facts.due_date,
            )
        ears, ears_name = _ears()
        session = VoiceSession(
            case_id=f"voice:{uuid.uuid4().hex[:8]}",
            facts=facts,
            ledger=self.ledger,
            now=datetime.now(UTC),
            ears=ears,
        )
        opened = session.open()
        with self._call_lock:
            self._call = session if opened.placed else None
            self._ended_call = session

        return {
            "placed": opened.placed,
            "case_id": session.case_id,
            "ears": ears_name,
            "amount": format_inr(facts.amount),
            "merchant": facts.merchant,
            "plan": facts.plan,
            "gates": [_gate_payload(r) for r in opened.decision.results],
            "say": _line_payload(opened.greeting),
        }

    def call_turn(self, transcript: str) -> dict[str, Any]:
        """One exchange on the open call."""
        with self._call_lock:
            session = self._call
        if session is None:
            raise NoCallInProgress("no call is in progress")

        turn = session.turn(transcript)
        if turn.ends_call:
            with self._call_lock:
                self._ended_call, self._call = session, None
        return {
            "heard": turn.heard,
            "heard_by": turn.heard_by,
            "model_output": turn.model_output,
            "facts": {k: str(v) for k, v in turn.facts.items()},
            "say": _line_payload(turn.say),
            "ends_call": turn.ends_call,
        }

    def probe_contact(self) -> dict[str, Any]:
        """Try to send an SMS against the call's current context.

        The closing beat: once a caller has said stop, this is refused by
        gate_suppression -- evaluated live, against the context the call
        itself updated, not by a job that runs later.
        """
        with self._call_lock:
            session = self._call or self._last_call
        if session is None:
            raise NoCallInProgress("no call to probe")

        action = ProposedAction(
            kind=ActionKind.SEND_REMINDER,
            channel=Channel.SMS,
            template_id="RP_DUNNING_01",
            variables=bind_variables("RP_DUNNING_01"),
            proposed_by=Actor.OPERATOR,
            rationale="probe: may we still contact this customer?",
        )
        decision = PolicyEngine().evaluate_and_record(action, session.context, self.ledger)
        return {
            "permitted": decision.permitted,
            "summary": decision.explain(),
            "gates": [_gate_payload(r) for r in decision.results],
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

    def xray(_request: Request, **params: str) -> Response:
        """The printable attestation for one case.

        Served outside /api because it is a document someone navigates to,
        bookmarks and prints -- not a fetch the console makes.
        """
        case_id = params["case_id"]
        report = build_xray(case_id, room.store.read_case(case_id))
        return html_response(render_xray(report), status=200 if report.events else 404)

    def downtime(_request: Request, **_params: str) -> Response:
        return json_response(room.downtime.view().payload())

    def hero(_request: Request, **_params: str) -> Response:
        return html_response(render_hero(media=find_hero_media(room.assets)))

    def hero_media(request: Request, **_params: str) -> Response:
        """Serve the cold-open clip, if there is one.

        The only file this server reads off disk, and it takes no path
        parameter: the filename comes from a fixed allowlist, so there is no
        user input for a traversal to travel in.
        """
        name = find_hero_media(room.assets)
        if name is None:
            return json_response({"error": "no hero media"}, status=404)
        path = room.assets / name
        return media_response(
            path,
            MEDIA_TYPES.get(path.suffix, "application/octet-stream"),
            range_header=request.header("Range"),
        )

    def race(_request: Request, **_params: str) -> Response:
        return html_response(render_race())

    def race_data(request: Request, **_params: str) -> Response:
        """One case under both planners.

        Defaults to the ranked hero case rather than a pinned id, so a
        different seed picks a different case instead of failing.
        """
        wanted = request.query.get("case") or find_divergent_cases().hero
        if wanted is None:
            return json_response({"error": "no divergent case in this batch"}, status=404)
        try:
            return json_response(replay_case(wanted).payload())
        except KeyError as exc:
            return json_response({"error": str(exc)}, status=404)

    def voice_open(request: Request, **_params: str) -> Response:
        amount = request.json().get("amount_paise")
        return json_response(room.open_call(amount_paise=int(amount) if amount else None))

    def voice_turn(request: Request, **_params: str) -> Response:
        transcript = str(request.json().get("transcript", "")).strip()
        if not transcript:
            return json_response({"error": "nothing was said"}, status=400)
        try:
            return json_response(room.call_turn(transcript))
        except NoCallInProgress as exc:
            return json_response({"error": str(exc)}, status=409)

    def voice_probe(_request: Request, **_params: str) -> Response:
        try:
            return json_response(room.probe_contact())
        except NoCallInProgress as exc:
            return json_response({"error": str(exc)}, status=409)

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
    router.get("/case/<case_id>/xray", xray)
    router.get("/api/downtime", downtime)
    router.get("/race", race)
    router.get("/api/race", race_data)
    router.get("/hero", hero)
    router.get("/hero/media", hero_media)
    router.get("/api/redteam", redteam_list)
    router.post("/api/redteam/<slug>", redteam_run)
    router.post("/api/voice/open", voice_open)
    router.post("/api/voice/turn", voice_turn)
    router.post("/api/voice/probe", voice_probe)
    return router
