"""Vectors 2 & 3 -- reap bound under an unkillable/escaped child, and the
stdin/stdout >64KB deadlock surface.

Vector 2: when a grandchild ESCAPES the process group (setsid) AND inherits
the stdout pipe, killpg cannot reach it, so the reap `communicate(timeout=5)`
must NOT block forever -- apm must stay bounded.

Vector 3: a command that both reads stdin and writes >64KB to stdout AND
stderr must not deadlock; communicate drains both pipes concurrently. A
runaway writer under a short timeout must be bounded and killed.
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
from apm_cli.core.script_executors import _execute_command

from .conftest import PYEXE, kill_pid, make_command_entry, pid_alive

# Grandchild that escapes via setsid but KEEPS the inherited stdout pipe
# open while sleeping, so the parent's reap-read could hang on the pipe.
_ESCAPE_HOLDS_PIPE = (
    "import os, sys, time\n"
    "os.setsid()\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "time.sleep(float(sys.argv[2]))\n"  # holds stdout pipe the whole time
)

# Reads all of stdin, then writes a large blob to both stdout and stderr.
_BIG_IO = (
    "import sys\n"
    "_ = sys.stdin.read()\n"
    "n = int(sys.argv[1])\n"
    "sys.stdout.write('x' * n)\n"
    "sys.stderr.write('y' * n)\n"
    "sys.stdout.flush(); sys.stderr.flush()\n"
)


@pytest.mark.slow
def test_reap_is_bounded_when_grandchild_escapes_and_holds_pipe(
    apm_home: Path, tmp_path: Path
) -> None:
    """killpg misses a setsid escapee holding the pipe -> reap must be bounded.

    The escapee keeps the stdout pipe open for 30s; killpg cannot reach it,
    so the post-timeout `communicate(timeout=5)` reap would block on the
    pipe. apm must bound this (timeout + <=5s reap), not hang for 30s.
    """
    worker = tmp_path / "escpipe.py"
    worker.write_text(_ESCAPE_HOLDS_PIPE, encoding="utf-8")
    pidfile = tmp_path / "escpipe.pid"
    # NOTE: stdout is intentionally NOT redirected, so the escapee inherits
    # the captured pipe and can block the reap-read.
    cmd = f'{PYEXE} "{worker}" "{pidfile}" 30'
    script = make_command_entry(cmd, timeout_sec=1)
    event = LifecycleEvent.create(
        event="post-install",
        packages=[PackageInfo(name="rt/pkg", reference="v1")],
        scope="project",
        working_directory=str(tmp_path),
    )

    pid: int | None = None
    try:
        start = time.monotonic()
        _execute_command(script, event, project_root=str(tmp_path))
        elapsed = time.monotonic() - start
        if pidfile.exists():
            pid = int(pidfile.read_text().strip())
    finally:
        if pid is not None:
            kill_pid(pid)
    # 1s timeout + <=5s reap + margin. Must be well under the 30s sleep.
    assert elapsed < 9.0, (
        f"apm hung for {elapsed:.1f}s reaping an escaped pipe-holding child -- "
        "communicate(timeout=...) bound on the reap is missing/ineffective."
    )


def test_big_stdout_stderr_with_stdin_no_deadlock(apm_home: Path, tmp_path: Path) -> None:
    """>64KB on both stdout and stderr while reading stdin must not deadlock."""
    worker = tmp_path / "bigio.py"
    worker.write_text(_BIG_IO, encoding="utf-8")
    # 256KB each way -- far past the ~64KB pipe buffer where naive
    # write-then-read code deadlocks.
    cmd = f'{PYEXE} "{worker}" 262144'
    script = make_command_entry(cmd, timeout_sec=15)
    event = LifecycleEvent.create(
        event="post-install",
        packages=[PackageInfo(name="rt/pkg", reference="v1")],
        scope="project",
        working_directory=str(tmp_path),
    )

    start = time.monotonic()
    _execute_command(script, event, project_root=str(tmp_path))  # must return
    elapsed = time.monotonic() - start
    assert elapsed < 10.0, f"big-IO command did not complete promptly: {elapsed:.1f}s"


@pytest.mark.slow
def test_runaway_writer_is_bounded_and_killed(apm_home: Path, tmp_path: Path) -> None:
    """A script writing forever must hit the timeout and be reaped."""
    pidfile = tmp_path / "runaway.pid"
    runaway = tmp_path / "runaway.py"
    runaway.write_text(
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "while True:\n"
        "    sys.stdout.write('z' * 4096)\n",
        encoding="utf-8",
    )
    cmd = f'{PYEXE} "{runaway}" "{pidfile}"'
    runner = LifecycleScriptRunner(scripts=[make_command_entry(cmd, timeout_sec=1)])
    event = LifecycleEvent.create(
        event="post-install",
        packages=[PackageInfo(name="rt/pkg", reference="v1")],
        scope="project",
        working_directory=str(tmp_path),
    )

    pid: int | None = None
    try:
        start = time.monotonic()
        runner.fire("post-install", event)
        elapsed = time.monotonic() - start
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not pidfile.exists():
            time.sleep(0.05)
        if pidfile.exists():
            pid = int(pidfile.read_text().strip())
            time.sleep(0.3)
            alive = pid_alive(pid)
        else:
            alive = False
    finally:
        if pid is not None:
            kill_pid(pid)
    assert elapsed < 9.0, f"runaway writer not bounded: {elapsed:.1f}s"
    assert not alive, "runaway writer survived its timeout -- not reaped"
