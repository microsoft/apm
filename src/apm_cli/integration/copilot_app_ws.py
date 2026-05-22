"""Localhost WebSocket IPC client for the GitHub Copilot desktop App.

The App binds a per-launch authenticated WebSocket on ``127.0.0.1:<port>``
and writes the port and token to ``~/.copilot/run/ws.{port,token}``
(mode 0o600 -- we re-check the token file's mode at read time and
refuse to use a group/other-readable token). Any local process that
can read those files can speak the full ``WsClientMessage`` dialect.
This module is the typed, synchronous, scope-limited Python client APM
uses to register a project from a filesystem path
(``create_project_from_path``).

Scope: project registration only.

Workflow rows are written via direct SQLite in
``copilot_app_db.deploy_workflow`` regardless of which path created the
project. This keeps lockfile ids stable (namespaced
``owner/pkg/stem``) instead of opaque server-side UUIDs, and removes
the need for paired ``create_workflow`` / ``update_workflow`` IPC. The
WS surface still earns its keep on the project side: the App runs full
discovery (owner/repo detection, default branch, account binding) and
the resulting project row is the one the webview already knows about,
which is how we avoid the white-screen failure mode where an
externally-written ``project_id`` doesn't resolve. When the App is
closed the WS surface is unavailable and the caller falls back to
direct SQLite project registration via ``copilot_app_project``.

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
import re
import socket
import stat
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


_TOKEN_QUERY_RE = re.compile(r"(\?|&)token=[^&\s\"'>]+")


def _scrub_token(text: str) -> str:
    """Redact any ``?token=...`` / ``&token=...`` query material from *text*.

    Used before wrapping ``websockets`` library exceptions into our
    ``WsError`` subtypes: those exception messages frequently echo back
    the full handshake URL, which embeds the per-launch token. The
    token is short-lived but still credential material, so we keep it
    out of diagnostics, log lines, and user-visible warnings.
    """
    return _TOKEN_QUERY_RE.sub(r"\1token=<redacted>", text)


def _token_file_mode_ok(path: Path) -> bool:
    """Return True iff the token file has no group/other permissions.

    The App writes the file as 0o600. If a later actor (user, backup
    tool, sync agent) widens the mode, anything readable by group or
    other can lift the token and impersonate APM against the App's WS
    server for as long as the App stays open. We treat any
    group/other bit as a refusal-to-read rather than warning-and-
    continuing -- the SQLite fallback is always available, so the
    cost of being strict is one extra restart, while the cost of being
    permissive is a real credential leak.

    POSIX-only check. On Windows ``os.stat`` synthesizes group/other
    bits from the read-only flag (typically ``0o100666`` for r/w
    files), so the POSIX-style ``& S_IRWXG | S_IRWXO`` test would
    spuriously reject every token file. We short-circuit to ``True``
    on Windows; ACL hardening on Windows is out of scope for this
    module.
    """
    if os.name == "nt":
        return True
    try:
        st = path.stat()
    except OSError:
        return False
    mode = st.st_mode
    return not (mode & (stat.S_IRWXG | stat.S_IRWXO))


def _read_creds() -> tuple[int, str] | None:
    """Return ``(port, token)`` from the App's run-files or ``None``.

    Both files must exist and be readable; either missing means the App
    is not running (or never has been since boot). The token file's
    mode is verified to match the App's documented 0o600 posture --
    if widened to group/other-readable we refuse rather than risk
    sending credential material extracted from a non-private file.
    We do not validate the token format itself; the server rejects
    invalid tokens at handshake.
    """
    run = _run_dir()
    port_path = run / _PORT_FILE
    token_path = run / _TOKEN_FILE
    if not port_path.is_file() or not token_path.is_file():
        return None
    if not _token_file_mode_ok(token_path):
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


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class WsClient:
    """Synchronous WS-IPC client.

    Use as a context manager so the underlying socket is closed even
    when an exception propagates::

        with WsClient() as client:
            project = client.create_project_from_path(repo_root)

    The client exposes only ``create_project_from_path``: workflow
    rows are written via direct SQLite (``copilot_app_db``) regardless
    of which path created the project. This keeps lockfile ids
    namespaced and stable (see module docstring).

    All public methods raise a ``WsError`` subtype on any failure; the
    caller in ``prompt_integrator._integrate_prompts_for_copilot_app``
    catches ``WsError`` and falls through to the SQLite project
    registration path.
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
            # Scrub the handshake URL's ``?token=<base64>`` before it
            # lands in our exception text -- the ``websockets`` library
            # echoes the full URL back in several of its InvalidStatus
            # / InvalidHandshake messages.
            raw = str(exc)
            msg = _scrub_token(raw)
            scrubbed_lower = msg.lower()
            # ``websockets`` raises InvalidStatus / InvalidHandshake for
            # HTTP-level rejections. 401/403 -> auth; everything else ->
            # generic protocol error. We string-match defensively so a
            # library-version bump doesn't break the branch.
            if "401" in msg or "403" in msg or "unauthor" in scrubbed_lower:
                raise WsAuthError(f"WS auth rejected: {msg}") from None
            if "refused" in scrubbed_lower or "connectionrefused" in scrubbed_lower:
                raise WsAppNotRunning(f"WS connection refused: {msg}") from None
            raise WsProtocolError(f"WS handshake failed: {msg}") from None

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

        ``was_created`` is inferred from the server's reply type:
        ``project_created`` -> ``was_created=True`` (new ``projects`` row),
        ``project_updated`` -> ``was_created=False`` (HIT on existing
        ``main_repo_path``). The restart hint downstream only fires on
        ``was_created=True``; on ``project_updated`` the webview already
        knows the row and no restart is needed. The "was new" flag rides
        on the message ``type`` rather than a dedicated field -- see
        ``_extract_project_fields`` for the parser.

        Upstream issue github/github-app#5483 tracks adding a live
        broadcast so externally-inserted rows surface in the webview
        without a restart; until that lands, the hint is the user-visible
        signal that a manual restart wires the new project into the UI.
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


# ---------------------------------------------------------------------------
# Permissive parsers
# ---------------------------------------------------------------------------


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
