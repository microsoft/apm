"""Vector 3 -- process-lifetime / orphan leak (high-value).

shell=True without start_new_session means subprocess.run's timeout kill
targets the shell only. A worker the shell spawned is reparented and
survives past the timeout -> an ORPHANED process leak.

The SECURE expectation is that the worker is dead once the script's
timeout has fired. On head code it is still alive, so this test FAILS --
a genuine break. Proposed fix: start_new_session=True + os.killpg() on
TimeoutExpired.

The test always reaps the worker in a finally block, so it never leaks
a process of its own.
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

_GRANDCHILD = (
    "import os, sys, time\n"
    "with open(sys.argv[1], 'w') as f:\n"
    "    f.write(str(os.getpid()))\n"
    "time.sleep(float(sys.argv[2]))\n"
)


@pytest.mark.slow
def test_orphan_survives_timeout(apm_home: Path, tmp_path: Path) -> None:
    worker = tmp_path / "grandchild.py"
    worker.write_text(_GRANDCHILD, encoding="utf-8")
    pidfile = tmp_path / "worker.pid"

    # Redirect the worker's std streams off the captured pipe so the
    # timeout path is not itself blocked by the orphan holding the pipe.
    cmd = f'{PYEXE} "{worker}" "{pidfile}" 8 >/dev/null 2>&1'
    runner = LifecycleScriptRunner(scripts=[make_command_entry(cmd, timeout_sec=1)])
    event = LifecycleEvent.create(
        event="post-install",
        packages=[PackageInfo(name="rt/pkg", reference="v1")],
        scope="project",
        working_directory=str(tmp_path),
    )

    pid: int | None = None
    try:
        runner.fire("post-install", event)  # returns after the 1s timeout

        # The worker writes its PID immediately on start.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not pidfile.exists():
            time.sleep(0.05)
        assert pidfile.exists(), "worker never started -- test setup failure"
        pid = int(pidfile.read_text().strip())

        # Give the kill path a beat to act (it should have, on a secure impl).
        time.sleep(0.3)
        alive = pid_alive(pid)
    finally:
        if pid is not None:
            kill_pid(pid)

    assert not alive, (
        "ORPHAN LEAK: worker spawned by a timed-out command script is still "
        "alive after the timeout. shell=True needs start_new_session + killpg."
    )
