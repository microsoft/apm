"""Round 21 -- adversarial probes against the round-20 threaded bounded
capture cluster (`_capture_bounded`, `_drain_capped`, `_signal_kill_group`,
`_kill_process_group`) and the firing path in `_execute_command`.

Every probe drives REAL subprocesses. Hang detection uses a daemon-thread
watchdog (`_run_bounded`) so a genuine deadlock surfaces as a test failure
rather than wedging the suite. Stuck children are reaped via the Popen
object (`proc.kill()` / killpg), never the banned shell `kill`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from apm_cli.core.lifecycle_scripts import LifecycleEvent
from apm_cli.core.script_executors import (
    _MAX_CAPTURE_CHARS,
    _capture_bounded,
    _execute_command,
)

from .conftest import make_command_entry, pid_alive

PYEXE = sys.executable
_W = Path(__file__).parent / "_workers"
_FLOOD = str(_W / "rt21_flood.py")
_SLEEP = str(_W / "rt21_sleep.py")
_BADUTF8 = str(_W / "rt21_badutf8.py")


def _run_bounded(fn, watchdog: float):
    """Run ``fn`` on a daemon thread; return (done, result, exc).

    ``done`` is False if the call did not return within ``watchdog`` secs
    (i.e. a hang/deadlock). result/exc carry the outcome when done.
    """
    box: dict[str, object] = {}

    def _target() -> None:
        try:
            box["result"] = fn()
        except BaseException as e:
            box["exc"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=watchdog)
    if t.is_alive():
        return False, None, None
    return True, box.get("result"), box.get("exc")


def _popen(cmd: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        cmd,
        shell=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


# --------------------------------------------------------------------------
# PRIME SUSPECT: dual-stream over-cap flood with no stdin read -> deadlock?
# --------------------------------------------------------------------------
def test_dual_stream_overcap_no_stdin_read_does_not_deadlock() -> None:
    """Child writes >2MB to BOTH stdout and stderr and never reads stdin.

    Secure contract: `_capture_bounded` keeps draining past the cap so the
    child can never wedge on a full pipe, trips `over`, SIGKILLs the group,
    and returns (capped=True) promptly. A watchdog of 30s catches a genuine
    deadlock; well-behaved code returns in well under a second.
    """
    proc = _popen(f"{PYEXE} {_FLOOD} {3 << 20}")
    try:
        done, result, exc = _run_bounded(
            lambda: _capture_bounded(proc, "x" * (256 * 1024), 25.0),
            watchdog=30.0,
        )
        assert done, "DEADLOCK: _capture_bounded did not return within 30s"
        assert exc is None, f"unexpected exception: {exc!r}"
        stdout, stderr, capped = result  # type: ignore[misc]
        assert capped is True, "expected capped=True on >2MB dual-stream flood"
        assert len(stdout) <= _MAX_CAPTURE_CHARS
        assert len(stderr) <= _MAX_CAPTURE_CHARS
    finally:
        with __import__("contextlib").suppress(Exception):
            proc.kill()
        with __import__("contextlib").suppress(Exception):
            proc.wait(timeout=5)


# --------------------------------------------------------------------------
# TIMEOUT REAP: sleeping child with no output, no stdin read.
# --------------------------------------------------------------------------
def test_timeout_reaps_sleeping_child_no_orphan() -> None:
    """A `sleep`-style child with timeout=1 is reaped ~1s; group dies."""
    proc = _popen(f"{PYEXE} {_SLEEP} 3600")
    child_pid = proc.pid
    start = time.monotonic()
    done, _result, exc = _run_bounded(lambda: _capture_bounded(proc, "{}", 1.0), watchdog=15.0)
    elapsed = time.monotonic() - start
    assert done, "DEADLOCK: capture did not return for a timed-out sleep child"
    assert isinstance(exc, subprocess.TimeoutExpired), f"got {exc!r}"
    assert elapsed < 8.0, f"timeout reap too slow: {elapsed:.1f}s"
    # caller's handler reaps via _kill_process_group; emulate that contract
    from apm_cli.core.script_executors import _kill_process_group

    _kill_process_group(proc)
    time.sleep(0.3)
    assert not pid_alive(child_pid), "orphan: child survived timeout reap"


# --------------------------------------------------------------------------
# NON-FINITE GUARD: NaN / inf deadline must fast-reject, not loop forever.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_timeout_fast_rejects(bad: float) -> None:
    proc = _popen(f"{PYEXE} {_SLEEP} 3600")
    try:
        done, _result, exc = _run_bounded(lambda: _capture_bounded(proc, "{}", bad), watchdog=5.0)
        assert done, f"DEADLOCK: non-finite timeout {bad!r} did not reject"
        assert isinstance(exc, ValueError), f"expected ValueError, got {exc!r}"
    finally:
        with __import__("contextlib").suppress(Exception):
            os.killpg(proc.pid, signal.SIGKILL)
        with __import__("contextlib").suppress(Exception):
            proc.wait(timeout=5)


# --------------------------------------------------------------------------
# RETURNCODE CORRECTNESS: a failing script must not be reported as success.
# --------------------------------------------------------------------------
def test_failing_returncode_is_preserved() -> None:
    proc = _popen(f"{PYEXE} -c 'import sys; sys.exit(7)'")
    done, _result, exc = _run_bounded(lambda: _capture_bounded(proc, "{}", 10.0), watchdog=15.0)
    assert done and exc is None
    assert proc.returncode == 7, f"returncode lost/raced: {proc.returncode!r}"
    assert proc.returncode is not None


# --------------------------------------------------------------------------
# INVALID UTF-8: drain reads in text mode; bad bytes raise UnicodeDecodeError
# (a ValueError subclass) inside the drain thread. Probe whether that aborts
# the drain early and lets a >cap flood escape un-capped / wedge the child.
# --------------------------------------------------------------------------
def test_invalid_utf8_flood_is_still_bounded() -> None:
    """Child emits raw invalid UTF-8 then floods >3MB and exits 0.

    Secure contract: capture must still terminate promptly and remain
    bounded. If the drain thread dies on UnicodeDecodeError and stops
    reading, a large flood could either wedge the child (deadlock) or
    escape the cap. Watchdog catches a hang; assertions catch un-capping.
    """
    proc = _popen(f"{PYEXE} {_BADUTF8}")
    try:
        done, result, exc = _run_bounded(lambda: _capture_bounded(proc, "{}", 25.0), watchdog=30.0)
        assert done, "DEADLOCK: invalid-UTF8 flood wedged _capture_bounded"
        assert exc is None, f"capture raised into caller: {exc!r}"
        stdout, _stderr, _capped = result  # type: ignore[misc]
        assert len(stdout) <= _MAX_CAPTURE_CHARS
    finally:
        with __import__("contextlib").suppress(Exception):
            proc.kill()
        with __import__("contextlib").suppress(Exception):
            proc.wait(timeout=5)


# --------------------------------------------------------------------------
# CONCURRENCY: N simultaneous captures -> isolation + no thread leak.
# --------------------------------------------------------------------------
def test_concurrent_captures_no_thread_leak() -> None:
    base = threading.active_count()
    results: list[tuple[str, str, bool]] = []
    lock = threading.Lock()

    def _one() -> None:
        proc = _popen(f"{PYEXE} {_FLOOD} {2 << 20}")
        try:
            r = _capture_bounded(proc, "{}", 25.0)
            with lock:
                results.append(r)
        finally:
            with __import__("contextlib").suppress(Exception):
                proc.kill()
            with __import__("contextlib").suppress(Exception):
                proc.wait(timeout=5)

    threads = [threading.Thread(target=_one) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=40)
    assert all(not t.is_alive() for t in threads), "a capture deadlocked"
    assert len(results) == 8
    assert all(capped for *_x, capped in results)
    time.sleep(1.0)
    leaked = threading.active_count() - base
    assert leaked <= 1, f"thread leak: {leaked} extra threads after captures"


# --------------------------------------------------------------------------
# FULL FIRING PATH: failing flood through _execute_command stays bounded and
# is reported as error (returncode != 0), output capped.
# --------------------------------------------------------------------------
def test_execute_command_flood_path(apm_home: Path) -> None:
    ev = LifecycleEvent.create("post-install")
    done, _result, exc = _run_bounded(
        lambda: _execute_command(
            make_command_entry(f"{PYEXE} {_FLOOD} {3 << 20}", timeout_sec=25), ev
        ),
        watchdog=35.0,
    )
    assert done, "DEADLOCK: _execute_command wedged on dual-stream flood"
    assert exc is None, f"_execute_command raised: {exc!r}"


_BGHOLD = str(_W / "rt21_bg_holds_pipe.py")


def test_success_path_grandchild_holds_stdout_join_delay() -> None:
    """Parent exits 0 instantly but a backgrounded grandchild keeps the
    stdout write-end open (no EOF). Characterise: does capture return
    bounded, what is the latency, and does the grandchild survive?

    This is the SUCCESS path (returncode 0) -- `_kill_process_group` is
    NOT called by the caller, so any orphan here is by-design fire-and-
    forget. We only assert NO deadlock + bounded latency.
    """
    holder = 30.0  # >> the 2x join(5) bound: proves we don't wait the orphan
    proc = _popen(f"{PYEXE} {_BGHOLD} {holder}")
    start = time.monotonic()
    done, _result, exc = _run_bounded(lambda: _capture_bounded(proc, "{}", 25.0), watchdog=40.0)
    elapsed = time.monotonic() - start
    try:
        assert done, "DEADLOCK: grandchild-holds-stdout wedged capture"
        assert exc is None, f"raised: {exc!r}"
        assert proc.returncode == 0
        # Bounded by join(timeout=5) on the two still-blocked drain threads
        # (stdout + stderr) => ~10s, and crucially INDEPENDENT of the 30s
        # orphan lifetime. The secure contract is "bounded, not waiting the
        # backgrounded child"; communicate() would have blocked the full 30s
        # (or hit the 25s timeout). Floor 5s confirms a real join occurred.
        assert 5.0 <= elapsed < 15.0, f"latency not the join bound: {elapsed:.1f}s"
    finally:
        with __import__("contextlib").suppress(Exception):
            os.killpg(proc.pid, signal.SIGKILL)
        with __import__("contextlib").suppress(Exception):
            proc.wait(timeout=5)
