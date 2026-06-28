"""Round 30 -- ASYMMETRIC drain reap: a backgrounded grandchild that closes
stdout but HOLDS stderr (one capture pipe EOFs, the other stays wedged) must
still drive the surgical reap, bound the install, and reap the in-group leak.

Domain: install / process / concurrency.

Target surface (REAL, no reimplementation): ``_capture_bounded`` +
``_signal_kill_group`` in ``core/script_executors.py``, exercised through a
real ``subprocess.Popen(..., start_new_session=True)`` (so the SUT's own
``start_new_session`` + ``os.killpg`` + threaded drains run authentically),
and end-to-end through ``_execute_command``.

WHY THIS IS A DISTINCT VECTOR
=============================
Rounds 22/23/24 stressed the reap with grandchildren that hold BOTH pipes
(symmetric) or that ``setsid``-escape. This probe holds exactly ONE pipe
(stderr) while letting the other (stdout) hit EOF immediately. The reap
predicate is ``any(w.is_alive() for w in drains)`` over BOTH drains -- so one
finished drain must not mask a still-wedged sibling. If the predicate were
keyed on the wrong drain (or on "all alive"), the stderr leak would slip
through: the grandchild + the stderr drain daemon + the stderr fd would leak,
and a heavier load could exhaust fds.

SECURE CONTRACT (asserted; PASSES on HEAD if robust):
  * ``_capture_bounded`` RETURNS within a bounded latency ceiling; and
  * the in-group grandchild is REAPED -- ``os.killpg(proc.pid, 0)`` raises
    ``ProcessLookupError`` (whole group gone) shortly after return.

Control: the round-24 redirecting daemon (stdout+stderr -> a file, both drains
EOF) SURVIVES -- proving the reap is surgical, not a blanket kill.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from apm_cli.core.lifecycle_scripts import LifecycleEvent
from apm_cli.core.script_executors import _capture_bounded, _execute_command

from .conftest import PYEXE, make_command_entry, pid_alive

_ASYM = str(Path(__file__).parent / "_workers" / "rt30_asym_drain.py")
_REDIR = str(Path(__file__).parent / "_workers" / "rt24_redir_daemon.py")

_LATENCY_CEILING_S = 3.0


def _wait_pidfile(pidfile: Path, deadline: float) -> int:
    while time.monotonic() < deadline:
        if pidfile.exists():
            txt = pidfile.read_text().strip()
            if txt:
                return int(txt)
        time.sleep(0.01)
    return -1


def _group_gone(pgid: int, deadline: float) -> bool:
    """True once ``killpg(pgid, 0)`` reports the whole group reaped."""
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.02)
    return False


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX killpg required")
def test_round30_asym_drain_is_reaped_and_bounded(apm_home, tmp_path):
    pidfile = tmp_path / "asym.pid"
    cmd = f'{PYEXE} {_ASYM} "{pidfile}" 30'
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pgid = proc.pid
    grandchild = -1
    try:
        t0 = time.monotonic()
        _out, _err, capped = _capture_bounded(proc, "{}", 30.0)
        elapsed = time.monotonic() - t0

        grandchild = _wait_pidfile(pidfile, time.monotonic() + 1.0)
        assert grandchild > 0, "grandchild never recorded its pid"

        # Bounded latency: the stderr drain was wedged but the surgical reap
        # fires off the grace, not the full join budget.
        assert elapsed < _LATENCY_CEILING_S, (
            f"asymmetric-drain capture took {elapsed:.2f}s (> {_LATENCY_CEILING_S}s) "
            "-- a half-finished drain set should not burn the join budget"
        )
        # In-group grandchild MUST be reaped by killpg(proc.pid).
        assert _group_gone(pgid, time.monotonic() + 2.0), (
            "in-group asymmetric pipe-holder survived the surgical reap "
            "(stderr drain leak: a finished stdout drain masked the live stderr drain)"
        )
        assert not capped
    finally:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX killpg required")
def test_round30_redirecting_daemon_survives_control(apm_home, tmp_path):
    """Control: a daemon that redirects BOTH streams to a file survives."""
    pidfile = tmp_path / "redir.pid"
    logfile = tmp_path / "svc.log"
    cmd = f'{PYEXE} {_REDIR} "{pidfile}" "{logfile}" 4'
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pgid = proc.pid
    daemon = -1
    try:
        t0 = time.monotonic()
        _capture_bounded(proc, "{}", 30.0)
        elapsed = time.monotonic() - t0
        daemon = _wait_pidfile(pidfile, time.monotonic() + 1.0)
        assert daemon > 0
        assert elapsed < _LATENCY_CEILING_S
        # The redirected daemon EOF'd both drains -> must NOT be reaped.
        assert pid_alive(daemon), "a legit redirecting daemon (npm/yarn parity) was wrongly reaped"
    finally:
        if daemon > 0:
            with contextlib.suppress(OSError):
                os.kill(daemon, signal.SIGKILL)
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX killpg required")
def test_round30_asym_drain_end_to_end_bounded(apm_home, tmp_path):
    """End-to-end ``_execute_command``: an asymmetric pipe-holder must not
    hang the synchronous firing path."""
    pidfile = tmp_path / "asym_e2e.pid"
    cmd = f'{PYEXE} {_ASYM} "{pidfile}" 30'
    entry = make_command_entry(cmd, event="post-install")
    event = LifecycleEvent(event="post-install")
    t0 = time.monotonic()
    _execute_command(entry, event, logger=None, verbose=False)
    elapsed = time.monotonic() - t0
    grandchild = _wait_pidfile(pidfile, time.monotonic() + 1.0)
    try:
        assert elapsed < _LATENCY_CEILING_S, (
            f"_execute_command hung {elapsed:.2f}s on an asymmetric pipe-holder"
        )
    finally:
        if grandchild > 0:
            with contextlib.suppress(OSError):
                os.kill(grandchild, signal.SIGKILL)
