"""Round 30 -- _feed/stdin deadlock: a script that NEVER reads stdin while
flooding stdout+stderr past the cap, driven with a LARGE stdin payload that
exceeds the OS pipe buffer (so the ``_feed`` writer blocks). The install must
still be BOUNDED and capped -- ``_feed`` must not be able to wedge the reap.

Domain: install / hang / resource-unbounded.

Target surface (REAL): ``_capture_bounded`` -- the ``_feed`` stdin-writer
thread (``workers[0]``) and the over-cap watchdog. The reap predicate keys off
the two DRAIN workers (``workers[1:]``) only; a still-blocked ``_feed`` (stdin
unconsumed, payload > pipe buf) must NOT drive or stall the kill decision.

WHY DISTINCT
============
Round 24 proved a large stdin payload must not FALSE-reap a redirecting daemon.
This probe inverts it: stdin is large AND unread AND the child floods past the
cap. The watchdog SIGKILLs the group on over-cap; ``_feed`` then EPIPEs and
unblocks. If the reap waited on ``_feed`` (instead of the drains), the blocked
writer would stall the return. Secure contract: bounded + capped regardless of
the unconsumed multi-MiB stdin.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from apm_cli.core.script_executors import _MAX_CAPTURE_CHARS, _capture_bounded

from .conftest import PYEXE

_FLOOD = str(Path(__file__).parent / "_workers" / "rt21_flood.py")

_CEILING_S = 8.0


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX killpg required")
def test_round30_feed_blocked_large_stdin_still_bounded(apm_home):
    # ~3 MiB stdin that the child never reads -> _feed blocks after the pipe
    # buffer fills. The child floods 4 MiB/stream past the 1 MiB cap.
    big_stdin = "X" * (3 * 1024 * 1024)
    cmd = f"{PYEXE} {_FLOOD} {4 * 1024 * 1024}"
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
    try:
        t0 = time.monotonic()
        out, err, capped = _capture_bounded(proc, big_stdin, 30.0)
        elapsed = time.monotonic() - t0

        assert elapsed < _CEILING_S, (
            f"_capture_bounded took {elapsed:.2f}s with a blocked _feed + over-cap "
            "flood -- a blocked stdin writer must not stall the reap"
        )
        assert capped, "over-cap flood did not set capped"
        assert len(out) <= _MAX_CAPTURE_CHARS
        assert len(err) <= _MAX_CAPTURE_CHARS
    finally:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)
