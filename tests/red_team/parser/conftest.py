"""Shared fixtures and helpers for the parser red-team chaos suite.

All helpers are hermetic: they write into ``tmp_path`` and never touch the
real ``/etc/apm/policy.d`` or the user's home directory. The wall-clock
guard (:func:`run_guarded`) runs a callable in a daemon thread and fails
fast if it does not finish within a bound -- this lets a vulnerable parser
(hang / DoS) be detected without ever wedging CI.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def write_text_bytes(path: Path, data: bytes) -> Path:
    """Write raw bytes to *path* (used for NUL / control-char fixtures)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_apm_yml(root: Path, content: str) -> Path:
    """Write an ``apm.yml`` containing *content* at *root* and return it."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / "apm.yml"
    target.write_text(content, encoding="utf-8")
    return target


def run_guarded(fn, timeout: float = 8.0):
    """Run *fn* in a daemon thread with a wall-clock bound.

    Returns ``(finished, result, exception)``. ``finished`` is False if the
    call did not complete within *timeout* -- the signal a parser hang / DoS
    has occurred. The worker is a daemon thread, so a wedged call cannot
    keep the test process (or CI) alive.
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
    """A hermetic admin-policy directory wired into discovery.

    Patches ``_get_policy_scripts_dir`` in the lifecycle_scripts module so
    both ``discover_scripts`` and the ``validate`` CLI read from tmp instead
    of ``/etc/apm/policy.d``.
    """
    from apm_cli.core import lifecycle_scripts

    pdir = tmp_path / "policy.d"
    pdir.mkdir()
    monkeypatch.setattr(lifecycle_scripts, "_get_policy_scripts_dir", lambda: pdir)
    return pdir


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Point APM_HOME at a tmp dir with no user apm.yml.

    Guarantees the user tier contributes nothing so tests observe only the
    project / policy input under test.
    """
    home = tmp_path / "apm_home"
    home.mkdir()
    monkeypatch.setenv("APM_HOME", str(home))
    return home
