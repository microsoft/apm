"""The project-remote read is fresh, except inside the record's write-time render."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.policy import _remote

pytestmark = pytest.mark.unit


def _repo(tmp_path: Path, url: str) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", url], check=True)
    return tmp_path


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_changed_remote_is_read_fresh_outside_the_render_seam(tmp_path: Path) -> None:
    root = _repo(tmp_path, "https://github.com/acme/one.git")
    assert _remote._git_remote_origin_url(root) == "https://github.com/acme/one.git"
    subprocess.run(
        ["git", "-C", str(root), "remote", "set-url", "origin", "https://github.com/acme/two.git"],
        check=True,
    )
    assert _remote._git_remote_origin_url(root) == "https://github.com/acme/two.git"


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_render_seam_reuses_the_read_without_spawning_git(tmp_path: Path) -> None:
    root = _repo(tmp_path, "https://github.com/acme/one.git")
    assert _remote._git_remote_origin_url(root) == "https://github.com/acme/one.git"
    shutil.rmtree(root / ".git")  # a fresh read would now find no remote
    with _remote.reuse_remote_reads():
        assert _remote._git_remote_origin_url(root) == "https://github.com/acme/one.git"
    assert _remote._git_remote_origin_url(root) is None
