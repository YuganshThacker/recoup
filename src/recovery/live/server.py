"""Transport for the control room: a routed HTTP server with an SSE stream.

Written against ``http.server`` rather than a framework. The repository's core
has no runtime dependencies and the report renders as one self-contained file
with no CDN; a demo layer that dragged in a web stack would spend that property
for a convenience worth less than it.

**This is a security surface, not plumbing.** The red-team panel drives real
policy code, so the transport takes a defensive posture:

* it binds loopback by default -- a console on the venue network is that panel
  handed to the room;
* write routes require a header a cross-site form post cannot set;
* a handler that raises becomes a 500 with a generic body, because a stack
  trace on screen during a demo is both a leak and a bad look.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from recovery.live.broadcast import BroadcastLedger

CONSOLE_HEADER = "X-Recoup-Console"
"""Required on write routes. A cross-site ``<form>`` post cannot set a custom
header, and the console's own ``fetch`` calls always do -- so the check costs
nothing and closes CSRF against endpoints that deliberately execute attacks."""

HEARTBEAT_SECONDS = 15.0
"""Idle comment interval. Without it an intermediary can close a quiet stream,
and the console goes dark for reasons that look like a bug in the system."""

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        """Case-insensitive lookup; HTTP header casing is not meaningful."""
        wanted = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == wanted), None)

    def json(self) -> dict[str, Any]:
        """Parse the body, treating an empty or malformed one as no arguments.

        Every write route has a working default, so a request without a body is
        a valid request rather than an error to surface mid-demo.
        """
        if not self.body:
            return {}
        try:
            parsed = json.loads(self.body)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True, slots=True)
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    stream: Generator[bytes, None, None] | None = None
    """Set for server-sent events, where the body is unbounded and the
    connection stays open for the life of the run."""


Handler = Callable[..., Response]


def json_response(payload: Any, status: int = 200) -> Response:
    """JSON with integers intact -- no float ever touches a money figure."""
    body = json.dumps(payload, separators=(",", ":"), default=str).encode()
    return Response(status=status, body=body, content_type="application/json; charset=utf-8")


def html_response(markup: str, status: int = 200) -> Response:
    return Response(status=status, body=markup.encode(), content_type="text/html; charset=utf-8")


def _no_store(response: Response) -> Response:
    """Console data is live. A cached view of a live system is a wrong view."""
    return Response(
        status=response.status,
        body=response.body,
        content_type=response.content_type,
        headers={**response.headers, "Cache-Control": "no-store"},
        stream=response.stream,
    )


class Router:
    """Path patterns to handlers. ``<name>`` segments become keyword arguments."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, ...], dict[str, Handler]] = {}

    def get(self, pattern: str, handler: Handler) -> None:
        self._add("GET", pattern, handler)

    def post(self, pattern: str, handler: Handler) -> None:
        self._add("POST", pattern, handler)

    def _add(self, method: str, pattern: str, handler: Handler) -> None:
        self._routes.setdefault(_segments(pattern), {})[method] = handler

    def dispatch(self, request: Request) -> Response:
        """Resolve and invoke, converting every failure into a status code."""
        wanted = _segments(request.path)
        for pattern, methods in self._routes.items():
            params = _match(pattern, wanted)
            if params is None:
                continue
            handler = methods.get(request.method)
            if handler is None:
                return json_response({"error": "method not allowed"}, status=405)
            if request.method in _WRITE_METHODS and request.header(CONSOLE_HEADER) is None:
                return json_response({"error": "console header required"}, status=403)
            return _no_store(_invoke(handler, request, params))
        return json_response({"error": "not found"}, status=404)


def _invoke(handler: Handler, request: Request, params: dict[str, str]) -> Response:
    """Contain handler failures.

    Deliberately broad: one broken panel must not end the demo, and the console
    can render an error card from a 500 far better than a dead socket.
    """
    try:
        return handler(request, **params)
    except Exception:
        return json_response({"error": "handler failed"}, status=500)


def _segments(path: str) -> tuple[str, ...]:
    return tuple(segment for segment in path.split("/") if segment)


def _match(pattern: tuple[str, ...], path: tuple[str, ...]) -> dict[str, str] | None:
    """Return captured parameters, or ``None`` when the shapes differ."""
    if len(pattern) != len(path):
        return None
    params: dict[str, str] = {}
    for expected, actual in zip(pattern, path, strict=True):
        if expected.startswith("<") and expected.endswith(">"):
            params[expected[1:-1]] = actual
        elif expected != actual:
            return None
    return params


# --- server-sent events ----------------------------------------------------


def event_stream(
    ledger: BroadcastLedger,
    *,
    replay: int = 0,
    stop: threading.Event | None = None,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> Generator[bytes, None, None]:
    """Yield SSE frames for one viewer until it disconnects.

    Unsubscribing in ``finally`` is load-bearing: a refreshed tab that left its
    subscriber attached would keep taking a copy of every event for the rest of
    the run, and enough refreshes would turn watching into a cost the run pays.
    """
    halt = stop if stop is not None else threading.Event()
    subscriber = ledger.subscribe(replay=replay)
    try:
        while not halt.is_set():
            missed = subscriber.take_dropped()
            if missed:
                yield _frame("gap", {"missed": missed})
                continue

            batch = subscriber.drain(timeout=heartbeat_seconds)
            if not batch:
                yield b": ping\n\n"
                continue

            for event in batch:
                yield b"event: audit\ndata: " + event.to_json().encode() + b"\n\n"
    finally:
        ledger.unsubscribe(subscriber)


def _frame(name: str, payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, separators=(",", ":"))
    return f"event: {name}\ndata: {body}\n\n".encode()


# --- the socket ------------------------------------------------------------


class ConsoleServer:
    """A threaded HTTP server bound to loopback."""

    def __init__(self, router: Router, *, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.router = router
        self.host = host
        self._requested_port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The bound port, which differs from the requested one when it was 0."""
        if self._httpd is None:
            return self._requested_port
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._httpd = ThreadingHTTPServer((self.host, self._requested_port), self._handler())
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @contextmanager
    def running(self) -> Iterator[ConsoleServer]:
        self.start()
        try:
            yield self
        finally:
            self.stop()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        router = self.router

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                self._respond("GET")

            def do_POST(self) -> None:
                self._respond("POST")

            def _respond(self, method: str) -> None:
                split = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                request = Request(
                    method=method,
                    path=split.path,
                    query=dict(parse_qsl(split.query)),
                    body=self.rfile.read(length) if length else b"",
                    headers={key: value for key, value in self.headers.items()},
                )
                response = router.dispatch(request)
                if response.stream is not None:
                    self._write_stream(response)
                else:
                    self._write_body(response)

            def _write_body(self, response: Response) -> None:
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(response.body)))
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(response.body)

            def _write_stream(self, response: Response) -> None:
                assert response.stream is not None
                self.send_response(response.status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                try:
                    for chunk in response.stream:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (TimeoutError, BrokenPipeError, ConnectionResetError):
                    pass  # the viewer closed the tab; the generator cleans up
                finally:
                    response.stream.close()

            def log_message(self, format: str, *args: Any) -> None:
                """Silence per-request stderr noise.

                The console is the log during a demo, and http.server's default
                writes straight to stderr rather than through anything the
                project controls.
                """

        return _Handler
