"""Tests for ``copilot_app_ws``: liveness probe + WS protocol round-trip (pure logic cases)."""

from __future__ import annotations

import socket
import sys
import threading
import time
from collections.abc import Callable

import pytest
from websockets.sync.server import serve as _serve

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Server harness
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    """A throw-away ``websockets.sync`` server bound to localhost."""

    def __init__(self, handler: Callable, *, expect_token: str | None = None):
        self.port = _free_port()
        self.expect_token = expect_token
        self._handler = handler
        self._server = None
        self._thread: threading.Thread | None = None
        self.connections: list[dict] = []

    def _process_request(self, connection, request):
        # Token gate via query-string (mirror the App's posture).
        if self.expect_token is not None:
            path = getattr(request, "path", "/")
            if f"token={self.expect_token}" not in path:
                from websockets.datastructures import Headers
                from websockets.http11 import Response

                return Response(401, "Unauthorized", Headers(), b"bad token")
        # Origin gate: must be tauri://localhost.
        headers = getattr(request, "headers", {})
        origin = headers.get("Origin") if hasattr(headers, "get") else None
        self.connections.append({"origin": origin, "path": getattr(request, "path", "/")})
        return None

    def _wrapped(self, websocket):
        import contextlib

        with contextlib.suppress(Exception):
            self._handler(websocket)

    def _run_serve_forever(self) -> None:
        """Run the server's accept loop, absorbing only the known
        Windows shutdown race.

        ``websockets.sync.server.Server.shutdown()`` only registers a
        POSIX self-pipe watcher (guarded by ``sys.platform != "win32"``
        upstream); on Windows it just closes the listening socket, so
        if the accept loop's background thread is blocked inside
        ``selectors``/``select.select()`` at that exact moment, Windows
        raises ``OSError: [WinError 10038]`` (an operation was
        attempted on something that is not a socket) instead of the
        loop observing a clean, POSIX-style closed-socket return. That
        is the *only* condition this catches -- any other exception
        (including other ``OSError`` subtypes) propagates so a real
        server failure is never silently swallowed (see
        microsoft/apm#2233).
        """
        try:
            self._server.serve_forever()
        except OSError as error:
            if sys.platform == "win32" and getattr(error, "winerror", None) == 10038:
                return
            raise

    def __enter__(self):
        self._server = _serve(
            self._wrapped,
            "127.0.0.1",
            self.port,
            process_request=self._process_request,
        )
        self._thread = threading.Thread(target=self._run_serve_forever, daemon=True)
        self._thread.start()
        # Tiny sleep so accept loop is ready when the client connects.
        time.sleep(0.05)
        return self

    def __exit__(self, *_):
        import contextlib

        with contextlib.suppress(Exception):
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


@pytest.mark.windows_compat
def test_server_context_joins_serve_forever_thread() -> None:
    def handler(websocket):
        websocket.recv()

    with _Server(handler) as srv:
        thread = srv._thread
        assert thread is not None

    assert not thread.is_alive()


class _FakeSyncServer:
    """Stand-in for ``websockets.sync.server.Server`` that raises a
    caller-supplied error from ``serve_forever`` without opening a
    socket."""

    def __init__(self, error: BaseException | None) -> None:
        self._error = error

    def serve_forever(self) -> None:
        if self._error is not None:
            raise self._error


def _bare_server() -> _Server:
    """Build a ``_Server`` instance without running ``__init__`` (which
    binds a real socket) so ``_run_serve_forever`` can be exercised in
    isolation."""
    return _Server.__new__(_Server)


@pytest.mark.windows_compat
def test_run_serve_forever_absorbs_windows_shutdown_race(monkeypatch) -> None:
    """The Windows-only shutdown race (WinError 10038) must be
    swallowed -- reproduced here by manually tagging a plain ``OSError``
    with ``winerror``, since CPython only populates that attribute on
    real Windows builds (see microsoft/apm#2233)."""
    monkeypatch.setattr(sys, "platform", "win32")
    error = OSError("An operation was attempted on something that is not a socket")
    error.winerror = 10038
    srv = _bare_server()
    srv._server = _FakeSyncServer(error)

    srv._run_serve_forever()  # must not raise


@pytest.mark.windows_compat
def test_run_serve_forever_reraises_other_winerrors(monkeypatch) -> None:
    """Only WinError 10038 is a proven, expected shutdown race; any
    other Windows OSError must propagate rather than being silently
    swallowed."""
    monkeypatch.setattr(sys, "platform", "win32")
    error = OSError("some other winsock failure")
    error.winerror = 10054
    srv = _bare_server()
    srv._server = _FakeSyncServer(error)

    with pytest.raises(OSError, match="some other winsock failure"):
        srv._run_serve_forever()


@pytest.mark.windows_compat
def test_run_serve_forever_reraises_on_non_windows(monkeypatch) -> None:
    """The narrow catch is Windows-only: on POSIX platforms even a
    winerror-tagged OSError must not be swallowed, since the real
    upstream shutdown race never manifests there."""
    monkeypatch.setattr(sys, "platform", "linux")
    error = OSError("would not occur on posix")
    error.winerror = 10038
    srv = _bare_server()
    srv._server = _FakeSyncServer(error)

    with pytest.raises(OSError, match="would not occur on posix"):
        srv._run_serve_forever()


@pytest.mark.windows_compat
def test_run_serve_forever_propagates_normal_completion(monkeypatch) -> None:
    """A clean ``serve_forever`` return (the POSIX-shutdown happy path)
    must not be affected by the wrapper."""
    monkeypatch.setattr(sys, "platform", "linux")
    srv = _bare_server()
    srv._server = _FakeSyncServer(None)

    srv._run_serve_forever()  # must not raise


# ---------------------------------------------------------------------------
# Liveness probe
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Message round-trips
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Security hardenings (token scrub + file mode)
# ---------------------------------------------------------------------------
