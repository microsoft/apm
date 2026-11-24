"""The project-remote read is fresh, except inside the record's write-time render."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.policy import _remote

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-policy-remote-origin-reader")]


def _assert_origin(root: Path, expected: str | None) -> None:
    """The remote policy discovery reads for *root* (the owner's observable answer)."""
    assert _remote._git_remote_origin_url(root) == expected


def _repo(tmp_path: Path, url: str) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", url], check=True)
    return tmp_path


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_changed_remote_is_read_fresh_outside_the_render_seam(tmp_path: Path) -> None:
    root = _repo(tmp_path, "https://github.com/acme/one.git")
    _assert_origin(root, "https://github.com/acme/one.git")
    subprocess.run(
        ["git", "-C", str(root), "remote", "set-url", "origin", "https://github.com/acme/two.git"],
        check=True,
    )
    _assert_origin(root, "https://github.com/acme/two.git")


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_render_seam_reuses_the_read_without_spawning_git(tmp_path: Path) -> None:
    root = _repo(tmp_path, "https://github.com/acme/one.git")
    _assert_origin(root, "https://github.com/acme/one.git")
    shutil.rmtree(root / ".git")  # a fresh read would now find no remote
    with _remote.reuse_remote_reads():
        _assert_origin(root, "https://github.com/acme/one.git")
    _assert_origin(root, None)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_user_code_between_reads_makes_the_render_seam_read_fresh(tmp_path: Path) -> None:
    """A lifecycle script may change the remote: forget_invocation_memos ends the reuse."""
    from apm_cli.core.invocation_memo import forget_invocation_memos

    root = _repo(tmp_path, "https://github.com/acme/one.git")
    _assert_origin(root, "https://github.com/acme/one.git")
    forget_invocation_memos()
    shutil.rmtree(root / ".git")
    with _remote.reuse_remote_reads():
        _assert_origin(root, None)
