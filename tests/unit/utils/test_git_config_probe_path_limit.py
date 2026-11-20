"""Git config probe for a Git directory deeper than Git's explicit GIT_DIR limit (#4397)."""

from __future__ import annotations

import sys
from pathlib import Path

from apm_cli.utils.git_env import git_clone_env

# Git dies with "'$GIT_DIR' too big" when an explicit GIT_DIR is longer than
# PATH_MAX - 40. PATH_MAX is 260 in Git for Windows, 1024 on macOS, 4096 on Linux.
_GIT_PATH_MAX = {"win32": 260, "darwin": 1024}.get(sys.platform, 4096)


def _deep_directory(root: Path, length: int) -> Path:
    path = root
    while len(str(path)) < length - 60:
        path = path / ("d" * 50)
    path = path / ("p" * (length - len(str(path)) - 1))
    path.mkdir(parents=True)
    return path


def test_clone_config_probe_runs_for_a_git_dir_beyond_the_explicit_limit(tmp_path: Path) -> None:
    target = _deep_directory(tmp_path.resolve(), _GIT_PATH_MAX - 34)
    assert len(str(target / ".git")) > _GIT_PATH_MAX - 40

    env = git_clone_env("https://example.com/acme/tool.git", {}, target)

    assert isinstance(env, dict)
    assert not (target / ".git").exists()
