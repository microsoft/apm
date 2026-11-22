"""GitPython names stay matchable and patchable after its import was deferred."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-cli-startup-import-budget")]


def test_git_command_error_matches_real_errors_once_gitpython_is_loaded() -> None:
    from git.exc import GitCommandError

    from apm_cli.deps.git_lazy import git_command_error, new_git_command_error

    assert git_command_error() is GitCommandError
    error = new_git_command_error(["git", "ls-remote"], 128, stderr="denied")
    caught = None
    try:
        raise error
    except git_command_error() as exc:
        caught = exc
    assert isinstance(caught, GitCommandError)


def test_gitpython_names_remain_patchable_module_attributes() -> None:
    import git

    from apm_cli.deps import bare_cache, github_downloader

    assert github_downloader.Repo is git.Repo
    assert github_downloader.git is git
    assert bare_cache.Repo is git.Repo
    assert issubclass(github_downloader.GitProgressReporter, git.RemoteProgress)
    double = MagicMock()
    with patch("apm_cli.deps.github_downloader.Repo", double):
        assert github_downloader.Repo is double
    assert github_downloader.Repo is git.Repo


def test_loaded_git_module_ignores_a_module_still_initializing(monkeypatch) -> None:
    import types
    from importlib.machinery import ModuleSpec

    from apm_cli.deps.git_lazy import loaded_git_module

    synthetic = types.ModuleType("git")
    synthetic.__spec__ = ModuleSpec("git", None)
    synthetic.__spec__._initializing = True
    monkeypatch.setitem(sys.modules, "git", synthetic)

    assert loaded_git_module({}) is None
    synthetic.__spec__._initializing = False
    assert loaded_git_module({}) is synthetic
    double = MagicMock()
    assert loaded_git_module({"git": double}) is double
