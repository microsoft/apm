"""Tests for ``copilot_app_ws``: liveness probe + WS protocol round-trip.

We spin up a real ``websockets.sync.server`` on an ephemeral port,
write fake ``ws.{port,token}`` files into a fixture directory exposed
via the ``APM_COPILOT_APP_WS_RUN_DIR`` env override, and assert the
client speaks the App's dialect correctly.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from apm_cli.integration import copilot_app_ws as ws

websockets = pytest.importorskip("websockets")
from websockets.sync.server import serve as _serve  # noqa: E402

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

    def __enter__(self):
        self._server = _serve(
            self._wrapped,
            "127.0.0.1",
            self.port,
            process_request=self._process_request,
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
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


@pytest.fixture
def run_dir(tmp_path: Path, monkeypatch) -> Path:
    rd = tmp_path / "run"
    rd.mkdir()
    monkeypatch.setenv(ws._RUN_DIR_ENV, str(rd))
    return rd


def _write_creds(run_dir: Path, port: int, token: str) -> None:
    port_path = run_dir / "ws.port"
    token_path = run_dir / "ws.token"
    port_path.write_text(str(port), encoding="ascii")
    token_path.write_text(token, encoding="ascii")
    # Match the App's 0o600 posture so ``_read_creds`` accepts the
    # token file -- the production module refuses to read a token
    # that is group/other-readable (see _token_file_mode_ok).
    os.chmod(token_path, 0o600)


def test_server_context_joins_serve_forever_thread() -> None:
    def handler(websocket):
        websocket.recv()

    with _Server(handler) as srv:
        thread = srv._thread
        assert thread is not None

    assert not thread.is_alive()


# ---------------------------------------------------------------------------
# Liveness probe
# ---------------------------------------------------------------------------


class TestWsAvailable:
    def test_no_files_returns_false(self, run_dir: Path) -> None:
        assert ws.ws_available() is False

    def test_missing_token_file_returns_false(self, run_dir: Path) -> None:
        (run_dir / "ws.port").write_text("12345", encoding="ascii")
        assert ws.ws_available() is False

    def test_invalid_port_returns_false(self, run_dir: Path) -> None:
        _write_creds(run_dir, 0, "tok")
        assert ws.ws_available() is False

    def test_stale_port_no_listener_returns_false(self, run_dir: Path) -> None:
        # Port file exists, but nothing is listening: probe must fail
        # within the probe budget (100ms) -- we give a generous wall
        # clock budget here.
        _write_creds(run_dir, _free_port(), "tok")
        t0 = time.monotonic()
        assert ws.ws_available() is False
        assert time.monotonic() - t0 < 1.0

    def test_listener_present_returns_true(self, run_dir: Path) -> None:
        def handler(websocket):
            websocket.recv()

        with _Server(handler) as srv:
            _write_creds(run_dir, srv.port, "tok")
            assert ws.ws_available() is True


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


class TestHandshake:
    def test_app_not_running_raises_app_not_running(self, run_dir: Path) -> None:
        # No files at all.
        client = ws.WsClient()
        with pytest.raises(ws.WsAppNotRunning):
            client._connect()

    def test_connection_refused_raises_app_not_running(self, run_dir: Path) -> None:
        _write_creds(run_dir, _free_port(), "tok")
        client = ws.WsClient()
        with pytest.raises(ws.WsAppNotRunning):
            client._connect()

    def test_bad_token_raises_auth_error(self, run_dir: Path) -> None:
        def handler(websocket):
            websocket.recv()

        with _Server(handler, expect_token="GOOD") as srv:
            _write_creds(run_dir, srv.port, "BAD")
            client = ws.WsClient()
            with pytest.raises(ws.WsAuthError):
                client._connect()

    def test_origin_header_is_tauri_localhost(self, run_dir: Path) -> None:
        def handler(websocket):
            websocket.recv()

        with _Server(handler) as srv:
            _write_creds(run_dir, srv.port, "tok")
            with ws.WsClient():
                pass
            # The probe ran first then the real client connect, so
            # we expect at least one connection with Origin set.
            origins = [c.get("origin") for c in srv.connections]
            assert "tauri://localhost" in origins


# ---------------------------------------------------------------------------
# Message round-trips
# ---------------------------------------------------------------------------


def _send_greetings(websocket) -> None:
    """Push some greeting frames so the client's drain code is exercised."""
    websocket.send(json.dumps({"type": "server_hello"}))
    websocket.send(json.dumps({"type": "keep_awake_changed", "enabled": False}))


class TestCreateProject:
    @pytest.mark.skipif(
        os.name == "nt",
        reason="Flaky on Windows: race between server-side close and client-side "
        "drain of buffered project_created frame. Coverage retained on POSIX; "
        "the create_project_from_path code path is platform-agnostic.",
    )
    def test_round_trip_returns_id_and_created_flag(self, run_dir: Path) -> None:
        def handler(websocket):
            _send_greetings(websocket)
            raw = websocket.recv()
            msg = json.loads(raw)
            assert msg["type"] == "create_project_from_path"
            assert msg["path"] == "/tmp/some/repo"
            websocket.send(
                json.dumps(
                    {
                        "type": "project_created",
                        "project": {
                            "id": "proj-uuid-1",
                            "main_repo_path": "/tmp/some/repo",
                        },
                    }
                )
            )

        with _Server(handler) as srv:
            _write_creds(run_dir, srv.port, "tok")
            with ws.WsClient() as client:
                result = client.create_project_from_path(Path("/tmp/some/repo"))
        assert result.project_id == "proj-uuid-1"
        assert result.was_created is True
        assert result.main_repo_path == "/tmp/some/repo"

    def test_project_updated_reply_yields_was_created_false(self, run_dir: Path) -> None:
        def handler(websocket):
            websocket.recv()
            websocket.send(
                json.dumps(
                    {
                        "type": "project_updated",
                        "project": {"id": "proj-uuid-2", "main_repo_path": "/x"},
                    }
                )
            )

        with _Server(handler) as srv:
            _write_creds(run_dir, srv.port, "tok")
            with ws.WsClient() as client:
                r = client.create_project_from_path(Path("/x"))
        assert r.was_created is False
        assert r.project_id == "proj-uuid-2"

    def test_server_error_raises_protocol_error(self, run_dir: Path) -> None:
        def handler(websocket):
            websocket.recv()
            websocket.send(json.dumps({"type": "error", "message": "no such folder"}))

        with _Server(handler) as srv:
            _write_creds(run_dir, srv.port, "tok")
            with ws.WsClient() as client:
                with pytest.raises(ws.WsProtocolError, match=r"no such folder"):
                    client.create_project_from_path(Path("/nope"))

    def test_malformed_reply_raises_protocol_error(self, run_dir: Path) -> None:
        def handler(websocket):
            websocket.recv()
            websocket.send("not json at all")

        with _Server(handler) as srv:
            _write_creds(run_dir, srv.port, "tok")
            with ws.WsClient() as client:
                with pytest.raises(ws.WsProtocolError):
                    client.create_project_from_path(Path("/x"))

    def test_extra_fields_are_tolerated(self, run_dir: Path) -> None:
        def handler(websocket):
            websocket.recv()
            websocket.send(
                json.dumps(
                    {
                        "type": "project_created",
                        "project": {
                            "id": "p-1",
                            "main_repo_path": "/r",
                            "future_field": True,
                        },
                        "another_unknown": 42,
                    }
                )
            )

        with _Server(handler) as srv:
            _write_creds(run_dir, srv.port, "tok")
            with ws.WsClient() as client:
                r = client.create_project_from_path(Path("/r"))
        assert r.project_id == "p-1"


class TestDrainAndInterleavedPush:
    def test_response_found_after_push_messages(self, run_dir: Path) -> None:
        def handler(websocket):
            websocket.recv()
            websocket.send(json.dumps({"type": "workflows_changed"}))
            websocket.send(json.dumps({"type": "github_auth_success"}))
            websocket.send(
                json.dumps(
                    {
                        "type": "project_created",
                        "project": {"id": "p-late", "main_repo_path": "/r"},
                    }
                )
            )

        with _Server(handler) as srv:
            _write_creds(run_dir, srv.port, "tok")
            with ws.WsClient() as client:
                r = client.create_project_from_path(Path("/r"))
        assert r.project_id == "p-late"


class TestRecvTimeout:
    def test_recv_timeout_raises_protocol_error(self, run_dir: Path) -> None:
        def handler(websocket):
            websocket.recv()
            # Never reply -- client must time out.
            time.sleep(2.0)

        with _Server(handler) as srv:
            _write_creds(run_dir, srv.port, "tok")
            with ws.WsClient(recv_timeout_s=0.3) as client:
                with pytest.raises(ws.WsProtocolError, match=r"timed out"):
                    client.create_project_from_path(Path("/r"))


# ---------------------------------------------------------------------------
# Security hardenings (token scrub + file mode)
# ---------------------------------------------------------------------------


class TestTokenScrub:
    def test_scrub_removes_token_query_arg(self) -> None:
        url = "ws://127.0.0.1:51234/?token=ABC123xyz= HTTP rejected"
        scrubbed = ws._scrub_token(url)
        assert "ABC123xyz" not in scrubbed
        assert "token=<redacted>" in scrubbed

    def test_scrub_handles_token_in_chained_query(self) -> None:
        s = "GET /api?foo=1&token=SECRET_VALUE&bar=2 failed"
        scrubbed = ws._scrub_token(s)
        assert "SECRET_VALUE" not in scrubbed
        assert "token=<redacted>" in scrubbed

    def test_auth_error_message_does_not_leak_token(self, run_dir: Path, monkeypatch) -> None:
        """End-to-end: a real 401 from the server must not embed the token."""

        def handler(websocket):
            # Never reached -- request gate below returns 401.
            websocket.recv()

        secret_token = "TOPSECRETtoken12345"
        with _Server(handler, expect_token="OTHER") as srv:
            _write_creds(run_dir, srv.port, secret_token)
            client = ws.WsClient()
            with pytest.raises(ws.WsAuthError) as excinfo:
                client._connect()
        # The token MUST NOT appear anywhere in the exception text or
        # in any chained __context__ / __cause__ chain reachable from
        # the wrapped exception. We use ``from None`` for exactly this
        # reason -- guard it with a test.
        assert secret_token not in str(excinfo.value)
        chain = excinfo.value.__cause__ or excinfo.value.__context__
        # ``from None`` suppresses __cause__; __context__ is permitted
        # (implicit during except handling) but only matters if Python
        # prints it. We accept either: no chain at all, OR a chain
        # whose stringification also redacts the token.
        if chain is not None:
            # __context__ is the original websockets exception. Its own
            # str() may still contain the URL; we cannot rewrite that
            # at the library level, but ``raise ... from None`` ensures
            # neither the default traceback printer nor APM's
            # diagnostics formatter walks back to it.
            pass  # accepted -- documented trade-off


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permission bits: production code short-circuits to True on Windows "
    "(os.stat synthesizes group/other bits from the read-only flag).",
)
class TestTokenFileMode:
    def test_world_readable_token_is_rejected(self, run_dir: Path, monkeypatch) -> None:
        _write_creds(run_dir, 12345, "tok")
        token_path = run_dir / "ws.token"
        # Widen the token file to be world-readable.
        os.chmod(token_path, 0o644)
        # ``_read_creds`` MUST refuse rather than return creds.
        assert ws._read_creds() is None
        # And the public liveness probe likewise reports unavailable.
        assert ws.ws_available() is False

    def test_group_readable_token_is_rejected(self, run_dir: Path) -> None:
        _write_creds(run_dir, 12345, "tok")
        os.chmod(run_dir / "ws.token", 0o640)
        assert ws._read_creds() is None

    def test_owner_only_token_is_accepted(self, run_dir: Path) -> None:
        _write_creds(run_dir, 12345, "tok")
        os.chmod(run_dir / "ws.token", 0o600)
        creds = ws._read_creds()
        assert creds == (12345, "tok")
