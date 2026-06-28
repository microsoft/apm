"""Shared fixtures + helpers for the install red-team firing suite.

Everything here is HERMETIC: APM_HOME is redirected into tmp_path so the
trust store, the user apm.yml tier, and ~/.apm/logs/scripts.log all live
inside the test sandbox. No network: org-policy discovery is stubbed to
None by default (the deny_all attack stubs it explicitly).
"""

from __future__ import annotations

import os
import signal
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.core.lifecycle_scripts import (
    LifecycleEvent,
    PackageInfo,
    ScriptEntry,
    build_runner_from_context,
)
from apm_cli.core.script_trust import trust_project_scripts
from apm_cli.utils.yaml_io import dump_yaml

# Python executable used to build hermetic, cross-process command scripts.
PYEXE = sys.executable


@pytest.fixture(autouse=True)
def _neutralize_guarded_session():
    """Route HTTP dispatch back through the mockable ``requests.post``.

    Production wraps dispatch in a DNS-pinned ``requests.Session`` (the
    round-2 rebinding fix), which bypasses ``monkeypatch.setattr(requests,
    "post", ...)``. Forcing ``_get_guarded_session`` AND
    ``_get_capturing_session`` to ``None`` keeps the firing-path tests
    hermetic; the pin is covered directly in
    ``http_sweep/test_dns_rebinding_pinned.py`` and the capturing-session
    socket force-close in ``http_sweep/test_round23_semaphore_starvation.py``.
    """
    from apm_cli.core import script_executors

    with (
        patch.object(script_executors, "_get_guarded_session", return_value=None),
        patch.object(script_executors, "_get_capturing_session", return_value=None),
    ):
        yield


@pytest.fixture
def apm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect APM_HOME into the sandbox and clear APM_NO_SCRIPTS."""
    home = tmp_path / "apm_home"
    home.mkdir()
    monkeypatch.setenv("APM_HOME", str(home))
    monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
    return home


def touch_cmd(sentinel: Path) -> str:
    """A command-script string that creates *sentinel* with no inner quotes."""
    return (
        f'{PYEXE} -c "import sys,pathlib; '
        f'pathlib.Path(sys.argv[1]).write_text(chr(120))" "{sentinel}"'
    )


def append_cmd(target: Path, token: str) -> str:
    """A command-script string that appends *token* + newline to *target*.

    Opens in append mode (chr(97) == 'a') so concurrent runs accumulate;
    no inner quotes so it survives YAML + shell round-tripping.
    """
    return (
        f'{PYEXE} -c "import sys; '
        f'open(sys.argv[1], chr(97)).write(sys.argv[2]+chr(10))" '
        f'"{target}" "{token}"'
    )


def write_project(project: Path, event: str, commands: list[str]) -> Path:
    """Write an apm.yml under *project* with command scripts for *event*.

    Returns the apm.yml path. Uses dump_yaml so command strings (which
    contain quotes and colons) are serialised safely.
    """
    project.mkdir(parents=True, exist_ok=True)
    lifecycle = {event: [{"type": "command", "run": cmd} for cmd in commands]}
    apm_yml = project / "apm.yml"
    dump_yaml({"name": "rt-pkg", "version": "0.0.0", "lifecycle": lifecycle}, apm_yml)
    return apm_yml


def trust(apm_yml: Path) -> None:
    """Record trust for the lifecycle: subtree (APM_HOME already redirected)."""
    trust_project_scripts(apm_yml)


@contextmanager
def stub_policy(deny_all: bool = False):
    """Patch org-policy discovery used by build_runner_from_context.

    Default returns None (no org policy). With deny_all=True returns a
    minimal object exposing .policy.executables.deny_all == True.
    """
    if not deny_all:
        with patch(
            "apm_cli.policy.discovery.discover_policy_with_chain",
            return_value=None,
        ):
            yield
        return

    class _Exec:
        deny_all = True

    class _Pol:
        executables = _Exec()

    class _Result:
        policy = _Pol()

    with patch(
        "apm_cli.policy.discovery.discover_policy_with_chain",
        return_value=_Result(),
    ):
        yield


def fire_via_context(
    project_root: Path,
    event: str = "post-install",
    *,
    deny_all: bool = False,
    logger: object = None,
) -> list:
    """Build a runner on the REAL firing path and fire *event*.

    Joins any HTTP daemon threads so the call is deterministic.
    """
    with stub_policy(deny_all=deny_all):
        runner = build_runner_from_context(project_root=str(project_root), logger=logger)
    evt = LifecycleEvent.create(
        event=event,
        packages=[PackageInfo(name="rt/pkg", reference="v1")],
        scope="project",
        working_directory=str(project_root),
    )
    threads = runner.fire(event, evt)
    for t in threads:
        t.join(timeout=10)
    return threads


def make_command_entry(
    command: str,
    event: str = "post-install",
    *,
    source: str = "user",
    timeout_sec: int | None = None,
    cwd: str | None = None,
) -> ScriptEntry:
    """Build a command ScriptEntry directly (source=user bypasses the gate).

    Used by isolation / orphan / concurrency attacks that need precise
    control over the script set while still exercising the real fire()
    + executor path.
    """
    return ScriptEntry(
        script_type="command",
        event=event,
        command=command,
        timeout_sec=timeout_sec,
        cwd=cwd,
        source=source,
    )


def pid_alive(pid: int) -> bool:
    """True if *pid* refers to a live process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_pid(pid: int) -> None:
    """Best-effort SIGKILL; never raises."""
    import contextlib

    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)
