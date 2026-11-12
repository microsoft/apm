"""Regression tests for #3056: Git child output must not depend on the locale.

On a Simplified Chinese Windows host the default text codec is cp936 (GBK).
``subprocess`` text mode decodes child output with that codec, so UTF-8 bytes
emitted by Git (branch names, paths, remote banners) crashed ``apm install``
inside the reader thread. These tests force the default codec to GBK and
drive real child processes through the install-path Git executors.
"""

from __future__ import annotations

import locale
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from apm_cli.utils.git_env import (
    GitOutputDecodeError,
    clone_git_worktree,
    git_remote_refs,
    run_git_text,
)
from apm_cli.utils.git_sparse import _tracked_symlinks

pytestmark = pytest.mark.windows_compat

_UTF8_NAME = "release\u2014\u201dnext"

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture
def cp936_default_codec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make subprocess text mode default to GBK, as on a zh-CN Windows host."""
    monkeypatch.setattr(locale, "getencoding", lambda: "gbk", raising=False)
    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: "gbk")


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def _source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "src"
    _git("init", "-q", str(repo))
    (repo / f"{_UTF8_NAME}.md").write_text("x\n", encoding="utf-8")
    _git("-C", str(repo), "add", "-A")
    _git(
        "-C",
        str(repo),
        "-c",
        "user.email=apm@example.invalid",
        "-c",
        "user.name=apm",
        "commit",
        "-q",
        "-m",
        "init",
    )
    _git("-C", str(repo), "branch", _UTF8_NAME)
    return repo


def _child(stdout: bytes = b"", stderr: bytes = b"", code: int = 0) -> list[str]:
    script = (
        "import sys;"
        f"sys.stdout.buffer.write({stdout!r});sys.stdout.flush();"
        f"sys.stderr.buffer.write({stderr!r});sys.stderr.flush();"
        f"sys.exit({code})"
    )
    return [sys.executable, "-c", script]


@requires_git
def test_remote_refs_decode_utf8_under_cp936_locale(
    tmp_path: Path, cp936_default_codec: None
) -> None:
    repo = _source_repo(tmp_path)

    result = git_remote_refs(str(repo), check=True)

    assert f"refs/heads/{_UTF8_NAME}" in result.stdout


@requires_git
def test_sparse_ls_tree_paths_decode_utf8_under_cp936_locale(
    tmp_path: Path, cp936_default_codec: None
) -> None:
    repo = _source_repo(tmp_path)

    # ls-tree output must decode; the file is regular so no symlink is reported.
    assert _tracked_symlinks("git", repo, ["."], env=None, timeout=30, extra_git_args=None) == []


@requires_git
def test_progress_clone_decodes_utf8_paths_under_cp936_locale(
    tmp_path: Path, cp936_default_codec: None
) -> None:
    repo = _source_repo(tmp_path)
    target = tmp_path / f"dest-{_UTF8_NAME}"
    lines: list[str] = []

    class _Progress:
        def new_message_handler(self):
            return lines.append

    clone_git_worktree(str(repo), target, progress=_Progress())

    assert (target / f"{_UTF8_NAME}.md").is_file()


def test_valid_utf8_output_is_decoded_deterministically(cp936_default_codec: None) -> None:
    result = run_git_text(
        _child(stdout="ok \u2014\r\n".encode(), stderr="warn \u201d\n".encode()),
        capture_output=True,
    )

    assert result.returncode == 0
    assert result.stdout == "ok \u2014\n"
    assert result.stderr == "warn \u201d\n"


def test_malformed_stdout_is_surfaced_not_silently_corrupted(cp936_default_codec: None) -> None:
    with pytest.raises(GitOutputDecodeError, match="not valid UTF-8") as excinfo:
        run_git_text(_child(stdout=b"refs \x94 tail\n"), capture_output=True)

    assert isinstance(excinfo.value, subprocess.SubprocessError)
    assert "0x94" in str(excinfo.value)


def test_command_failure_is_preserved_with_malformed_stderr(cp936_default_codec: None) -> None:
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        run_git_text(
            _child(stdout=b"partial \xff\n", stderr=b"fatal: \x94 denied\n", code=128),
            capture_output=True,
            check=True,
        )

    assert excinfo.value.returncode == 128
    assert excinfo.value.stderr == "fatal: \\x94 denied\n"
    assert excinfo.value.stdout == "partial \\xff\n"


def test_unchecked_failure_returns_visible_escapes(cp936_default_codec: None) -> None:
    result = run_git_text(_child(stderr=b"fatal: \x94\n", code=1), capture_output=True)

    assert result.returncode == 1
    assert result.stderr == "fatal: \\x94\n"
