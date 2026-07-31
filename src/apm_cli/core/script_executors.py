"""Lifecycle script executors -- one per action type.

Each executor isolates failures: it catches all exceptions internally
and logs failures in verbose mode only (using ``[i]`` ASCII symbol).
``http`` scripts dispatch in a background daemon thread; ``command``
scripts run synchronously and can delay the operation up to their timeout.

Two script types (Copilot CLI aligned):

- ``command`` -- shell command via subprocess, event JSON on **stdin**
- ``http``    -- HTTPS POST with JSON body, env-var expansion in headers

Script output is appended to ``~/.apm/logs/scripts.log`` (with known
credential values redacted) so administrators can audit what scripts
produce without enabling verbose CLI output.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import signal
import socket  # noqa: F401 -- test seam: tests patch se.socket.getaddrinfo
import subprocess
import threading
import time
from typing import TYPE_CHECKING

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

# Re-export every symbol from the helpers so ``apm_cli.core.script_executors.*``
# remains the canonical import path (existing test seams unchanged).
from ._script_http_transport import (  # noqa: F401
    _CAPTURING_SESSION,
    _CAPTURING_SESSION_LOCK,
    _CGNAT_NET,
    _DISPATCH_SOCK,
    _GUARDED_SESSION,
    _GUARDED_SESSION_LOCK,
    _HTTP_ABANDON_GRACE,
    _HTTP_CONNECT_TIMEOUT,
    _HTTP_INFLIGHT,
    _MAX_HTTP_TIMEOUT,
    _METADATA_HOSTNAMES,
    _SITE_LOCAL_NET,
    MAX_HTTP_DISPATCH_THREADS,
    _abort_dispatch_sock,
    _build_capturing_session,
    _build_guarded_session,
    _coerce_http_deadline,
    _connect_layer_host,
    _dispatch_http_request,
    _environ_proxies_for,
    _get_capturing_session,
    _get_guarded_session,
    _host_to_ip_literal,
    _ip_is_internal,
    _normalize_host,
    _prepare_http,
    _record_dispatch_sock,
    _safe_urlparse,
    _SockRecordingMixin,
    _ssrf_block_reason,
    _ssrf_safe_connect,
    _SSRFConnectError,
)
from ._script_secret_helpers import (  # noqa: F401
    _CAPTURE_DRAIN_GRACE,
    _CREDENTIAL_BLOB_NAMES,
    _CREDENTIAL_BLOB_SUFFIX,
    _CREDENTIAL_DENYLIST,
    _CREDENTIAL_NAME_PREFIX,
    _DEFAULT_COMMAND_TIMEOUT,
    _DEFAULT_HTTP_TIMEOUT,
    _DENYLIST_EXEMPT,
    _ENV_VAR_PATTERN,
    _LINE_BREAK_ESCAPES,
    _LINE_BREAK_PATTERN,
    _MAX_CAPTURE_CHARS,
    _MAX_LOG_BYTES,
    _MAX_LOG_FIELD_CHARS,
    _MIN_REDACT_LEN,
    _POSIX_PERMS,
    _SLOW_SCRIPT_THRESHOLD_SEC,
    _append_to_script_log,
    _escape_header_field,
    _expand_env_vars,
    _get_scripts_log_path,
    _http_payload,
    _is_denylisted,
    _matches_credential,
    _neutralize_newlines,
    _redact_secrets,
    _redact_url_credentials,
    _rotate_log_if_large,
    _truncate_log_field,
)

if TYPE_CHECKING:
    from apm_cli.core.command_logger import CommandLogger
    from apm_cli.core.lifecycle_scripts import LifecycleEvent, ScriptEntry


_logger = logging.getLogger(__name__)


def execute_script(
    script: ScriptEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
    project_root: str | None = None,
) -> threading.Thread | None:
    """Dispatch to the correct executor based on script type.

    Returns the daemon thread for HTTP scripts (so callers can optionally
    join it), or None for command scripts and no-ops.
    """
    if script.script_type == "http":
        return _execute_http(script, event, logger=logger, verbose=verbose)
    elif script.script_type == "command":
        _execute_command(script, event, logger=logger, verbose=verbose, project_root=project_root)
    return None


# -- HTTP executor ---------------------------------------------------------


def _execute_http(
    script: ScriptEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
) -> threading.Thread | None:
    """Send an HTTP POST to the script URL in a daemon thread.

    Returns the started thread so callers can optionally join it, or
    ``None`` when the destination is refused by a security gate.

    Security hardening:
    - HTTPS-only (rejects ``http://``)
    - SSRF guard (refuses internal / loopback / link-local / metadata)
    - No redirect following
    - ``stream=True`` (response body never buffered)
    - Configurable timeout (default 10s)
    - Header values support ``$ENV_VAR`` expansion (credential vars blocked)
    """
    prepared = _prepare_http(script, event, logger=logger, verbose=verbose)
    if prepared is None:
        return None

    url, payload, request_headers, timeout, event_name, safe_url, hostname = prepared

    thread = threading.Thread(
        target=_dispatch_http_request,
        args=(url, payload, request_headers, timeout, event_name, safe_url),
        daemon=True,
    )
    thread.start()

    if verbose and logger:
        logger.verbose_detail(f"[i] {event.event} event dispatched to {hostname}")

    return thread


def dispatch_http_batch(
    scripts: list[ScriptEntry],
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
) -> list[threading.Thread]:
    """Dispatch many HTTP scripts through a bounded worker pool.

    Starts at most ``MAX_HTTP_DISPATCH_THREADS`` worker threads that drain
    a shared queue, so an event with hundreds of http entries can never
    spawn hundreds of simultaneous threads/sockets. Returns the started
    worker threads so callers can join them. Per-entry SSRF/HTTPS gating
    is applied inside each worker via :func:`_prepare_http`.
    """
    import queue

    if not scripts:
        return []

    work: queue.Queue[ScriptEntry] = queue.Queue()
    for script in scripts:
        work.put(script)

    def _worker() -> None:
        while True:
            try:
                script = work.get_nowait()
            except queue.Empty:
                return
            try:
                prepared = _prepare_http(script, event, logger=logger, verbose=verbose)
                if prepared is not None:
                    url, payload, headers, timeout, event_name, safe_url, _host = prepared
                    _dispatch_http_request(url, payload, headers, timeout, event_name, safe_url)
            except Exception:
                _logger.debug("HTTP dispatch worker failed", exc_info=True)
            finally:
                work.task_done()

    pool_size = min(len(scripts), MAX_HTTP_DISPATCH_THREADS)
    workers = [threading.Thread(target=_worker, daemon=True) for _ in range(pool_size)]
    for worker in workers:
        worker.start()
    return workers


# -- Command executor ------------------------------------------------------


def _execute_command(
    script: ScriptEntry,
    event: LifecycleEvent,
    *,
    logger: CommandLogger | None = None,
    verbose: bool = False,
    project_root: str | None = None,
) -> None:
    """Execute a shell command with the event payload on stdin.

    Command scripts run synchronously (bounded by ``timeout``), unlike
    HTTP scripts which fire in a background thread.  The timeout default
    is 30s per script -- multiple slow scripts can accumulate, but each
    is capped.
    """
    cmd = script.effective_command
    if not cmd:
        _logger.debug("Command script has no command string, skipping")
        return

    env = _build_script_env(script)
    timeout = script.effective_timeout
    cwd = _resolve_cwd(script, project_root)

    start = time.monotonic()
    proc: subprocess.Popen[str] | None = None
    try:
        # start_new_session puts the shell (and every grandchild it
        # spawns) in its OWN process group, so a timeout can reap the
        # whole tree via killpg instead of orphaning backgrounded
        # grandchildren (subprocess.run's timeout kills only the shell).
        proc = subprocess.Popen(
            cmd,
            shell=True,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            start_new_session=True,
        )
        stdout, stderr, capped = _capture_bounded(proc, event.to_json(), timeout)
        returncode = proc.returncode
        if capped:
            note = " ...[output capped: exceeded in-memory capture limit]"
            stdout = stdout + note
            stderr = stderr + note
            if logger is not None:
                warn = getattr(logger, "warning", None) or getattr(logger, "verbose_detail", None)
                if warn is not None:
                    warn(
                        "[!] Lifecycle command script output exceeded the capture "
                        "limit and was truncated; the script was terminated."
                    )
        _append_to_script_log(
            event.event,
            "command",
            cmd,
            stdout=stdout,
            stderr=stderr,
            exit_code=returncode,
            status="ok" if returncode == 0 else "error",
        )
        elapsed = time.monotonic() - start
        if elapsed > _SLOW_SCRIPT_THRESHOLD_SEC and logger is not None:
            warn = getattr(logger, "warning", None) or getattr(logger, "verbose_detail", None)
            if warn is not None:
                warn(
                    f"[!] Lifecycle command script took {elapsed:.1f}s "
                    "(command scripts run synchronously and delay the operation)."
                )
    except subprocess.TimeoutExpired:
        _logger.debug("Command script timed out: %s", cmd)
        _kill_process_group(proc)
        _append_to_script_log(event.event, "command", cmd, status="timeout")
        if logger:
            warn = getattr(logger, "warning", None) or getattr(logger, "verbose_detail", None)
            if warn is not None:
                warn(
                    f"[!] Lifecycle command script timed out after {script.effective_timeout}s: {cmd}"
                )
    except Exception as exc:
        _logger.debug("Command script failed: %s", cmd, exc_info=True)
        _kill_process_group(proc)
        _append_to_script_log(event.event, "command", cmd, stderr=str(exc), status="error")
        if verbose and logger:
            logger.verbose_detail(f"[i] Lifecycle command script failed: {cmd}")


def _signal_kill_group(proc: subprocess.Popen | None) -> None:
    """Send SIGKILL to the script's whole process group (no reap).

    Split out of ``_kill_process_group`` so the bounded-capture reader can
    terminate a runaway/over-cap writer WITHOUT also calling
    ``proc.communicate`` -- the drain threads already own the stdout/stderr
    pipes, so a second reader there would race. Best-effort, never raises.
    """
    if proc is None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            proc.kill()


def _drain_capped(stream, sink: list[str], state: dict[str, int], cap: int) -> None:
    """Read ``stream`` to EOF, retaining at most ``cap`` chars in ``sink``.

    Past the cap the bytes are discarded but reading continues so the child
    can never wedge on a full pipe -- the watchdog in ``_capture_bounded``
    SIGKILLs the group once ``state['over']`` trips. ``state`` carries ``n``
    (chars retained) and ``over`` (1 once the cap is hit).
    """
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            remaining = cap - state["n"]
            if remaining > 0:
                piece = chunk[:remaining]
                sink.append(piece)
                state["n"] += len(piece)
                if state["n"] >= cap:
                    state["over"] = 1
            else:
                state["over"] = 1
    except (OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(OSError):
            stream.close()


def _capture_bounded(
    proc: subprocess.Popen[str],
    stdin_text: str,
    timeout: float,
    cap: int = _MAX_CAPTURE_CHARS,
) -> tuple[str, str, bool]:
    """Drive ``proc`` to completion with a hard per-stream capture cap.

    A bounded replacement for ``proc.communicate(input=..., timeout=...)``:
    stdin is fed and stdout/stderr are drained on separate daemon threads so
    no single stream can deadlock, and each captured stream is clamped to
    ``cap`` characters. If either stream overflows the cap the process group
    is SIGKILLed promptly. Re-raises ``subprocess.TimeoutExpired`` on timeout
    (after killing the group + joining the readers) so the caller's existing
    timeout handling is unchanged. Returns ``(stdout, stderr, capped)``.
    """
    out: list[str] = []
    err: list[str] = []
    out_state = {"n": 0, "over": 0}
    err_state = {"n": 0, "over": 0}

    # Reject a non-finite / non-numeric deadline up front (NaN, inf, list,
    # dict, str): a NaN deadline would make every `remaining <= 0` and
    # `proc.wait(min(0.1, remaining))` comparison false-y and silently
    # DISABLE the timeout bound, letting a slow script run unbounded. This
    # mirrors the old `proc.communicate(timeout=...)` which raised promptly
    # on such values; the caller's isolation handler reaps the process.
    if not (
        isinstance(timeout, (int, float))
        and not isinstance(timeout, bool)
        and math.isfinite(timeout)
    ):
        raise ValueError(f"invalid capture timeout: {timeout!r}")

    def _feed() -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
        except (OSError, ValueError):
            pass

    workers = [
        threading.Thread(target=_feed, daemon=True),
        threading.Thread(
            target=_drain_capped, args=(proc.stdout, out, out_state, cap), daemon=True
        ),
        threading.Thread(
            target=_drain_capped, args=(proc.stderr, err, err_state, cap), daemon=True
        ),
    ]
    for w in workers:
        w.start()

    deadline = time.monotonic() + timeout
    killed_over = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _signal_kill_group(proc)
            for w in workers:
                w.join(timeout=5)
            raise subprocess.TimeoutExpired(proc.args, timeout)
        if (out_state["over"] or err_state["over"]) and not killed_over:
            _signal_kill_group(proc)
            killed_over = True
        try:
            proc.wait(timeout=min(0.1, remaining))
            break
        except subprocess.TimeoutExpired:
            continue

    # Clean shell exit reaped the LEADER. If NOTHING else holds the capture
    # pipes, the drains hit EOF and finish within a short grace -- this is the
    # well-behaved case AND the legitimately-detached-daemon case (a lifecycle
    # script may background a service whose stdio is redirected away from our
    # pipes, e.g. ``nohup svc >svc.log 2>&1 &``: its drains EOF immediately, so
    # we never reap it -- mirroring npm/yarn, which let a redirected daemon
    # survive). Only if a backgrounded GROUP MEMBER still holds the capture
    # pipes open after the grace are the drains wedged on read() (leaking the
    # grandchild + its fds + the two drain daemons); reap the whole group so the
    # pipes hit EOF and the drains finish. This bounds the wedge to the grace
    # instead of the full 5s join budget, and -- unlike an unconditional reap --
    # does NOT kill a daemon that correctly redirected its stdio.
    grace_deadline = time.monotonic() + _CAPTURE_DRAIN_GRACE
    # Only the two stdout/stderr DRAINS (workers[1:]) decide the reap. workers[0]
    # is the stdin _feed writer: a still-alive _feed means stdin is merely
    # UNCONSUMED (e.g. a legitimately-detached daemon that inherited but never
    # reads stdin, and on a many-package install the event JSON exceeds the OS
    # pipe buffer so the write blocks). Abandoning that bounded daemon-thread
    # write is harmless -- it does NOT hold our capture pipes -- so it must not
    # drive the kill decision, else a redirected-stdio daemon (which DID EOF both
    # drains, the npm/yarn-parity survival case) is wrongly reaped by killpg.
    drains = workers[1:]
    for w in drains:
        w.join(timeout=max(0.0, grace_deadline - time.monotonic()))
    if any(w.is_alive() for w in drains):
        # A group member still holds the pipes after the grace. Reap the group so
        # the drains hit EOF. If a grandchild ESCAPED the group -- e.g. it called
        # ``os.setsid()`` to form its OWN session/group -- ``killpg(proc.pid)``
        # cannot reach it and the drains stay wedged; so bound the post-kill wait
        # to a short settle budget and RETURN rather than block the install ~5s
        # per wedged drain. The escapee is a deliberately self-detached daemon (it
        # severed its own group); npm/yarn cannot kill such a process either. Its
        # two drain daemons are daemon threads (reaped at process exit) and the
        # residual fds are bounded by scripts-per-install -- a bounded latency
        # cost, not an unbounded hang.
        _signal_kill_group(proc)
        reap_deadline = time.monotonic() + _CAPTURE_DRAIN_GRACE
        for w in drains:
            w.join(timeout=max(0.0, reap_deadline - time.monotonic()))
    return "".join(out), "".join(err), bool(out_state["over"] or err_state["over"])


def _kill_process_group(proc: subprocess.Popen | None) -> None:
    """Kill the script's whole process group, then reap it.

    Required after a timeout: shell=True + start_new_session means the
    shell leads its own process group, so a single SIGKILL to the group
    also reaps backgrounded grandchildren that would otherwise orphan
    (the shell may have already exited while a grandchild lives on).
    Best-effort -- never raises into the install flow.

    The group is signalled by ``proc.pid`` directly rather than via
    ``os.getpgid(proc.pid)``: under ``start_new_session`` the child's
    PGID equals its PID, and the group persists as long as ANY member
    is alive. If the shell leader has already exited (a zombie) while a
    ``&``-backgrounded grandchild lives on, ``os.getpgid`` would raise
    ``ProcessLookupError`` and strand the live grandchild;
    ``killpg(proc.pid, ...)`` reaps the whole group regardless.
    """
    if proc is None:
        return
    _signal_kill_group(proc)
    with contextlib.suppress(subprocess.TimeoutExpired, ValueError, OSError):
        proc.communicate(timeout=5)


# -- Helpers ---------------------------------------------------------------


def _build_script_env(script: ScriptEntry) -> dict[str, str]:
    """Build the environment dict for command scripts.

    Inherits the current process environment but strips any variables
    whose names match the credential denylist (TOKEN, SECRET, PAT, KEY,
    PASSWORD, PASSPHRASE, CREDENTIAL, AUTHTOKEN) to prevent accidental
    exfiltration via scripts. A script may opt specific names back in via
    ``allowedEnvVars`` (e.g. ``ANALYTICS_TOKEN``) -- this is best-effort
    convenience, NOT a security boundary: a command script can read any
    file it has permission to.
    """
    allowed = frozenset(script.allowed_env_vars or ())
    env = {k: v for k, v in os.environ.items() if not _is_denylisted(k, allowed)}
    if script.env:
        # script.env values are merged last and may reintroduce credential-named
        # variables deliberately set by the script author. This is intentional
        # best-effort convenience (the user configured it explicitly), NOT a
        # security boundary: a command script can read any file it has permission
        # to regardless of env filtering.
        env.update(script.env)
    return env


def _resolve_cwd(script: ScriptEntry, project_root: str | None) -> str | None:
    """Resolve the working directory for a command script.

    Rejects relative paths that escape project_root to prevent lateral
    movement (e.g. 'cwd: ../../.ssh').  Absolute cwd values are passed
    through unchanged because they are explicit and visible in apm.yml.
    """
    if not script.cwd:
        return project_root
    from pathlib import Path

    if Path(script.cwd).is_absolute():
        return script.cwd
    root = Path(project_root) if project_root else Path.cwd()
    resolved = (root / script.cwd).resolve()
    root_resolved = root.resolve()
    if not str(resolved).startswith(str(root_resolved) + os.sep) and resolved != root_resolved:
        _logger.warning(
            "Script cwd '%s' escapes project root -- using project root instead", script.cwd
        )
        return str(root_resolved)
    return str(resolved)
