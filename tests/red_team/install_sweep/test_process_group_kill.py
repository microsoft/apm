"""Vector 1 -- process-group kill robustness (attack the killpg fix).

The round-1 fix put the shell in its own session (start_new_session) and
reaps the whole group with SIGKILL on timeout. Here we attack THAT fix:

- a script that ignores SIGTERM / traps signals -> SIGKILL on the group
  must still reap it (SIGKILL is uncatchable).
- a grandchild that double-forks and re-parents to init/launchd but does
  NOT call setsid -> stays in the group -> must be reaped.
- a grandchild that escapes via os.setsid() -> known OS limitation: it
  survives, but apm MUST NOT crash and MUST stay bounded (LOW observation,
  not a HIGH break).
- _kill_process_group on an already-dead proc must never leak
  ProcessLookupError / PermissionError into the install flow.

Every test reaps its own descendants in a finally block.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from apm_cli.core.lifecycle_scripts import (
    LifecycleEvent,
    LifecycleScriptRunner,
    PackageInfo,
)

from .conftest import PYEXE, kill_pid, make_command_entry, pid_alive

# Child that ignores SIGTERM + traps SIGINT, records its pid, sleeps long.
_SIGNAL_IGNORER = (
    "import os, sys, signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "time.sleep(float(sys.argv[2]))\n"
)

# Three-level reparent: A keeps the foreground alive past the timeout; B
# forks C then exits so C re-parents to init/launchd. None call setsid, so
# C keeps the shell's pgid and the TIMEOUT-path killpg must reach it.
_REPARENT_TIMEOUT = (
    "import os, sys, time\n"
    "dur = float(sys.argv[2])\n"
    "b = os.fork()\n"
    "if b == 0:\n"
    "    c = os.fork()\n"
    "    if c == 0:\n"
    "        open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "        time.sleep(dur)\n"
    "        os._exit(0)\n"
    "    os._exit(0)\n"  # B exits -> C reparents, still in the shell's group
    "time.sleep(dur)\n"  # A holds the foreground so the timeout actually fires
)

# Fast-exit foreground that backgrounds a grandchild via '&'. The shell
# returns in well under the timeout, so the kill path NEVER runs.
_BACKGROUND_FASTEXIT = (
    "import os, sys, time\n"
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "    time.sleep(float(sys.argv[2]))\n"
    "    os._exit(0)\n"
    "os._exit(0)\n"  # foreground exits immediately -> no timeout, no killpg
)

# Grandchild that escapes the group with its OWN new session.
_SETSID_ESCAPE = (
    "import os, sys, time\n"
    "os.setsid()\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "time.sleep(float(sys.argv[2]))\n"
)


def _fire_one(cmd: str, working: Path, timeout_sec: int = 1) -> None:
    runner = LifecycleScriptRunner(scripts=[make_command_entry(cmd, timeout_sec=timeout_sec)])
    event = LifecycleEvent.create(
        event="post-install",
        packages=[PackageInfo(name="rt/pkg", reference="v1")],
        scope="project",
        working_directory=str(working),
    )
    runner.fire("post-install", event)


def _read_pid(pidfile: Path, deadline_s: float = 3.0) -> int:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end and not pidfile.exists():
        time.sleep(0.05)
    assert pidfile.exists(), "descendant never started -- test setup failure"
    return int(pidfile.read_text().strip())


@pytest.mark.slow
def test_sigterm_ignoring_child_is_killed(apm_home: Path, tmp_path: Path) -> None:
    """A child that ignores SIGTERM must still die: killpg uses SIGKILL."""
    worker = tmp_path / "ignorer.py"
    worker.write_text(_SIGNAL_IGNORER, encoding="utf-8")
    pidfile = tmp_path / "ignorer.pid"
    cmd = f'{PYEXE} "{worker}" "{pidfile}" 8 >/dev/null 2>&1'

    pid: int | None = None
    try:
        _fire_one(cmd, tmp_path)
        pid = _read_pid(pidfile)
        time.sleep(0.4)
        alive = pid_alive(pid)
    finally:
        if pid is not None:
            kill_pid(pid)
    assert not alive, (
        "SIGTERM-ignoring child survived timeout -- killpg must send SIGKILL "
        "(uncatchable), not SIGTERM."
    )


@pytest.mark.slow
def test_double_forked_reparented_grandchild_is_killed(apm_home: Path, tmp_path: Path) -> None:
    """Reparented grandchild (same pgid) must be reaped by the timeout killpg.

    The foreground process stays alive past the timeout so the kill path
    actually fires; the grandchild has re-parented to init but kept the
    shell's process group, so killpg must still reach it.
    """
    worker = tmp_path / "dfork.py"
    worker.write_text(_REPARENT_TIMEOUT, encoding="utf-8")
    pidfile = tmp_path / "dfork.pid"
    cmd = f'{PYEXE} "{worker}" "{pidfile}" 8 >/dev/null 2>&1'

    pid: int | None = None
    try:
        _fire_one(cmd, tmp_path)
        pid = _read_pid(pidfile)
        time.sleep(0.4)
        alive = pid_alive(pid)
    finally:
        if pid is not None:
            kill_pid(pid)
    assert not alive, (
        "Reparented (double-forked) grandchild survived -- it kept the shell's "
        "process group, so the timeout killpg should have reaped it."
    )


@pytest.mark.slow
def test_backgrounded_grandchild_survives_normal_completion(apm_home: Path, tmp_path: Path) -> None:
    """OBSERVATION (not a break): a fast-exiting script that backgrounds a
    grandchild leaks it on NORMAL completion -- the group is only reaped on
    timeout/exception, never on clean exit.

    This is standard POSIX job-control semantics (npm/yarn behave the same:
    a lifecycle script may legitimately start a daemon). It is NOT a
    'surviving process after timeout' because no timeout occurs. Documented
    here so the boundary is explicit; the test asserts the current,
    intentional behaviour and reaps the grandchild itself.
    """
    worker = tmp_path / "bgexit.py"
    worker.write_text(_BACKGROUND_FASTEXIT, encoding="utf-8")
    pidfile = tmp_path / "bgexit.pid"
    cmd = f'{PYEXE} "{worker}" "{pidfile}" 8 >/dev/null 2>&1'

    pid: int | None = None
    try:
        _fire_one(cmd, tmp_path, timeout_sec=5)  # generous timeout; never hit
        pid = _read_pid(pidfile)
        time.sleep(0.3)
        alive = pid_alive(pid)
    finally:
        if pid is not None:
            kill_pid(pid)
    assert alive, (
        "Expected the backgrounded grandchild to survive normal completion "
        "(documented POSIX job-control behaviour). If this now fails, apm "
        "started reaping background daemons on clean exit -- re-evaluate."
    )


@pytest.mark.slow
def test_setsid_escapee_does_not_crash_apm(apm_home: Path, tmp_path: Path) -> None:
    """A grandchild that creates its OWN session escapes killpg.

    Documented OS limitation (LOW): killpg targets the original group, so
    a brand-new session is unreachable. The contract apm MUST uphold is
    that this does NOT crash apm and stays bounded. We assert apm returns
    cleanly; the escapee's survival is the known, accepted gap.
    """
    worker = tmp_path / "escape.py"
    worker.write_text(_SETSID_ESCAPE, encoding="utf-8")
    pidfile = tmp_path / "escape.pid"
    cmd = f'{PYEXE} "{worker}" "{pidfile}" 8 >/dev/null 2>&1'

    pid: int | None = None
    try:
        start = time.monotonic()
        _fire_one(cmd, tmp_path)  # must not raise
        elapsed = time.monotonic() - start
        pid = _read_pid(pidfile)
    finally:
        if pid is not None:
            kill_pid(pid)
    # apm bounded: timeout(1s) + reap(<=5s) + margin.
    assert elapsed < 9.0, f"apm not bounded on setsid escape: {elapsed:.1f}s"


def test_kill_process_group_on_dead_proc_is_silent(apm_home: Path, tmp_path: Path) -> None:
    """_kill_process_group on an already-exited proc must not raise."""
    import subprocess

    from apm_cli.core.script_executors import _kill_process_group

    proc = subprocess.Popen(
        f'{PYEXE} -c "pass"',
        shell=True,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    proc.wait(timeout=5)  # fully exited; pgid lookup will race/ProcessLookupError
    # Must swallow ProcessLookupError/PermissionError/OSError.
    _kill_process_group(proc)
    _kill_process_group(None)  # None guard


def test_kill_process_group_no_zombie_left(apm_home: Path, tmp_path: Path) -> None:
    """After killpg the direct child is reaped (returncode set, no zombie)."""
    import subprocess

    from apm_cli.core.script_executors import _kill_process_group

    proc = subprocess.Popen(
        f'{PYEXE} -c "import time; time.sleep(30)"',
        shell=True,
        start_new_session=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _kill_process_group(proc)
        # communicate(timeout=5) inside the reap must have set returncode.
        assert proc.returncode is not None, "child not reaped -- zombie risk"
        assert not pid_alive(proc.pid), "child still alive after killpg+reap"
    finally:
        kill_pid(proc.pid)
