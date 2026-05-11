"""Regression trap for the root-conftest Path.home() override.

Locks in the contract that survives the windows-2025-vs2026 GitHub
runner: ``Path.home()`` MUST NOT raise even when every env var that
``ntpath.expanduser`` (Windows) or ``posixpath.expanduser`` (POSIX)
consults is unset. The earlier 56-failure / 53-failure / 46-failure
runs all tripped on a single xdist worker that hit
``RuntimeError: Could not determine home directory``.
"""

from __future__ import annotations

from pathlib import Path


def test_path_home_does_not_raise_with_cleared_env(monkeypatch):
    """Path.home() must return a usable Path even with HOME/USERPROFILE/etc cleared."""
    for key in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
        monkeypatch.delenv(key, raising=False)

    home = Path.home()

    assert isinstance(home, Path)
    assert str(home)


def test_path_home_honors_per_test_home_setenv(monkeypatch, tmp_path):
    """Per-test ``monkeypatch.setenv("HOME", ...)`` must still redirect Path.home()."""
    target = tmp_path / "custom-home"
    target.mkdir()
    monkeypatch.setenv("HOME", str(target))

    assert Path.home() == target


def test_path_expanduser_does_not_raise_with_cleared_env(monkeypatch):
    """Path('~/pkg').expanduser() must not raise on the windows-2025-vs2026 runner.

    ntpath.expanduser raises RuntimeError when USERPROFILE and HOMEPATH are
    both absent. Production code in install.package_resolution depends on
    expanduser to detect that `~/pkg` is absolute. The root conftest wraps
    Path.expanduser so this case falls back to the hermetic tmp dir.
    """
    for key in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
        monkeypatch.delenv(key, raising=False)

    expanded = Path("~/pkg").expanduser()

    assert expanded.is_absolute()
    assert expanded.name == "pkg"
