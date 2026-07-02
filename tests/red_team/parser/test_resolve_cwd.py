"""RED-TEAM: _resolve_cwd containment against parser-supplied cwd values.

A command script's ``cwd`` comes straight from the manifest. The resolver
must clamp relative paths that escape the project root (``../../.ssh``),
catch symlinks inside the repo that point outside, pass explicit absolute
paths through unchanged, and handle the empty / dot / None-root degenerate
cases without raising.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _entry(cwd):
    from apm_cli.core.lifecycle_scripts import ScriptEntry

    return ScriptEntry(script_type="command", event="post-install", bash="echo hi", cwd=cwd)


def _resolve(cwd, project_root):
    from apm_cli.core.script_executors import _resolve_cwd

    return _resolve_cwd(_entry(cwd), project_root)


def test_relative_escape_clamps_to_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    resolved = _resolve("../../../../etc", str(root))
    assert resolved == str(root.resolve())


def test_dotdot_within_resolves_inside(tmp_path):
    root = tmp_path / "repo"
    (root / "a" / "b").mkdir(parents=True)
    # repo/a/b/../.. == repo -- stays inside, returns the root.
    resolved = _resolve("a/b/../..", str(root))
    assert resolved == str(root.resolve())


def test_absolute_cwd_passes_through(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    # Absolute paths are explicit and visible in apm.yml -> passed through.
    resolved = _resolve(str(outside), str(root))
    assert resolved == str(outside)


def test_symlink_escaping_root_is_clamped(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret"
    outside.mkdir()
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    # cwd 'escape' is a relative symlink; resolve() must follow it to
    # 'outside', detect the escape, and clamp back to the root.
    resolved = _resolve("escape", str(root))
    assert resolved == str(root.resolve())


def test_empty_cwd_returns_project_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    assert _resolve("", str(root)) == str(root)


def test_dot_cwd_resolves_to_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    assert _resolve(".", str(root)) == str(root.resolve())


def test_none_project_root_with_escape_clamps_to_cwd(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    # project_root=None -> root defaults to cwd; an escaping relative path
    # must clamp to the resolved cwd, not leak outside.
    resolved = _resolve("../../..", None)
    assert resolved == str(Path(work).resolve())


def test_none_cwd_and_none_root_returns_none(tmp_path):
    from apm_cli.core.lifecycle_scripts import ScriptEntry
    from apm_cli.core.script_executors import _resolve_cwd

    entry = ScriptEntry(script_type="command", event="post-install", bash="echo hi", cwd=None)
    assert _resolve_cwd(entry, None) is None
