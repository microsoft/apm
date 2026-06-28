"""Fixtures + helpers for the fresh install-path adversarial sweep.

Hermetic: APM_HOME redirected into tmp_path so the trust store and the
scripts.log live inside the sandbox. Real subprocesses / forks are used
where the attack demands it; every such test reaps its own pids/processes
in a finally block so the suite never leaks a process of its own.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from pathlib import Path

import pytest

from apm_cli.core.lifecycle_scripts import ScriptEntry
from apm_cli.utils.yaml_io import dump_yaml

PYEXE = sys.executable


@pytest.fixture
def apm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect APM_HOME into the sandbox and clear APM_NO_SCRIPTS."""
    home = tmp_path / "apm_home"
    home.mkdir()
    monkeypatch.setenv("APM_HOME", str(home))
    monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
    return home


def make_command_entry(
    command: str,
    event: str = "post-install",
    *,
    source: str = "user",
    timeout_sec: int | None = None,
    cwd: str | None = None,
) -> ScriptEntry:
    """Build a command ScriptEntry directly (source=user bypasses the gate)."""
    return ScriptEntry(
        script_type="command",
        event=event,
        command=command,
        timeout_sec=timeout_sec,
        cwd=cwd,
        source=source,
    )


def write_project(project: Path, event: str, commands: list[str]) -> Path:
    """Write an apm.yml under *project* with command scripts for *event*."""
    project.mkdir(parents=True, exist_ok=True)
    lifecycle = {event: [{"type": "command", "run": cmd} for cmd in commands]}
    apm_yml = project / "apm.yml"
    dump_yaml({"name": "rt-pkg", "version": "0.0.0", "lifecycle": lifecycle}, apm_yml)
    return apm_yml


def append_cmd(target: Path, token: str) -> str:
    """Command-script string that appends *token*+newline to *target*."""
    return (
        f'{PYEXE} -c "import sys; '
        f'open(sys.argv[1], chr(97)).write(sys.argv[2]+chr(10))" '
        f'"{target}" "{token}"'
    )


def pid_alive(pid: int) -> bool:
    """True if *pid* refers to a live (non-reaped) process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_pid(pid: int) -> None:
    """Best-effort SIGKILL; never raises."""
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)
