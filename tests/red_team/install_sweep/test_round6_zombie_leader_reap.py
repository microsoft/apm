"""Round-6 (r6-install-1) trap: reap a grandchild whose shell leader is a zombie.

The round-1 kill path read the group id with ``os.getpgid(proc.pid)``. Once the
shell leader exits -- e.g. ``sleep 30 & echo done`` returns the shell almost
immediately while the ``&``-backgrounded grandchild keeps running -- the leader
becomes a zombie and ``os.getpgid`` raises ``ProcessLookupError``. The code then
fell through to ``proc.kill()``, which only signalled the already-dead leader
and STRANDED the live grandchild (a leaked background process the timeout reap
was supposed to clean up).

The fix calls ``os.killpg(proc.pid, SIGKILL)`` directly: under
``start_new_session=True`` the process-group id equals the leader pid and the
group persists while ANY member is alive, so the grandchild is reaped even
though the leader is a zombie. This is deterministic and macOS-safe (no
external ``timeout``/``ulimit``); each test reaps its own pids.
"""

from __future__ import annotations

import contextlib
import subprocess
import time
from pathlib import Path

from apm_cli.core.script_executors import _kill_process_group

from .conftest import PYEXE, kill_pid, pid_alive


def _spawn_zombie_leader_with_grandchild(pidfile: Path) -> subprocess.Popen:
    """Shell that backgrounds a long grandchild then exits -> leader zombies."""
    worker = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(30)"
    cmd = f'{PYEXE} -c "{worker}" "{pidfile}" & echo started; exit 0'
    return subprocess.Popen(
        cmd,
        shell=True,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _read_pid(pidfile: Path, deadline_s: float = 3.0) -> int:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end and not pidfile.exists():
        time.sleep(0.05)
    assert pidfile.exists(), "grandchild never started -- test setup failure"
    return int(pidfile.read_text().strip())


def test_backgrounded_grandchild_reaped_when_leader_is_zombie(tmp_path: Path) -> None:
    pidfile = tmp_path / "gc.pid"
    proc = _spawn_zombie_leader_with_grandchild(pidfile)
    gc_pid: int | None = None
    try:
        gc_pid = _read_pid(pidfile)
        # Let the shell leader exit and become a zombie; grandchild stays alive.
        time.sleep(0.6)
        assert pid_alive(gc_pid), "grandchild died on its own -- setup failure"

        _kill_process_group(proc)
        time.sleep(0.4)
        assert not pid_alive(gc_pid), (
            "BREAK: backgrounded grandchild leaked -- killpg(proc.pid) must reap "
            "the group even when the shell leader is already a zombie."
        )
    finally:
        if gc_pid is not None:
            kill_pid(gc_pid)
        with contextlib.suppress(Exception):
            proc.wait(timeout=2)


def test_kill_process_group_does_not_raise_on_zombie_leader(tmp_path: Path) -> None:
    """The reap must swallow any ProcessLookupError from the zombie leader."""
    pidfile = tmp_path / "gc2.pid"
    proc = _spawn_zombie_leader_with_grandchild(pidfile)
    gc_pid: int | None = None
    try:
        gc_pid = _read_pid(pidfile)
        time.sleep(0.6)
        _kill_process_group(proc)  # must not raise
    finally:
        if gc_pid is not None:
            kill_pid(gc_pid)
        with contextlib.suppress(Exception):
            proc.wait(timeout=2)
