"""Shared fixtures for the parser_sweep red-team round-2 chaos suite.

All helpers are hermetic: they write into ``tmp_path`` and never touch the
real ``/etc/apm/policy.d`` or the user's home directory. ``run_guarded``
runs a callable in a daemon thread and fails fast if it does not finish
within a bound, so a pathological parse (hang / DoS) is caught instead of
wedging CI.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest


def write_bytes(path: Path, data: bytes) -> Path:
    """Write raw bytes to *path* (for non-UTF8 / control-char fixtures)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_apm_yml_bytes(root: Path, data: bytes) -> Path:
    """Write a raw-bytes ``apm.yml`` at *root* and return its path."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / "apm.yml"
    target.write_bytes(data)
    return target


def write_apm_yml(root: Path, content: str) -> Path:
    """Write a UTF-8 ``apm.yml`` containing *content* and return its path."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / "apm.yml"
    target.write_text(content, encoding="utf-8")
    return target


def run_guarded(fn, timeout: float = 8.0):
    """Run *fn* in a daemon thread with a wall-clock bound.

    Returns ``(finished, result, exception)``. ``finished`` is False if the
    call did not complete within *timeout* -- the signal a parser hang has
    occurred. The worker is a daemon thread, so a wedged call cannot keep
    the test process alive.
    """
    box: dict[str, object] = {}

    def _worker() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:
            box["exception"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout)
    finished = not thread.is_alive()
    return finished, box.get("result"), box.get("exception")


@pytest.fixture()
def policy_dir(tmp_path, monkeypatch):
    """A hermetic admin-policy directory wired into discovery + validate."""
    from apm_cli.core import lifecycle_scripts

    pdir = tmp_path / "policy.d"
    pdir.mkdir()
    monkeypatch.setattr(lifecycle_scripts, "_get_policy_scripts_dir", lambda: pdir)
    return pdir


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Point APM_HOME at a tmp dir with no user apm.yml."""
    home = tmp_path / "apm_home"
    home.mkdir()
    monkeypatch.setenv("APM_HOME", str(home))
    return home


@pytest.fixture()
def fire_event(tmp_path):
    """A synthetic LifecycleEvent for firing probes."""
    from apm_cli.core.lifecycle_scripts import LifecycleEvent

    return LifecycleEvent(event="post-install", working_directory=str(tmp_path))


def command_entry(**overrides):
    """Build a command ScriptEntry with sane defaults, overridable."""
    from apm_cli.core.lifecycle_scripts import ScriptEntry

    base = dict(
        script_type="command",
        event="post-install",
        bash="echo hi",
        command="echo hi",
    )
    base.update(overrides)
    return ScriptEntry(**base)


def fire(entry, event, project_root):
    """Fire a single entry through a runner and return started threads."""
    from apm_cli.core.lifecycle_scripts import LifecycleScriptRunner

    runner = LifecycleScriptRunner(scripts=[entry], project_root=str(project_root))
    return runner.fire("post-install", event)
