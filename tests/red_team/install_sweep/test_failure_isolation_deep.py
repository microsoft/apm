"""Vector 4 -- failure isolation + deterministic ordering under chaos.

Round-1 covered nonzero-exit / NUL-byte / missing-cwd / raising-executor /
timeout-in-middle. Here we push harder:

- a middle script that SIGKILLs ITSELF (returncode -9) must not abort
  neighbours.
- a timeout in slot 1 must run slots 2 and 3 exactly once each, in order
  (no skip, no double-run).
- command scripts run in declared order even when interleaved with a
  crashing one.
"""

from __future__ import annotations

import time
from pathlib import Path

from apm_cli.core.lifecycle_scripts import (
    LifecycleEvent,
    LifecycleScriptRunner,
    PackageInfo,
)

from .conftest import PYEXE, append_cmd, kill_pid, make_command_entry, pid_alive

_SELF_SIGKILL = f'{PYEXE} -c "import os,signal; os.kill(os.getpid(), signal.SIGKILL)"'


def _fire(scripts: list, working: Path) -> None:
    runner = LifecycleScriptRunner(scripts=scripts)
    event = LifecycleEvent.create(
        event="post-install",
        packages=[PackageInfo(name="rt/pkg", reference="v1")],
        scope="project",
        working_directory=str(working),
    )
    runner.fire("post-install", event)


def test_self_sigkill_middle_isolated(apm_home: Path, tmp_path: Path) -> None:
    """A middle script that SIGKILLs itself must not stop its neighbours."""
    order = tmp_path / "order.log"
    scripts = [
        make_command_entry(append_cmd(order, "one")),
        make_command_entry(_SELF_SIGKILL),
        make_command_entry(append_cmd(order, "three")),
    ]
    _fire(scripts, tmp_path)
    tokens = order.read_text().split() if order.exists() else []
    assert tokens == ["one", "three"], (
        f"self-SIGKILL broke isolation/ordering: got {tokens!r}, expected "
        "['one', 'three'] (each neighbour exactly once, in order)."
    )


def test_timeout_slot1_runs_remaining_once_in_order(apm_home: Path, tmp_path: Path) -> None:
    """A timeout in slot 1 must run slots 2 and 3 exactly once, in order."""
    order = tmp_path / "order.log"
    pidfile = tmp_path / "slow.pid"
    slow = tmp_path / "slow.py"
    slow.write_text(
        "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(8)\n",
        encoding="utf-8",
    )
    slow_cmd = f'{PYEXE} "{slow}" "{pidfile}" >/dev/null 2>&1'
    scripts = [
        make_command_entry(slow_cmd, timeout_sec=1),
        make_command_entry(append_cmd(order, "two")),
        make_command_entry(append_cmd(order, "three")),
    ]

    pid: int | None = None
    try:
        start = time.monotonic()
        _fire(scripts, tmp_path)
        elapsed = time.monotonic() - start
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not pidfile.exists():
            time.sleep(0.05)
        if pidfile.exists():
            pid = int(pidfile.read_text().strip())
            time.sleep(0.3)
            slow_alive = pid_alive(pid)
        else:
            slow_alive = False
    finally:
        if pid is not None:
            kill_pid(pid)

    tokens = order.read_text().split() if order.exists() else []
    assert tokens == ["two", "three"], (
        f"timeout in slot 1 corrupted neighbour run: got {tokens!r}, "
        "expected ['two', 'three'] exactly once each, in order."
    )
    assert not slow_alive, "slot-1 timeout process survived -- not reaped"
    # The whole event should be bounded by the single 1s timeout, not stack.
    assert elapsed < 6.0, f"event not bounded after slot-1 timeout: {elapsed:.1f}s"


def test_many_scripts_one_crasher_all_others_run(apm_home: Path, tmp_path: Path) -> None:
    """N scripts, the k-th SIGKILLs itself -> the other N-1 all run, in order."""
    order = tmp_path / "order.log"
    n = 6
    crash_at = 3
    scripts = []
    for i in range(n):
        if i == crash_at:
            scripts.append(make_command_entry(_SELF_SIGKILL))
        else:
            scripts.append(make_command_entry(append_cmd(order, f"s{i}")))
    _fire(scripts, tmp_path)
    tokens = order.read_text().split() if order.exists() else []
    expected = [f"s{i}" for i in range(n) if i != crash_at]
    assert tokens == expected, (
        f"crasher at slot {crash_at} broke isolation: got {tokens!r}, expected {expected!r}."
    )
