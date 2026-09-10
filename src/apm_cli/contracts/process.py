"""Bounded POSIX process-group observation for native contract attempts."""

import contextlib
import os
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .events import EventEmitter
from .models import (
    ByteSink,
    ContractError,
    ContractLimits,
    ProcessObservation,
    ProcessRequest,
    StartedSink,
)


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # POSIX EPERM proves existence, not termination or signal authority.
        return True
    return True


def _signal_group(pgid: int, selected: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, selected)


def _observe_group(pgid: int, timeout_seconds: float) -> tuple[dict[str, object], ...]:
    """Retain only owned-group identity/state/name, never argv or environment."""
    executable = shutil.which("ps")
    if executable is None:
        return ({"inspection": "ps unavailable"},)
    try:
        result = subprocess.run(
            (executable, "-g", str(pgid), "-o", "pid=,ppid=,pgid=,stat=,comm="),
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ({"inspection": "group inspection unavailable"},)
    if result.returncode not in (0, 1):
        return ({"inspection": "group inspection failed"},)
    identities: list[dict[str, object]] = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines()[:128]:
        columns = line.split(None, 4)
        if len(columns) == 5 and all(value.isdecimal() for value in columns[:3]):
            pid, ppid, group = (int(value) for value in columns[:3])
            if group == pgid:
                identities.append(
                    {
                        "pid": pid,
                        "ppid": ppid,
                        "pgid": group,
                        "state": columns[3],
                        "name": Path(columns[4]).name[:128],
                    }
                )
    return tuple(identities)


def supervise_process(
    request: ProcessRequest,
    *,
    on_bytes: ByteSink,
    on_started: StartedSink | None = None,
    events: EventEmitter | None = None,
    limits: ContractLimits | None = None,
) -> ProcessObservation:
    """Drain both pipes while bounding deadlines and original-group cleanup.

    This cannot contain descendants that leave the original process group.
    Callback failures propagate only after bounded cleanup has been attempted.
    """
    limits = limits or ContractLimits()
    started = time.monotonic()
    if os.name != "posix":
        return ProcessObservation(None, error="Managed native execution requires POSIX.")
    if request.timeout_seconds <= 0:
        return ProcessObservation(None, stop_reason="timeout")
    try:
        child = subprocess.Popen(
            request.argv,
            cwd=request.cwd,
            env=request.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return ProcessObservation(None, error=f"Could not start the selected process: {exc}")
    pgid = child.pid
    stop_reason = None
    stop_started: float | None = None
    sent: list[str] = []
    cleanup_confirmed = False
    next_heartbeat = started + 10
    leader_exited_at: float | None = None
    residual_group: tuple[dict[str, object], ...] = ()
    selector = selectors.DefaultSelector()
    try:
        for stream, pipe in (("stdout", child.stdout), ("stderr", child.stderr)):
            if pipe is None:
                raise ContractError(
                    "Managed child pipe was not created.", code="process_pipe_failed"
                )
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, stream)
        if on_started is not None:
            on_started(child.pid, pgid)
        if events is not None:
            events.emit("process_started", pid=child.pid, pgid=pgid)
        while True:
            try:
                now = time.monotonic()
                returncode = child.poll()
                group_alive = _group_exists(pgid)
                if returncode is not None and leader_exited_at is None:
                    leader_exited_at = now
                if now >= next_heartbeat and returncode is None and stop_started is None:
                    if events is not None:
                        events.emit("heartbeat", pid=child.pid)
                    next_heartbeat = now + 10
                if returncode is not None and not group_alive and not selector.get_map():
                    cleanup_confirmed = True
                    break
                if stop_started is None:
                    if now - started >= request.timeout_seconds:
                        stop_reason = "timeout"
                    elif (
                        leader_exited_at is not None
                        and group_alive
                        and now - leader_exited_at >= min(0.5, limits.cleanup_seconds / 4)
                    ):
                        stop_reason = "lingering_children"
                    if stop_reason is not None:
                        stop_started = (
                            leader_exited_at if stop_reason == "lingering_children" else now
                        )
                        if stop_reason == "lingering_children":
                            residual_group = _observe_group(
                                pgid, min(0.5, limits.cleanup_seconds / 4)
                            )
                        if events is not None:
                            events.emit("stop_requested", reason=stop_reason)
                        _signal_group(pgid, signal.SIGTERM)
                        sent.append("SIGTERM")
                if stop_started is not None:
                    elapsed = now - stop_started
                    if elapsed >= limits.cleanup_seconds / 2 and "SIGKILL" not in sent:
                        _signal_group(pgid, signal.SIGKILL)
                        sent.append("SIGKILL")
                    if elapsed >= limits.cleanup_seconds:
                        child.poll()
                        cleanup_confirmed = child.returncode is not None and not _group_exists(pgid)
                        break
                for key, _ in selector.select(timeout=0.05):
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if chunk:
                        on_bytes(key.data, chunk)
                    else:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
            except KeyboardInterrupt:
                stop_reason = "cancelled"
                if stop_started is None:
                    stop_started = time.monotonic()
                    if events is not None:
                        events.emit("stop_requested", reason=stop_reason)
                    _signal_group(pgid, signal.SIGTERM)
                    sent.append("SIGTERM")
    finally:
        # An exception in a drain/record callback must not strand a child.
        if not cleanup_confirmed:
            _signal_group(pgid, signal.SIGKILL)
            remainder = (
                limits.cleanup_seconds
                if stop_started is None
                else max(0, limits.cleanup_seconds - (time.monotonic() - stop_started))
            )
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=remainder)
        selector.close()
        for pipe in (child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()
    if events is not None and stop_reason is not None:
        events.emit("stop_observed", confirmed=cleanup_confirmed, reason=stop_reason)
    return ProcessObservation(
        child.returncode,
        pid=child.pid,
        pgid=pgid,
        elapsed_seconds=time.monotonic() - started,
        stop_reason=stop_reason,
        cleanup_confirmed=cleanup_confirmed,
        signals=tuple(sent),
        residual_group=residual_group,
    )


def local_git(
    root: Path,
    *arguments: str,
    maximum_bytes: int = 8 * 1024 * 1024,
    accepted_codes: tuple[int, ...] = (0,),
) -> bytes:
    """Run a bounded local Git operation without inherited hooks or Git overrides."""
    executable = shutil.which("git")
    if executable is None:
        raise ContractError(
            "Git is required for captured assessment workspaces.", code="git_missing"
        )
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_OPTIONAL_LOCKS="0",
    )
    output = bytearray()
    errors = bytearray()

    def receive(stream: str, chunk: bytes) -> None:
        destination = output if stream == "stdout" else errors
        if len(destination) + len(chunk) > maximum_bytes:
            raise ContractError("Local Git output exceeded its limit.", code="git_output_limit")
        destination.extend(chunk)

    observation = supervise_process(
        ProcessRequest(
            (
                executable,
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "user.name=APM captured baseline",
                "-c",
                "user.email=apm-local@invalid",
                *arguments,
            ),
            root,
            30,
            env,
        ),
        on_bytes=receive,
    )
    if (
        observation.returncode not in accepted_codes
        or observation.stop_reason
        or not observation.cleanup_confirmed
    ):
        raise ContractError(
            "Local Git baseline operation failed. Check Git and the project files.",
            code="baseline_git_failed",
        )
    return bytes(output)
