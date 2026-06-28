"""Round 24 -- a legit stdout/stderr-redirecting daemon must SURVIVE an
``apm install`` even when the lifecycle event JSON (fed on the script's stdin)
is large, as it is on a many-package install.

TARGET
======
``_capture_bounded`` in ``core/script_executors.py``. On a clean shell-leader
exit it grace-joins the capture workers and, if any worker is still alive,
SIGKILLs the whole process group (``os.killpg(proc.pid, SIGKILL)``) to EOF the
wedged drains.

THE BUG (round-24 finding)
==========================
The reap predicate is ``any(w.is_alive() for w in workers)`` where
``workers`` is ``[_feed, drain_stdout, drain_stderr]`` -- it INCLUDES the
``_feed`` stdin-writer thread. ``_feed`` writes the ENTIRE event JSON to the
child's stdin pipe. On a real ``apm install`` the event payload carries the
full ``packages`` list, so the JSON can exceed the OS pipe buffer (~64 KiB).

A legitimately-detached daemon (``nohup svc >svc.log 2>&1 &``) redirects its
stdout/stderr to a file -- so both DRAINS EOF immediately, the round-22
"redirected daemon survives" contract -- but inherits stdin and never reads it
(the overwhelmingly common case; services ignore stdin). With a large payload
``_feed`` BLOCKS on the oversized stdin write (the daemon holds the read-end
open, so no EPIPE). After the grace ``_feed`` is therefore STILL ALIVE, so
``any(w.is_alive())`` is True even though both stdout/stderr drains already
EOF'd. The group is reaped -- and because a ``nohup ... &`` daemon stays in the
shell's process group (no ``setsid``), ``killpg(proc.pid)`` REACHES AND KILLS
IT.

That is a FALSE REAP: the maintainer-protected, correctly-redirecting daemon
is killed on a large-package install. The trigger is purely the stdin payload
size -- the SAME daemon SURVIVES when the payload is small (control test
below), proving the wedged ``_feed`` thread, not the daemon, is the culprit.

SECURE CONTRACT
===============
A blocked ``_feed`` is never a reason to reap the group: feeding stdin is a
daemon thread whose only cost is one abandoned writer (bounded, reaped at
process exit), NOT a leaked pipe-holder. The reap decision must key off the
stdout/stderr DRAINS only (the threads whose liveness actually means a group
member still holds the capture pipes). A surgical, daemon-preserving fix:
reap only when a DRAIN is still alive, not when ``_feed`` is.

This probe FAILS at head 9069ceec3 (daemon killed by false reap) and PASSES
once the reap predicate excludes ``_feed``.
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from pathlib import Path

from apm_cli.core import script_executors as se

from .conftest import PYEXE, kill_pid, pid_alive

WORKER = Path(__file__).parent / "_workers" / "rt24_redir_daemon.py"

# Big enough to overflow any OS pipe buffer (macOS/Linux are 16-64 KiB) so the
# unread stdin write in ``_feed`` blocks. Mirrors a many-package install JSON.
_BIG_STDIN = "x" * (2 * 1024 * 1024)
_SMALL_STDIN = '{"event":"post-install","packages":[]}'

# ``_capture_bounded`` must return within the grace + settle budget, never the
# full multi-second join budget.
_RETURN_BOUND_SEC = 4.0


def _spawn_redir_daemon(pidfile: Path, logfile: Path, sleep_s: float) -> subprocess.Popen:
    """Launch the redirecting-daemon worker exactly as ``_execute_command`` does."""
    cmd = f'{PYEXE} "{WORKER}" "{pidfile}" "{logfile}" {sleep_s}'
    return subprocess.Popen(
        cmd,
        shell=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _read_pid(pidfile: Path, deadline_s: float = 5.0) -> int:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        with contextlib.suppress(OSError, ValueError):
            txt = pidfile.read_text().strip()
            if txt:
                return int(txt)
        time.sleep(0.02)
    raise AssertionError("daemon never recorded its pid")


def _run_capture(proc: subprocess.Popen, stdin_text: str, result: dict) -> None:
    try:
        t0 = time.monotonic()
        se._capture_bounded(proc, stdin_text, timeout=15.0)
        result["elapsed"] = time.monotonic() - t0
    except BaseException as exc:
        result["error"] = repr(exc)


def test_large_stdin_does_not_false_reap_redirecting_daemon(tmp_path: Path) -> None:
    """A redirected daemon must survive even with a large stdin payload."""
    pidfile = tmp_path / "daemon.pid"
    logfile = tmp_path / "daemon.log"
    proc = _spawn_redir_daemon(pidfile, logfile, sleep_s=8.0)
    daemon_pid: int | None = None
    result: dict = {}
    try:
        # Run the capture on a watchdog thread so a hang can never wedge the suite.
        worker = threading.Thread(target=_run_capture, args=(proc, _BIG_STDIN, result), daemon=True)
        worker.start()
        worker.join(timeout=12.0)
        assert not worker.is_alive(), "_capture_bounded hung well past its budget"

        elapsed = result.get("elapsed")
        assert elapsed is not None, f"_capture_bounded raised: {result.get('error')}"
        assert elapsed < _RETURN_BOUND_SEC, (
            f"_capture_bounded took {elapsed:.2f}s -- expected bounded return"
        )

        daemon_pid = _read_pid(pidfile)
        # Give any (erroneous) reap SIGKILL time to land.
        time.sleep(0.5)
        assert pid_alive(daemon_pid), (
            "FALSE REAP: a correctly stdout/stderr-redirecting daemon "
            "(npm/yarn parity, MUST survive) was killed because the wedged "
            "_feed stdin-writer was counted in the reap predicate on a "
            "large-payload install."
        )
    finally:
        if daemon_pid is not None:
            kill_pid(daemon_pid)
        with contextlib.suppress(Exception):
            se._signal_kill_group(proc)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


def test_small_stdin_redirecting_daemon_survives_control(tmp_path: Path) -> None:
    """Control: with a small payload the SAME daemon survives at this head.

    Proves the trigger is purely the stdin payload size (the wedged _feed),
    not anything about the daemon itself.
    """
    pidfile = tmp_path / "daemon.pid"
    logfile = tmp_path / "daemon.log"
    proc = _spawn_redir_daemon(pidfile, logfile, sleep_s=8.0)
    daemon_pid: int | None = None
    result: dict = {}
    try:
        worker = threading.Thread(
            target=_run_capture, args=(proc, _SMALL_STDIN, result), daemon=True
        )
        worker.start()
        worker.join(timeout=12.0)
        assert not worker.is_alive(), "_capture_bounded hung on the control path"
        assert result.get("elapsed") is not None, result.get("error")

        daemon_pid = _read_pid(pidfile)
        time.sleep(0.5)
        assert pid_alive(daemon_pid), (
            "control regression: redirected daemon killed even with small stdin"
        )
    finally:
        if daemon_pid is not None:
            kill_pid(daemon_pid)
        with contextlib.suppress(Exception):
            se._signal_kill_group(proc)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
