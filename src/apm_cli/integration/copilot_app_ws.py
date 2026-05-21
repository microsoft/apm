"""Localhost WebSocket IPC client for the GitHub Copilot desktop App.

The App binds a per-launch authenticated WebSocket on ``127.0.0.1:<port>``
and writes the port and token to ``~/.copilot/run/ws.{port,token}``
(mode 0o600). Any local process that can read those files can speak
the full ``WsClientMessage`` dialect. This module is the typed,
synchronous, scope-limited Python client APM uses to:

* register a project from a filesystem path (``create_project_from_path``),
* create or update an APM-managed workflow row attached to that
  project (``create_workflow`` / ``update_workflow``).

The WS path is the **preferred** integration surface when the App is
running: it goes through the App's own validation, fires the global
``WorkflowsChanged`` broadcast so the Workflows tab live-refreshes,
and avoids the white-screen failure mode where the webview cannot
resolve an externally-written ``project_id`` (see reverse-eng report).
When the App is closed the WS surface is unavailable and the caller
falls back to direct SQLite via ``copilot_app_project``.

Synchronous by design
---------------------
The install loop is fully synchronous; spinning up an asyncio loop
would force every primitive call site (``prompt_integrator``) into the
``asyncio.run`` ceremony with no benefit. We use
``websockets.sync.client.connect`` -- the official sync API shipped in
``websockets>=12`` -- which gives us blocking ``send``/``recv`` with
timeouts.

Security posture
----------------
We mirror the App's own webview behaviour:

* Always present ``Origin: tauri://localhost``. The server rejects
  any other origin (``websocket.rs:489-525``).
* Token is supplied via the ``?token=<base64>`` query string the
  server expects.
* TCP probe in ``ws_available`` is bounded to 100ms so install never
  hangs when the port file is stale (App crashed without cleanup).
* Parser is permissive: tolerate unknown / added top-level fields so
  upstream additions don't break us. We extract only what we use.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUN_DIR: str = ".copilot/run"
"""Directory under ``~/`` where the App publishes its port + token."""

_PORT_FILE: str = "ws.port"
_TOKEN_FILE: str = "ws.token"  # noqa: S105

_TCP_PROBE_TIMEOUT_S: float = 0.1
"""Liveness-probe budget: 100ms is plenty for a localhost connect and
keeps install responsive when the port file is stale (App crashed)."""

_WS_HANDSHAKE_TIMEOUT_S: float = 3.0
"""Maximum time to wait for the WS handshake to complete."""

_WS_RECV_TIMEOUT_S: float = 5.0
"""Default per-message recv timeout. Server replies arrive immediately
on localhost; this is a generous safety net against unexpected hangs."""

_ORIGIN_HEADER: str = "tauri://localhost"
"""The Origin value the App's WS server accepts (mirrors the bundled
webview's own Origin). Any other value is rejected at the gate."""

_USER_AGENT: str = "apm-cli/copilot-app-ws"
"""Recognisable UA string so the App's connection logs / telemetry can
attribute traffic to APM rather than the webview itself."""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WsError(Exception):
    """Base class for any WS-IPC failure.

    Callers in the integrator catch this to fall back to the direct-
    SQLite path; sub-types let UX code distinguish "App isn't running"
    (silent fallback, expected) from "App is running but rejected us"
    (warn-and-fall-back, surprising).
    """


class WsAppNotRunning(WsError):
    """Port file missing or TCP probe failed -- the App is closed.

    Triggers silent fallback to SQLite. Not a user-visible warning:
    closed App is the normal off-state.
    """


class WsAuthError(WsError):
    """Token rejected during handshake.

    Most common cause: stale ``~/.copilot/run/ws.token`` after an App
    restart. We warn-and-fall-back so install still succeeds.
    """


class WsProtocolError(WsError):
    """Server returned an error message or a malformed reply we cannot
    parse. Indicates either an upstream schema drift or a true server-
    side failure (e.g. ``create_project_from_path`` for a non-existent
    folder)."""


# ---------------------------------------------------------------------------
# Liveness probe
# ---------------------------------------------------------------------------


_RUN_DIR_ENV: str = "APM_COPILOT_APP_WS_RUN_DIR"
"""Environment override for the run-files directory. Tests and CI use
this to point at a fixture directory; production leaves it unset and
falls through to ``~/.copilot/run``."""


def _run_dir() -> Path:
    """Resolve the App's run-files directory.

    Honours ``APM_COPILOT_APP_WS_RUN_DIR`` (intended for tests / CI),
    otherwise falls back to ``~/.copilot/run``. Lazy so tests can
    monkeypatch ``Path.home`` or the env var per-test.
    """
    override = os.environ.get(_RUN_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / _RUN_DIR


def _read_creds() -> tuple[int, str] | None:
    """Return ``(port, token)`` from the App's run-files or ``None``.

    Both files must exist and be readable; either missing means the App
    is not running (or never has been since boot). We do not validate
    the token format here -- the server rejects invalid tokens at
    handshake.
    """
    run = _run_dir()
    port_path = run / _PORT_FILE
    token_path = run / _TOKEN_FILE
    if not port_path.is_file() or not token_path.is_file():
        return None
    try:
        port = int(port_path.read_text(encoding="ascii").strip())
        token = token_path.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return None
    if not (0 < port < 65536) or not token:
        return None
    return port, token


def ws_available() -> bool:
    """Quick liveness check: port file present AND TCP probe succeeds.

    Bounded to ``_TCP_PROBE_TIMEOUT_S`` so a stale port file (App
    crashed without cleanup) never costs the user more than 100ms on
    install. Returns ``False`` on any failure -- callers fall through
    to the SQLite path silently.
    """
    creds = _read_creds()
    if creds is None:
        return False
    port, _token = creds
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=_TCP_PROBE_TIMEOUT_S):
            return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Response value objects (permissive: extra fields ignored)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectCreated:
    """Outcome of ``create_project_from_path``.

    ``was_created`` is ``True`` only on a fresh INSERT. When the App
    already had a row for the given path (HIT) it returns the existing
    id and ``was_created`` is ``False`` -- drives the restart-once UX
    hint exactly like the SQLite fallback.
    """

    project_id: str
    was_created: bool
    main_repo_path: str


@dataclass(frozen=True)
class WorkflowCreated:
    """Outcome of ``create_workflow`` / ``update_workflow``.

    The App generates a UUIDv4 for new rows. APM's caller does not
    depend on the format -- the id is opaque and only used to address
    the row in subsequent ``update_workflow`` / delete calls.
    """

    workflow_id: str


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class WsClient:
    """Synchronous WS-IPC client.

    Use as a context manager so the underlying socket is closed even
    when an exception propagates::

        with WsClient() as client:
            project = client.create_project_from_path(repo_root)
            for wf in workflows:
                client.create_workflow(...)

    All public methods raise a ``WsError`` subtype on any failure; the
    caller in ``prompt_integrator._integrate_prompts_for_copilot_app``
    catches ``WsError`` and falls through to the SQLite path.
    """

    def __init__(self, *, recv_timeout_s: float = _WS_RECV_TIMEOUT_S) -> None:
        self._recv_timeout_s = recv_timeout_s
        self._conn: Any | None = None

    # -- lifecycle ------------------------------------------------------

    def __enter__(self) -> WsClient:
        self._connect()
        # Drain greeting messages (server_hello + a few housekeeping
        # broadcasts) so subsequent recv calls see the response to our
        # next send, not stale greetings.
        self._drain(timeout_s=0.5)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying socket. Idempotent."""
        import contextlib

        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None

    # -- connection -----------------------------------------------------

    def _connect(self) -> None:
        """Open the authenticated WS connection.

        Raises:
            WsAppNotRunning: port/token files missing or TCP refused.
            WsAuthError: server rejected the token at handshake.
            WsProtocolError: any other handshake failure.
        """
        # Lazy import: websockets is a hard dep but the import cost is
        # non-trivial and only matters when the App is running.
        try:
            from websockets.sync.client import connect
        except ImportError as exc:  # pragma: no cover - hard dep
            raise WsError(f"websockets library unavailable: {exc}") from exc

        creds = _read_creds()
        if creds is None:
            raise WsAppNotRunning("No ~/.copilot/run/ws.{port,token} files present")
        port, token = creds

        url = f"ws://127.0.0.1:{port}/?token={token}"
        try:
            self._conn = connect(
                url,
                additional_headers={
                    "Origin": _ORIGIN_HEADER,
                    "User-Agent": _USER_AGENT,
                },
                open_timeout=_WS_HANDSHAKE_TIMEOUT_S,
                max_size=2**24,
            )
        except Exception as exc:
            msg = str(exc)
            # ``websockets`` raises InvalidStatus / InvalidHandshake for
            # HTTP-level rejections. 401/403 -> auth; everything else ->
            # generic protocol error. We string-match defensively so a
            # library-version bump doesn't break the branch.
            if "401" in msg or "403" in msg or "unauthor" in msg.lower():
                raise WsAuthError(f"WS auth rejected: {exc}") from exc
            if "refused" in msg.lower() or "ConnectionRefused" in msg:
                raise WsAppNotRunning(f"WS connection refused: {exc}") from exc
            raise WsProtocolError(f"WS handshake failed: {exc}") from exc

    # -- low-level send/recv -------------------------------------------

    def _send(self, payload: dict[str, Any]) -> None:
        """Serialize *payload* as JSON and send. Raises ``WsError`` on failure."""
        if self._conn is None:
            raise WsError("WsClient is not connected; use as a context manager")
        try:
            self._conn.send(json.dumps(payload))
        except Exception as exc:
            raise WsProtocolError(f"WS send failed: {exc}") from exc

    def _recv(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        """Receive one JSON message. Raises ``WsProtocolError`` on parse failure."""
        if self._conn is None:
            raise WsError("WsClient is not connected; use as a context manager")
        budget = timeout_s if timeout_s is not None else self._recv_timeout_s
        try:
            raw = self._conn.recv(timeout=budget)
        except TimeoutError as exc:
            raise WsProtocolError(f"WS recv timed out after {budget:.1f}s") from exc
        except Exception as exc:
            raise WsProtocolError(f"WS recv failed: {exc}") from exc
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise WsProtocolError(f"WS reply was not valid JSON: {raw[:120]!r}") from exc
        if not isinstance(data, dict):
            raise WsProtocolError(f"WS reply was not a JSON object: {data!r}")
        return data

    def _drain(self, *, timeout_s: float) -> list[dict[str, Any]]:
        """Drain pending server-pushed messages for up to *timeout_s*.

        Used at handshake to flush the greeting batch. Per-message
        timeout is short so we exit promptly when the server goes
        quiet; total wall-clock budget is bounded by *timeout_s*.
        """
        out: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                out.append(self._recv(timeout_s=min(0.2, remaining)))
            except WsProtocolError:
                # Timeout / no more messages -- normal end-of-drain.
                break
        return out

    def _await_typed_reply(
        self,
        *,
        expected: set[str],
        max_messages: int = 8,
    ) -> dict[str, Any]:
        """Drain server messages until one's ``type`` matches *expected*.

        The server interleaves push-broadcasts (``workflows_changed``,
        ``keep_awake_changed``, ``github_auth_success``) with the
        response to our request, so we walk the inbox up to
        *max_messages* deep. ``error`` short-circuits with
        ``WsProtocolError``.
        """
        for _ in range(max_messages):
            msg = self._recv()
            t = msg.get("type")
            if t == "error":
                raise WsProtocolError(f"WS server returned error: {msg.get('message') or msg!r}")
            if t in expected:
                return msg
        raise WsProtocolError(
            f"WS server did not return any of {sorted(expected)} within {max_messages} messages"
        )

    # -- public API -----------------------------------------------------

    def create_project_from_path(self, path: Path) -> ProjectCreated:
        """Register *path* as a project via the App's own create flow.

        The App's handler runs full discovery: folder probe, GitHub
        owner/repo detection, default-branch resolution, account
        binding. We get back the resolved ``project_id``.

        ``was_created`` is best-effort: the server's
        ``project_created`` reply carries a ``project`` payload but
        not (currently) a "was new" flag, so we treat any successful
        reply as "created or already existed -- caller should ALWAYS
        emit the restart hint until upstream issue github/github-app#5483
        lands the live broadcast". The hint is suppressed once the
        webview learns about the row (i.e. after the user restarts).
        """
        self._send(
            {
                "type": "create_project_from_path",
                "path": str(path),
            }
        )
        reply = self._await_typed_reply(
            expected={"project_created", "project_updated"},
        )
        project_id, main_repo_path, was_created = _extract_project_fields(
            reply, fallback_path=str(path)
        )
        if not project_id:
            raise WsProtocolError(f"project_created reply missing id: {reply!r}")
        return ProjectCreated(
            project_id=project_id,
            was_created=was_created,
            main_repo_path=main_repo_path or str(path),
        )

    def create_workflow(
        self,
        *,
        name: str,
        prompt: str,
        interval: str = "manual",
        mode: str | None = None,
        schedule_hour: int = 9,
        schedule_day: int = 1,
        project_id: str | None = None,
        enabled: bool = False,
    ) -> WorkflowCreated:
        """Create a workflow row through the App.

        The App generates the workflow's id (UUIDv4). ``enabled``
        defaults to ``False`` to match the install contract: APM never
        flips the schedule on; the user opts in from the App UI.
        """
        payload: dict[str, Any] = {
            "type": "create_workflow",
            "name": name,
            "prompt": prompt,
            "interval": interval,
            "schedule_hour": schedule_hour,
            "schedule_day": schedule_day,
            "enabled": enabled,
        }
        if mode is not None:
            payload["mode"] = mode
        if project_id is not None:
            payload["project_id"] = project_id
        self._send(payload)
        reply = self._await_typed_reply(expected={"workflow_created"})
        wid = _extract_workflow_id(reply)
        if not wid:
            raise WsProtocolError(f"workflow_created reply missing id: {reply!r}")
        return WorkflowCreated(workflow_id=wid)

    def update_workflow(
        self,
        *,
        workflow_id: str,
        name: str | None = None,
        prompt: str | None = None,
        interval: str | None = None,
        mode: str | None = None,
        schedule_hour: int | None = None,
        schedule_day: int | None = None,
        project_id: str | None = None,
        enabled: bool | None = None,
    ) -> WorkflowCreated:
        """Update an existing workflow row in place.

        Only fields explicitly passed are sent on the wire so the
        server's partial-update semantics are preserved (unsent fields
        keep their existing value).
        """
        payload: dict[str, Any] = {
            "type": "update_workflow",
            "id": workflow_id,
        }
        for key, value in (
            ("name", name),
            ("prompt", prompt),
            ("interval", interval),
            ("mode", mode),
            ("schedule_hour", schedule_hour),
            ("schedule_day", schedule_day),
            ("project_id", project_id),
            ("enabled", enabled),
        ):
            if value is not None:
                payload[key] = value
        self._send(payload)
        reply = self._await_typed_reply(
            expected={"workflow_updated", "workflow_created"},
        )
        wid = _extract_workflow_id(reply) or workflow_id
        return WorkflowCreated(workflow_id=wid)


# ---------------------------------------------------------------------------
# Permissive parsers
# ---------------------------------------------------------------------------


def _extract_workflow_id(reply: dict[str, Any]) -> str | None:
    """Pull the workflow id out of any of the shapes the server emits."""
    for key in ("id", "workflow_id"):
        v = reply.get(key)
        if isinstance(v, str) and v:
            return v
    wf = reply.get("workflow")
    if isinstance(wf, dict):
        v = wf.get("id")
        if isinstance(v, str) and v:
            return v
    return None


def _extract_project_fields(
    reply: dict[str, Any],
    *,
    fallback_path: str,
) -> tuple[str | None, str | None, bool]:
    """Return ``(project_id, main_repo_path, was_created)`` from a project reply.

    Tolerates both ``{project: {...}}`` nested shape and top-level
    fields. ``was_created`` is True when the server signalled
    ``project_created`` and False when it signalled
    ``project_updated`` (a HIT on existing main_repo_path).
    """
    msg_type = reply.get("type", "")
    was_created = msg_type == "project_created"
    project_id: str | None = None
    main_repo_path: str | None = None
    for key in ("project_id", "id"):
        v = reply.get(key)
        if isinstance(v, str) and v:
            project_id = v
            break
    proj = reply.get("project")
    if isinstance(proj, dict):
        if not project_id:
            v = proj.get("id")
            if isinstance(v, str) and v:
                project_id = v
        for path_key in ("main_repo_path", "path"):
            pv = proj.get(path_key)
            if isinstance(pv, str) and pv:
                main_repo_path = pv
                break
    if main_repo_path is None:
        for path_key in ("main_repo_path", "path"):
            pv = reply.get(path_key)
            if isinstance(pv, str) and pv:
                main_repo_path = pv
                break
    return project_id, main_repo_path or fallback_path, was_created
