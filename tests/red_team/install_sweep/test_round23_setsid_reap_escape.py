"""Round 23 -- a backgrounded pipe-holder that ``setsid``s into a NEW process
group must not HANG the install; it survives (deliberate detach) but at a
BOUNDED latency cost.

Target: the round-22 success-path surgical reap in ``_capture_bounded``
(core/script_executors.py). On a clean shell-leader exit, the drains are
grace-joined; if a backgrounded group member still holds the capture pipes the
group is SIGKILLed (``os.killpg(proc.pid, SIGKILL)``) so the pipes EOF.

THE GENUINE HARM (the round-23 finding)
=======================================
A backgrounded grandchild that calls ``os.setsid()`` -- THE canonical
daemon-detach syscall -- becomes the leader of a BRAND-NEW process group whose
PGID == its own PID, which is NOT ``proc.pid``. fds 1/2 (the inherited capture
pipe write-ends) survive setsid unchanged, so the grandchild keeps the pipes
OPEN. The group-scoped reap ``killpg(proc.pid)`` cannot reach it (it left the
group), so the drains stay wedged on ``read()``.

On head 087e1425a the final fallback joined EACH wedged drain with
``join(timeout=5)`` -- burning the FULL ~10s (2 drains x 5s) before returning.
THAT ~10s success-path install HANG is the genuine harm.

THE CORRECT CONTRACT -- BOUNDED LATENCY, NOT REAP
=================================================
A process that ``setsid``s into its own session/group has *deliberately
detached itself* -- that is the daemon-detach contract. ``killpg(proc.pid)``
cannot reach a foreign group, and on macOS the group's members cannot even be
enumerated, so a generic package manager CANNOT kill such a process: npm, yarn
and pnpm all let a ``setsid``'d / double-forked daemon survive a lifecycle
script. Demanding APM reap it is over-strict and unachievable.

What APM MUST guarantee is that such an escapee does not turn a fire-and-forget
install into a multi-second HANG. So the secure contract asserted here is:

  * ``_capture_bounded`` (and the end-to-end ``_execute_command``) RETURN
    PROMPTLY (bounded latency ~ the grace budget, not the full join budget)
    even when a setsid escapee holds the capture pipes; and
  * the residual leak is BOUNDED (the two drain daemons for THIS one wedged
    script -- daemon threads reaped at process exit), not unbounded.

The escapee SURVIVING is expected (npm/yarn parity); the two control tests
prove the in-group case IS reaped and a redirected daemon IS preserved, so the
behavior is cleanly scoped.

These probes FAIL on head 087e1425a (the ~10s join-budget hang) and PASS once
the post-kill fallback is bounded to the short settle grace.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from apm_cli.core.lifecycle_scripts import LifecycleEvent
from apm_cli.core.script_executors import _capture_bounded, _execute_command

from .conftest import PYEXE, make_command_entry, pid_alive

_SETSID_HOLD = str(Path(__file__).parent / "_workers" / "rt23_setsid_holdpipe.py")
_HOLD = str(Path(__file__).parent / "_workers" / "holdpipe.py")

# Bounded-latency ceiling. The capture grace + one post-kill settle grace is
# ~1s (_CAPTURE_DRAIN_GRACE x 2); 3s leaves generous CI headroom while still
# proving the ~10s join-budget hang is gone.
_LATENCY_CEILING_S = 3.0


def _wait_pidfile(pidfile: Path, deadline: float) -> int:
    while time.monotonic() < deadline:
        if pidfile.exists():
            txt = pidfile.read_text().strip()
            if txt:
                return int(txt)
        time.sleep(0.02)
    raise AssertionError("worker never recorded its grandchild pid")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork required")
def test_capture_bounded_setsid_escape_is_bounded_latency(tmp_path):
    """A setsid'd pipe-holder must NOT hang the capture; it returns promptly.

    Drives REAL ``_capture_bounded`` against a shell that backgrounds a
    grandchild which ``setsid``s into a new group while holding the capture
    pipes. Asserts the secure contract: PROMPT return (bounded latency). FAILS
    on head 087e1425a -- the post-kill fallback burned the full ~10s join
    budget because ``killpg(proc.pid)`` misses the escaped grandchild.
    """
    pidfile = tmp_path / "setsid_gc.pid"
    # 30s holder: outlives any settle grace, so if the capture returned only
    # because the holder happened to exit we would see a false pass; it does
    # not, so a prompt return proves the join is bounded (not waiting on EOF).
    cmd = f'{PYEXE} {_SETSID_HOLD} "{pidfile}" 30'
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
    result: dict[str, float] = {}

    def _run() -> None:
        _capture_bounded(proc, "{}", 30.0)
        result["elapsed"] = time.monotonic() - t0

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(40)

    gc_pid = None
    try:
        assert not th.is_alive(), "_capture_bounded deadlocked"
        gc_pid = _wait_pidfile(pidfile, time.monotonic() + 5)

        # (A) Bounded latency: a foreign-group wedge must cost ~the grace, not
        #     the full join budget. The pre-fix setsid escape burns ~10s.
        assert result.get("elapsed", 99.0) < _LATENCY_CEILING_S, (
            f"setsid-escaped wedge caused a {result.get('elapsed')!r}s install "
            "hang (killpg(proc.pid) missed the escaped group; both drain joins "
            "burned the full 5s budget). Bound the post-kill join to the grace."
        )

        # (B) The escapee SURVIVES -- a process that setsid'd into its own group
        #     deliberately detached and cannot be reaped by a group-scoped kill
        #     (npm/yarn parity). This documents the accepted residual.
        assert pid_alive(gc_pid), (
            "setsid escapee unexpectedly died -- the contract is bounded "
            "latency with survival, not reap"
        )

        # (C) Residual thread leak is BOUNDED to the two drains for THIS one
        #     wedged script (daemon threads, reaped at process exit) -- not
        #     unbounded growth.
        leaked = threading.active_count() - before
        assert leaked <= 2, (
            f"expected at most the 2 wedged drain daemons, saw {leaked} leaked threads"
        )
    finally:
        if gc_pid is not None:
            with contextlib.suppress(OSError):
                os.kill(gc_pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork required")
def test_execute_command_setsid_escape_is_bounded_latency(apm_home, tmp_path):
    """End-to-end on the real firing path: _execute_command must not hang.

    Same setsid escape, but through the public ``_execute_command`` entry so the
    bounded-latency guarantee is proven on the production install path.
    """
    pidfile = tmp_path / "setsid_gc2.pid"
    cmd = f'{PYEXE} {_SETSID_HOLD} "{pidfile}" 30'
    entry = make_command_entry(cmd, timeout_sec=30)
    event = LifecycleEvent.create("post-install")

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
        assert not th.is_alive(), "_execute_command never returned (deadlock)"
        gc_pid = _wait_pidfile(pidfile, time.monotonic() + 5)

        assert state.get("elapsed", 99.0) < _LATENCY_CEILING_S, (
            f"install hung {state.get('elapsed')!r}s on a setsid-escaped wedge"
        )
    finally:
        if gc_pid is not None:
            with contextlib.suppress(OSError):
                os.kill(gc_pid, signal.SIGKILL)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork required")
def test_control_same_group_holder_is_reaped(tmp_path):
    """CONTROL (must PASS): the in-group pipe-holder IS reaped by round-22.

    Proves the bounded-latency relaxation does NOT weaken the in-group reap --
    a member that stayed in the group is still SIGKILLed and the drains EOF.
    """
    pidfile = tmp_path / "ctl_gc.pid"
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

    t0 = time.monotonic()
    result: dict[str, float] = {}

    def _run() -> None:
        _capture_bounded(proc, "{}", 30.0)
        result["elapsed"] = time.monotonic() - t0

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(40)

    gc_pid = None
    try:
        assert not th.is_alive(), "_capture_bounded deadlocked on control"
        gc_pid = _wait_pidfile(pidfile, time.monotonic() + 5)
        assert result.get("elapsed", 99.0) < _LATENCY_CEILING_S, "control: in-group reap was slow"
        deadline = time.monotonic() + 5
        while pid_alive(gc_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not pid_alive(gc_pid), "control: in-group holder should be reaped"
    finally:
        if gc_pid is not None:
            with contextlib.suppress(OSError):
                os.kill(gc_pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
