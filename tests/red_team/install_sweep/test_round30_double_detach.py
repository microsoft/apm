"""Round 30 -- DOUBLE-DETACH race: a grandchild that holds the capture pipes
in-group for a beat, then ``setsid``+re-forks to escape the group during the
window between the leader's exit and the installer's ``killpg``. The install
must stay BOUNDED whether the reap catches it in-group or it escapes first.

Domain: install / hang / orphan-leak.

Target surface (REAL): ``_capture_bounded`` surgical reap +
``_signal_kill_group``, via a real ``Popen(start_new_session=True)``.

WHY DISTINCT
============
Round 23 used a grandchild that ``setsid``s IMMEDIATELY. This probe delays the
escape so the reap can race it: the grandchild is reachable by
``killpg(proc.pid)`` for ``escape_delay`` seconds, then leaves. If the reap
fires during the in-group window it is reaped; if it escapes first the post-
kill wait is bounded to the settle grace and the call RETURNS. Either way the
install MUST be bounded -- never a 5s-per-drain join-budget hang.

We sweep ``escape_delay`` across the grace boundary (~0.5s) to flush a flaky
hang. Secure contract: every iteration returns under the latency ceiling.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from apm_cli.core.script_executors import _capture_bounded

from .conftest import PYEXE

_DD = str(Path(__file__).parent / "_workers" / "rt30_double_detach.py")

_LATENCY_CEILING_S = 3.0


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX killpg required")
@pytest.mark.parametrize("escape_delay", ["0.0", "0.45", "0.5", "0.55", "0.9"])
def test_round30_double_detach_is_bounded(apm_home, tmp_path, escape_delay):
    pidfile = tmp_path / f"dd_{escape_delay}.pid"
    cmd = f'{PYEXE} {_DD} "{pidfile}" {escape_delay} 30'
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
    escapee = -1
    try:
        t0 = time.monotonic()
        _capture_bounded(proc, "{}", 30.0)
        elapsed = time.monotonic() - t0
        assert elapsed < _LATENCY_CEILING_S, (
            f"double-detach (escape_delay={escape_delay}) hung {elapsed:.2f}s "
            f"(> {_LATENCY_CEILING_S}s) -- a group-escape race must stay bounded"
        )
        # Harvest the escapee pid (if it got far enough to record one) for cleanup.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not pidfile.exists():
            time.sleep(0.01)
        if pidfile.exists():
            txt = pidfile.read_text().strip()
            if txt:
                escapee = int(txt)
    finally:
        if escapee > 0:
            with contextlib.suppress(OSError):
                os.kill(escapee, signal.SIGKILL)
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)
