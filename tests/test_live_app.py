"""Control room application tests: run lifecycle, event flow, drill-down.

The claim this file defends is that the console *watches* a run rather than
participating in it. The run goes through the same ``run_batch`` the measured
batches use, with the same router and the same policy engine; the only
difference is which ledger store the events land in.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from recovery.live.app import ControlRoom, build_router
from recovery.live.server import CONSOLE_HEADER, Request


def _get(router: object, path: str) -> object:
    assert hasattr(router, "dispatch")
    return router.dispatch(Request(method="GET", path=path))  # type: ignore[attr-defined]


def _post(router: object, path: str) -> object:
    assert hasattr(router, "dispatch")
    return router.dispatch(  # type: ignore[attr-defined]
        Request(method="POST", path=path, headers={CONSOLE_HEADER: "1"})
    )


class _GatedRoom(ControlRoom):
    """A room whose run blocks until the test releases it.

    Timing-based concurrency tests -- start a big batch and hope it is still
    going -- pass on a fast machine and fail on a loaded one. Holding the run
    open explicitly makes the guard the thing under test rather than the clock.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.release = threading.Event()
        self.entered = threading.Event()

    def _run(self) -> None:
        self.entered.set()
        self.release.wait(timeout=10)
        super()._run()


def _payload(response: object) -> dict[str, object]:
    body = response.body  # type: ignore[attr-defined]
    parsed = json.loads(body)
    assert isinstance(parsed, dict)
    return parsed


# --- lifecycle -------------------------------------------------------------


def test_a_fresh_room_is_idle() -> None:
    room = ControlRoom()

    assert room.state()["status"] == "idle"


def test_a_run_completes_and_reports_its_size() -> None:
    room = ControlRoom(cases=8)
    room.start_run()

    assert room.wait(timeout=60) is True
    state = room.state()
    assert state["status"] == "finished"
    assert state["cases_total"] == 8


def test_a_second_run_is_refused_while_one_is_in_flight() -> None:
    # Two concurrent runs would interleave two case populations into one
    # stream, and the console would be showing a blend of two experiments.
    room = _GatedRoom(cases=4)
    room.start_run()
    room.entered.wait(timeout=10)
    try:
        assert room.start_run() is False, "the second start must be refused"
    finally:
        room.release.set()
        room.wait(timeout=60)


def test_a_finished_run_can_be_started_again() -> None:
    room = ControlRoom(cases=4)
    room.start_run()
    room.wait(timeout=60)

    assert room.start_run() is True
    room.wait(timeout=60)


# --- the non-interference seam ---------------------------------------------


def test_a_run_reaches_a_live_subscriber() -> None:
    room = ControlRoom(cases=6)
    subscriber = room.store.subscribe()

    room.start_run()
    room.wait(timeout=60)

    received = []
    while batch := subscriber.drain(timeout=0.2):
        received.extend(batch)
    assert received, "the console saw nothing of a completed run"
    assert {e.case_id for e in received}, "events must carry case ids"


def test_the_run_writes_through_to_the_ledger_store() -> None:
    room = ControlRoom(cases=4)
    room.start_run()
    room.wait(timeout=60)

    cases = room.store.all_cases()

    assert len(cases) == 4
    assert room.store.read_case(cases[0]) != []


def test_the_demo_script_provokes_a_real_refusal() -> None:
    # The control room is only worth watching if the policy engine visibly
    # says no. The demo script reaches for a debit before any notice exists,
    # which the mandate gate refuses with a named remedy.
    room = ControlRoom(cases=24)
    room.start_run()
    room.wait(timeout=120)

    summaries = [
        event.summary
        for case_id in room.store.all_cases()
        for event in room.store.read_case(case_id)
    ]

    assert any("refused" in summary for summary in summaries)


# --- routes ----------------------------------------------------------------


def test_the_console_is_served_at_the_root() -> None:
    response = _get(build_router(ControlRoom()), "/")

    assert response.status == 200  # type: ignore[attr-defined]
    assert b"<title>" in response.body  # type: ignore[attr-defined]


def test_state_is_readable_without_the_console_header() -> None:
    response = _get(build_router(ControlRoom()), "/api/state")

    assert _payload(response)["status"] == "idle"


def test_starting_a_run_over_the_api_needs_the_console_header() -> None:
    router = build_router(ControlRoom(cases=4))

    refused = router.dispatch(Request(method="POST", path="/api/run"))

    assert refused.status == 403


def test_starting_a_run_over_the_api_works() -> None:
    room = ControlRoom(cases=4)
    router = build_router(room)

    response = _post(router, "/api/run")
    room.wait(timeout=60)

    assert _payload(response)["started"] is True


def test_a_duplicate_start_over_the_api_is_a_conflict() -> None:
    room = _GatedRoom(cases=4)
    router = build_router(room)
    _post(router, "/api/run")
    room.entered.wait(timeout=10)
    try:
        assert _post(router, "/api/run").status == 409  # type: ignore[attr-defined]
    finally:
        room.release.set()
        room.wait(timeout=60)


def test_a_case_timeline_is_available_for_drill_down() -> None:
    room = ControlRoom(cases=4)
    router = build_router(room)
    room.start_run()
    room.wait(timeout=60)

    case_id = room.store.all_cases()[0]
    events = _payload(_get(router, f"/api/case/{case_id}"))["events"]

    assert isinstance(events, list)
    assert events, "a case with no timeline cannot be audited"


def test_an_unknown_case_is_404() -> None:
    response = _get(build_router(ControlRoom()), "/api/case/case_nope")

    assert response.status == 404  # type: ignore[attr-defined]


def test_the_downtime_panel_is_readable_without_credentials() -> None:
    # The console has to start with nothing configured, and the panel has to
    # say why it is empty rather than imply an all-clear.
    payload = _payload(_get(build_router(ControlRoom()), "/api/downtime"))

    assert payload["outages"] == []
    assert payload["available"] in (True, False)
    if payload["available"] is False:
        assert payload["reason"]


def test_the_cold_open_is_served() -> None:
    response = _get(build_router(ControlRoom()), "/hero")

    assert response.status == 200  # type: ignore[attr-defined]
    assert b"RECOUP" in response.body  # type: ignore[attr-defined]


def test_hero_media_is_404_when_no_clip_is_present() -> None:
    room = ControlRoom()
    room.assets = room.assets / "definitely-not-here"

    assert _get(build_router(room), "/hero/media").status == 404  # type: ignore[attr-defined]


def test_the_event_stream_is_an_sse_response() -> None:
    response = _get(build_router(ControlRoom()), "/api/events")

    assert response.stream is not None  # type: ignore[attr-defined]
    response.stream.close()  # type: ignore[attr-defined]
