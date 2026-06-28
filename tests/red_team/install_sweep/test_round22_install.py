"""Round 22 -- success-path grandchild + drain-thread leak in _capture_bounded.

Target: the round-20 threaded bounded-capture watchdog
(`_capture_bounded` / `_drain_capped` in core/script_executors.py) on the
NORMAL (non-timeout, non-cap) SUCCESS path of `_execute_command`.

THE BREAK
=========
The watchdog loop blocks on ``proc.wait(...)`` -- i.e. it considers the
script "finished" the instant the *shell leader* exits. But the two drain
threads block on ``proc.stdout.read()`` / ``proc.stderr.read()``, which only
return EOF when EVERY holder of the pipe write-end is gone. A lifecycle
command that backgrounds ANYTHING (``some-daemon &``, ``npm run build &``,
a dev-server/watcher) leaves a grandchild that INHERITED the capture pipes.
When the shell exits 0:

  1. ``proc.wait()`` returns immediately -> the watchdog breaks (no timeout,
     no cap), so ``_kill_process_group`` is NEVER called (success path).
  2. The grandchild is still alive, still holding the pipe write-ends open.
  3. Each drain thread is wedged in ``stream.read(65536)`` with no EOF.
  4. ``for w in workers: w.join(timeout=5)`` times out on BOTH drains
     (~10s wasted) and returns WITHOUT the threads having finished.

Net per install of a benign-but-trusted project that backgrounds a process:
  * a ~10s success-path install HANG (2 drains x 5s join timeout), AND
  * 2 leaked daemon threads blocked on read (+ their pipe-read fds), AND
  * a LEAKED grandchild process that is never reaped.

The OLD pre-round-20 code (``proc.communicate(input=..., timeout=...)``)
read to EOF, so the backgrounded grandchild made communicate block until the
30s timeout -> ``TimeoutExpired`` -> ``_kill_process_group`` REAPED the
grandchild. Round-20's switch to a ``proc.wait()``-driven watchdog regressed
that: on the success path the grandchild now survives and the drains leak.

This is unbounded per install in any long-lived parent that runs repeated
installs, and it is triggered by COMMON benign content (a backgrounded
watcher), not by hostile pre-trusted content.

THE FIX (round 22)
==================
``_capture_bounded`` now calls ``_signal_kill_group(proc)`` on the SUCCESS
break too (it already did on the timeout / over-cap branches), immediately
before the final drain-join. The clean ``proc.wait()`` reaps the shell leader;
the follow-up ``killpg`` reaps any backgrounded group member, closing the
inherited pipe write-ends so the drains hit EOF and the join returns promptly.
A well-behaved script with no surviving group member pays nothing (the killpg
is a suppressed ``ProcessLookupError`` no-op). These tests assert the CORRECTED
contract: prompt return, grandchild REAPED, drains NOT leaked.
"""

from __future__ import annotations

import contextlib
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from apm_cli.core.lifecycle_scripts import LifecycleEvent
from apm_cli.core.script_executors import _capture_bounded, _execute_command

from .conftest import PYEXE, make_command_entry, pid_alive

_HOLD = str(Path(__file__).parent / "_workers" / "holdpipe.py")


def _wait_pidfile(pidfile: Path, deadline: float) -> int:
    """Block until the worker has written its grandchild pid, return it."""
    while time.monotonic() < deadline:
        if pidfile.exists():
            txt = pidfile.read_text().strip()
            if txt:
                return int(txt)
        time.sleep(0.02)
    raise AssertionError("worker never recorded its grandchild pid")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork required")
def test_success_path_reaps_grandchild_and_drains(apm_home, tmp_path, monkeypatch):
    """Success-path install reaps the backgrounded grandchild + its drains.

    A lifecycle command backgrounds a grandchild that inherits stdout/stderr,
    then the shell exits 0. We drive REAL ``_execute_command`` on a daemon
    thread and assert it returns PROMPTLY, the grandchild is reaped (killpg on
    the success break), and the drain-thread count returns to baseline.
    """
    pidfile = tmp_path / "gc.pid"
    # 30s grandchild: would outlive a join(timeout=5) drain if it were leaked,
    # so observing it dead proves the success-path killpg reaped it.
    cmd = f'{PYEXE} {_HOLD} "{pidfile}" 30'
    entry = make_command_entry(cmd, timeout_sec=30)
    event = LifecycleEvent.create("post-install")

    before = threading.active_count()
    t0 = time.monotonic()
    state: dict[str, float] = {}

    def _run() -> None:
        _execute_command(entry, event)
        state["elapsed"] = time.monotonic() - t0

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(40)

    gc_pid = None
    try:
        assert not th.is_alive(), "_execute_command never returned (hard deadlock)"
        gc_pid = _wait_pidfile(pidfile, time.monotonic() + 5)

        # (A) The success path returned PROMPTLY -- no ~10s drain-join stall.
        assert state.get("elapsed", 99.0) < 8.0, (
            f"expected a prompt success-path return, saw {state.get('elapsed')!r}"
        )

        # (B) The backgrounded grandchild was REAPED by the success-break killpg
        #     (poll: SIGKILL + reparent-to-init reap is near-instant but async).
        deadline = time.monotonic() + 5
        while pid_alive(gc_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not pid_alive(gc_pid), (
            "expected the backgrounded grandchild to be reaped on the success path"
        )

        # (C) The drain threads finished -- active count returns to baseline.
        deadline = time.monotonic() + 5
        while threading.active_count() - before > 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        leaked = threading.active_count() - before
        assert leaked <= 0, f"expected no leaked drain threads, saw {leaked}"
    finally:
        if gc_pid is not None:
            with contextlib.suppress(OSError):
                os.kill(gc_pid, signal.SIGKILL)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork required")
def test_capture_bounded_reaps_pipe_holder(tmp_path):
    """Lower-level proof against _capture_bounded directly.

    Confirms the watchdog returns promptly AND reaps the backgrounded grandchild
    so the drain threads do not outlive the call (the success-break killpg).
    """
    import subprocess

    pidfile = tmp_path / "gc2.pid"
    cmd = f'{PYEXE} {_HOLD} "{pidfile}" 20'
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    before = threading.active_count()
    t0 = time.monotonic()
    result: dict[str, object] = {}

    def _run() -> None:
        out, err, capped = _capture_bounded(proc, "{}", 30.0)
        result["done"] = (out, err, capped)
        result["elapsed"] = time.monotonic() - t0

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(40)

    gc_pid = None
    try:
        assert not th.is_alive(), "_capture_bounded deadlocked"
        assert "done" in result, "capture never returned"
        assert result.get("elapsed", 99.0) < 8.0, "expected a prompt capture return"
        gc_pid = _wait_pidfile(pidfile, time.monotonic() + 5)

        # The shell exited 0; the success-break killpg reaped the grandchild,
        # so the pipes closed, the drains hit EOF, and they did NOT leak.
        deadline = time.monotonic() + 5
        while pid_alive(gc_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not pid_alive(gc_pid), "expected the pipe-holding grandchild reaped"

        deadline = time.monotonic() + 5
        while threading.active_count() - before > 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        leaked = threading.active_count() - before
        assert leaked <= 0, f"expected drains finished, saw {leaked} leaked"
    finally:
        if gc_pid is not None:
            with contextlib.suppress(OSError):
                os.kill(gc_pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
