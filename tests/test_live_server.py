"""Transport tests for the live surface: routing, SSE, and its security posture.

The control room exposes a red-team panel that deliberately drives real code
paths. That makes the transport a security surface, not just plumbing: it binds
loopback, it rejects cross-site writes, and a handler that raises must not take
the demo down with it.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from recovery.domain.events import Actor, AuditEvent, EventKind
from recovery.live.broadcast import BroadcastLedger
from recovery.live.server import (
    CONSOLE_HEADER,
    ConsoleServer,
    Request,
    Response,
    Router,
    event_stream,
    json_response,
    media_response,
)


def _event(seq: int = 1, case_id: str = "case_1") -> AuditEvent:
    return AuditEvent(
        event_id=f"evt_{seq}",
        case_id=case_id,
        seq=seq,
        occurred_at=datetime(2026, 8, 27, 10, 0, tzinfo=UTC),
        kind=EventKind.POLICY_EVALUATED,
        actor=Actor.RULES,
        summary="permitted: retry_debit",
    )


def _request(method: str = "GET", path: str = "/", **kwargs: object) -> Request:
    return Request(
        method=method,
        path=path,
        query=dict(kwargs.get("query", {})),  # type: ignore[arg-type]
        body=bytes(kwargs.get("body", b"")),  # type: ignore[arg-type]
        headers=dict(kwargs.get("headers", {})),  # type: ignore[arg-type]
    )


# --- routing ---------------------------------------------------------------


def test_router_matches_an_exact_path() -> None:
    router = Router()
    router.get("/api/health", lambda _req, **_p: json_response({"ok": True}))

    assert router.dispatch(_request(path="/api/health")).status == 200


def test_router_extracts_path_parameters() -> None:
    router = Router()
    router.get("/api/case/<case_id>/xray", lambda _req, **p: json_response({"id": p["case_id"]}))

    body = router.dispatch(_request(path="/api/case/case_42/xray")).body

    assert json.loads(body)["id"] == "case_42"


def test_an_unknown_path_is_404() -> None:
    assert Router().dispatch(_request(path="/nope")).status == 404


def test_a_known_path_with_the_wrong_method_is_405() -> None:
    router = Router()
    router.post("/api/run", lambda _req, **_p: json_response({}))

    assert router.dispatch(_request(method="GET", path="/api/run")).status == 405


def test_a_handler_that_raises_becomes_500_rather_than_taking_the_demo_down() -> None:
    def explode(_req: Request, **_p: str) -> Response:
        raise RuntimeError("boom")

    router = Router()
    router.get("/api/boom", explode)

    response = router.dispatch(_request(path="/api/boom"))

    assert response.status == 500
    assert b"boom" not in response.body, "internal detail must not leak to the page"


# --- cross-site protection -------------------------------------------------


def test_a_write_route_rejects_a_request_without_the_console_header() -> None:
    # The red-team routes drive real code. A cross-site form post cannot set a
    # custom header, so requiring one keeps another page on the venue wifi from
    # firing them.
    router = Router()
    router.post("/api/redteam/forge", lambda _req, **_p: json_response({}))

    assert router.dispatch(_request(method="POST", path="/api/redteam/forge")).status == 403


def test_a_write_route_accepts_a_request_from_the_console() -> None:
    router = Router()
    router.post("/api/redteam/forge", lambda _req, **_p: json_response({"ran": True}))

    response = router.dispatch(
        _request(method="POST", path="/api/redteam/forge", headers={CONSOLE_HEADER: "1"})
    )

    assert response.status == 200


def test_read_routes_need_no_header() -> None:
    router = Router()
    router.get("/api/state", lambda _req, **_p: json_response({}))

    assert router.dispatch(_request(path="/api/state")).status == 200


# --- responses -------------------------------------------------------------


def test_json_response_sets_its_content_type() -> None:
    assert json_response({"a": 1}).content_type.startswith("application/json")


def test_query_strings_are_parsed() -> None:
    router = Router()
    router.get("/api/x", lambda req, **_p: json_response({"n": req.query.get("n")}))

    body = router.dispatch(_request(path="/api/x", query={"n": "5"})).body

    assert json.loads(body)["n"] == "5"


# --- server-sent events ----------------------------------------------------


def test_the_stream_emits_ledger_events_as_sse_frames() -> None:
    ledger = BroadcastLedger()
    ledger.append(_event(1))
    stop = threading.Event()

    stream = event_stream(ledger, replay=10, stop=stop, heartbeat_seconds=0.01)
    frame = next(stream)
    stop.set()
    stream.close()

    assert frame.startswith(b"event: audit\ndata: ")
    assert frame.endswith(b"\n\n")
    assert json.loads(frame.split(b"data: ", 1)[1])["summary"] == "permitted: retry_debit"


def test_an_idle_stream_emits_a_heartbeat_so_proxies_do_not_close_it() -> None:
    ledger = BroadcastLedger()
    stop = threading.Event()

    stream = event_stream(ledger, replay=0, stop=stop, heartbeat_seconds=0.01)
    frame = next(stream)
    stop.set()
    stream.close()

    assert frame.startswith(b":")


def test_closing_the_stream_unsubscribes_the_viewer() -> None:
    # A refreshed tab leaks a subscriber otherwise, and every leak keeps taking
    # a copy of every event for the rest of the run.
    ledger = BroadcastLedger()
    stop = threading.Event()

    stream = event_stream(ledger, replay=0, stop=stop, heartbeat_seconds=0.01)
    next(stream)
    assert ledger.subscriber_count == 1

    stream.close()

    assert ledger.subscriber_count == 0


def test_a_viewer_that_fell_behind_is_told_what_it_missed() -> None:
    ledger = BroadcastLedger(queue_size=1)
    stop = threading.Event()
    stream = event_stream(ledger, replay=0, stop=stop, heartbeat_seconds=0.01)
    # The generator is lazy, so it has not subscribed until it is first pulled.
    # Appending before that would be appending to an audience of nobody.
    next(stream)

    for seq in (1, 2, 3, 4):
        ledger.append(_event(seq))

    frames = b"".join(next(stream) for _ in range(3))
    stop.set()
    stream.close()

    assert b"event: gap\n" in frames


# --- media -----------------------------------------------------------------


def test_a_whole_file_is_served_when_no_range_is_asked_for(tmp_path: Path) -> None:
    clip = tmp_path / "hero.mp4"
    clip.write_bytes(b"0123456789")

    response = media_response(clip, "video/mp4", range_header=None)

    assert response.status == 200
    assert response.body == b"0123456789"
    assert response.headers["Accept-Ranges"] == "bytes"


def test_a_byte_range_is_honoured(tmp_path: Path) -> None:
    # Chrome asks for one on <video> and handles a flat 200 unevenly.
    clip = tmp_path / "hero.mp4"
    clip.write_bytes(b"0123456789")

    response = media_response(clip, "video/mp4", range_header="bytes=2-5")

    assert response.status == 206
    assert response.body == b"2345"
    assert response.headers["Content-Range"] == "bytes 2-5/10"


def test_an_open_ended_range_runs_to_the_end(tmp_path: Path) -> None:
    clip = tmp_path / "hero.mp4"
    clip.write_bytes(b"0123456789")

    response = media_response(clip, "video/mp4", range_header="bytes=7-")

    assert response.body == b"789"
    assert response.headers["Content-Range"] == "bytes 7-9/10"


@pytest.mark.parametrize(
    "header", ["bytes=99-200", "bytes=abc-def", "kilobytes=1-2", "bytes=5-1", ""]
)
def test_an_unusable_range_falls_back_to_the_whole_file(header: str, tmp_path: Path) -> None:
    # A partial-content negotiation is not worth failing a demo over.
    clip = tmp_path / "hero.mp4"
    clip.write_bytes(b"0123456789")

    assert media_response(clip, "video/mp4", range_header=header).status == 200


# --- the real socket -------------------------------------------------------


def test_the_server_answers_over_a_real_socket() -> None:
    router = Router()
    router.get("/api/health", lambda _req, **_p: json_response({"ok": True}))
    server = ConsoleServer(router, port=0)

    with (
        server.running(),
        urllib.request.urlopen(f"{server.url}/api/health", timeout=5) as response,
    ):
        assert json.load(response)["ok"] is True


def test_it_binds_loopback_by_default() -> None:
    # The console can trigger real actions. Exposing it on a venue network by
    # default would be handing that panel to the room.
    assert ConsoleServer(Router(), port=0).host == "127.0.0.1"


def test_a_404_over_the_socket_does_not_kill_the_server() -> None:
    router = Router()
    router.get("/api/health", lambda _req, **_p: json_response({"ok": True}))
    server = ConsoleServer(router, port=0)

    with server.running():
        try:
            urllib.request.urlopen(f"{server.url}/missing", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404

        with urllib.request.urlopen(f"{server.url}/api/health", timeout=5) as response:
            assert response.status == 200
