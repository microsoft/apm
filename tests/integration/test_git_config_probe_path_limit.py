"""Git config probe for a Git directory deeper than Git's explicit GIT_DIR limit (#4397)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from apm_cli.utils.git_env import (
    GitUrlRewriteError,
    get_git_executable,
    git_clone_env,
    git_no_templates_args,
    git_subprocess_env,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-OWN-git-child-repository-isolation"),
]

# Git dies with "'$GIT_DIR' too big" when an explicit GIT_DIR is longer than
# PATH_MAX - 40. PATH_MAX is 260 in Git for Windows, 1024 on macOS, 4096 on Linux.
_GIT_PATH_MAX = {"win32": 260, "darwin": 1024}.get(sys.platform, 4096)
_REMOTE = "https://example.com/acme/tool.git"


def _deep_directory(root: Path, length: int) -> Path:
    path = root
    while len(str(path)) < length - 60:
        path = path / ("d" * 50)
    path = path / ("p" * (length - len(str(path)) - 1))
    path.mkdir(parents=True)
    assert len(str(path / ".git")) > _GIT_PATH_MAX - 40
    return path


def _git(*args: str) -> None:
    subprocess.run(
        [get_git_executable(), *args],
        check=True,
        capture_output=True,
        env=git_subprocess_env(),
    )


def test_clone_config_probe_runs_for_a_git_dir_beyond_the_explicit_limit(tmp_path: Path) -> None:
    target = _deep_directory(tmp_path.resolve(), _GIT_PATH_MAX - 34)

    env = git_clone_env(_REMOTE, {}, target)

    assert isinstance(env, dict)
    assert not (target / ".git").exists()


def test_deep_clone_config_probe_still_refuses_an_unsafe_url_rewrite(tmp_path: Path) -> None:
    target = _deep_directory(tmp_path.resolve(), _GIT_PATH_MAX - 34)
    _git("init", *git_no_templates_args(), "--quiet", str(target))
    _git("-C", str(target), "config", "url.http://example.com/.insteadOf", "https://example.com/")

    with pytest.raises(GitUrlRewriteError):
        git_clone_env(_REMOTE, {}, target)
