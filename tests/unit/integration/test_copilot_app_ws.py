"""Tests for ``copilot_app_ws``: liveness probe + WS protocol round-trip.

We spin up a real ``websockets.sync.server`` on an ephemeral port,
write fake ``ws.{port,token}`` files into a fixture directory exposed
via the ``APM_COPILOT_APP_WS_RUN_DIR`` env override, and assert the
client speaks the App's dialect correctly.
"""

from __future__ import annotations

import json
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


@pytest.fixture
def run_dir(tmp_path: Path, monkeypatch) -> Path:
    rd = tmp_path / "run"
    rd.mkdir()
    monkeypatch.setenv(ws._RUN_DIR_ENV, str(rd))
    return rd


def _write_creds(run_dir: Path, port: int, token: str) -> None:
    (run_dir / "ws.port").write_text(str(port), encoding="ascii")
    (run_dir / "ws.token").write_text(token, encoding="ascii")


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


class TestCreateWorkflow:
    def test_round_trip_returns_id_and_omits_none_fields(self, run_dir: Path) -> None:
        received: dict = {}

        def handler(websocket):
            received["msg"] = json.loads(websocket.recv())
            websocket.send(json.dumps({"type": "workflow_created", "id": "wf-1"}))

        with _Server(handler) as srv:
            _write_creds(run_dir, srv.port, "tok")
            with ws.WsClient() as client:
                r = client.create_workflow(
                    name="hello",
                    prompt="say hi",
                    mode="plan",
                    project_id="proj-1",
                )
        assert r.workflow_id == "wf-1"
        msg = received["msg"]
        assert msg["type"] == "create_workflow"
        assert msg["name"] == "hello"
        assert msg["mode"] == "plan"
        assert msg["project_id"] == "proj-1"
        assert msg["enabled"] is False  # default

    def test_workflow_id_from_nested_workflow_object(self, run_dir: Path) -> None:
        def handler(websocket):
            websocket.recv()
            websocket.send(json.dumps({"type": "workflow_created", "workflow": {"id": "wf-2"}}))

        with _Server(handler) as srv:
            _write_creds(run_dir, srv.port, "tok")
            with ws.WsClient() as client:
                r = client.create_workflow(name="n", prompt="p")
        assert r.workflow_id == "wf-2"


class TestUpdateWorkflow:
    def test_only_set_fields_are_sent(self, run_dir: Path) -> None:
        received: dict = {}

        def handler(websocket):
            received["msg"] = json.loads(websocket.recv())
            websocket.send(json.dumps({"type": "workflow_updated", "id": "wf-3"}))

        with _Server(handler) as srv:
            _write_creds(run_dir, srv.port, "tok")
            with ws.WsClient() as client:
                client.update_workflow(workflow_id="wf-3", prompt="new")
        msg = received["msg"]
        assert msg["type"] == "update_workflow"
        assert msg["id"] == "wf-3"
        assert msg["prompt"] == "new"
        # Unset fields must NOT be on the wire (partial update).
        for key in ("name", "interval", "mode", "schedule_hour", "enabled"):
            assert key not in msg


class TestDrainAndInterleavedPush:
    def test_response_found_after_push_messages(self, run_dir: Path) -> None:
        def handler(websocket):
            websocket.recv()
            websocket.send(json.dumps({"type": "workflows_changed"}))
            websocket.send(json.dumps({"type": "github_auth_success"}))
            websocket.send(json.dumps({"type": "workflow_created", "id": "wf-late"}))

        with _Server(handler) as srv:
            _write_creds(run_dir, srv.port, "tok")
            with ws.WsClient() as client:
                r = client.create_workflow(name="n", prompt="p")
        assert r.workflow_id == "wf-late"


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
                    client.create_workflow(name="n", prompt="p")
