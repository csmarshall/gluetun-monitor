"""Real-apprise validation (#22) — the bar that lets apprise auto-merge.

Drives the REAL apprise library end to end against a localhost HTTP sink (no
network egress, no secrets): if an apprise update broke the parse/dispatch path we
depend on, this turns CI red. It needs no daemon, so it runs in the normal test job
(a required check) and auto-merge waits on it — mirroring how the real-daemon test
(#24) gates docker-py.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.notify import AppriseNotifier, NotifyEvent

pytest.importorskip("apprise")  # a declared dependency; importable in CI


def _log(stream: io.StringIO) -> Logger:
    return Logger(log_file=None, level="DEBUG", stream=stream)


@pytest.fixture
def sink() -> Iterator[tuple[int, list[bytes]]]:
    """A throwaway localhost HTTP server; yields (port, received_bodies)."""
    received: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            received.append(self.rfile.read(length))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            pass  # keep test output quiet

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, received
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_real_apprise_delivers_event_to_sink(sink: tuple[int, list[bytes]]) -> None:
    port, received = sink
    notifier = AppriseNotifier(
        (f"json://127.0.0.1:{port}",),
        min_level="INFO",
        throttle_seconds=0,
        logger=_log(io.StringIO()),
    )
    notifier.notify(NotifyEvent("ERROR", "title-XYZ", "body-ABC", "k"))

    assert received, "real apprise did not POST to the localhost sink"
    body = received[0]
    assert b"title-XYZ" in body
    assert b"body-ABC" in body


def test_real_apprise_test_method_delivers(sink: tuple[int, list[bytes]]) -> None:
    port, received = sink
    notifier = AppriseNotifier(
        (f"json://127.0.0.1:{port}",),
        min_level="WARN",
        throttle_seconds=0,
        logger=_log(io.StringIO()),
    )
    assert notifier.test() is True
    assert received


def test_real_apprise_rejects_bad_scheme_and_warns() -> None:
    stream = io.StringIO()
    notifier = AppriseNotifier(
        ("totally-not-a-scheme://nowhere",),
        min_level="INFO",
        throttle_seconds=0,
        logger=_log(stream),
    )
    # No server got registered, so a send reports no success (swallowed); the
    # rejection was warned at build time.
    notifier.notify(NotifyEvent("ERROR", "t", "b", "k"))
    assert "rejected by apprise" in stream.getvalue()
