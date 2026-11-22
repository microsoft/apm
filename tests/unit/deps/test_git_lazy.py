"""GitPython names stay matchable and patchable after its import was deferred."""

from __future__ import annotations

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
