"""GitPython stays unloaded until a clone needs it (owner: cli-startup-import-budget).

``import git`` loads about 60 modules and runs ``git version`` in a
subprocess. Building the install downloader, as every ``apm install`` does
(including a no-op), must not pay that; GitPython loads only on a clone path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-cli-startup-import-budget")]

_PROBE = """
import json, sys
import apm_cli.commands.install
import apm_cli.deps.bare_cache
import apm_cli.deps.clone_engine
import apm_cli.deps.git_reference_resolver
from apm_cli.core.auth import AuthResolver
from apm_cli.deps.github_downloader import GitHubPackageDownloader
GitHubPackageDownloader(auth_resolver=AuthResolver())
print(json.dumps(sorted(m for m in sys.modules if m == "git" or m.startswith("git."))))
"""


def test_install_downloader_setup_does_not_import_gitpython() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.splitlines()[-1]) == []


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
