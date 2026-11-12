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

from apm_cli.cache import git_cache as git_cache_module
from apm_cli.cache.git_cache import GitCache
from apm_cli.utils.git_env import (
    GitOutputDecodeError,
    _git_command_label,
    clone_git_worktree,
    git_remote_refs,
    run_git_text,
)
from apm_cli.utils.git_sparse import (
    apply_sparse_cone,
    materialize_tracked_symlinks,
    sparse_checkout_active,
)

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

    apply_sparse_cone("git", repo, [_UTF8_NAME], env=None)

    assert sparse_checkout_active("git", repo, env=None)
    # ls-tree -z paths must decode; the file is regular so no link is materialized.
    assert materialize_tracked_symlinks("git", repo, None, env=None) == []


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


def _decode_error(argv: list[str]) -> GitOutputDecodeError:
    return GitOutputDecodeError(argv, UnicodeDecodeError("utf-8", b"\x94", 0, 1, "bad"))


_SECRET_URL = "https://x-access-token:ghp_secret@example.com/o/r.git"


@pytest.mark.parametrize(
    ("command", "label"),
    [
        (["git", "-C", "/home/u/secret", "ls-remote", _SECRET_URL], "git ls-remote"),
        (
            ["C:\\Git\\git.exe", "-c", f"remote.origin.url={_SECRET_URL}", "fetch", _SECRET_URL],
            "git fetch",
        ),
        (["/usr/bin/git", "--git-dir", "/tmp/bare", "--config-env", "a=B", "clone"], "git clone"),
        (["git", "--work-tree=/w", "--namespace", "ns", "checkout", "main"], "git checkout"),
        (["git", "-c", "k=v"], "git"),
        ([], "git"),
    ],
)
def test_decode_error_names_subcommand_without_urls_or_paths(
    command: list[str], label: str
) -> None:
    assert _git_command_label(command) == label

    message = str(_decode_error(command))

    assert message.startswith(f"'{label}' wrote output that is not valid UTF-8 (byte 0x94")
    for secret in ("ghp_secret", "example.com", "/home/u/secret", "/tmp/bare", "a=B", "/w"):
        assert secret not in message


def _ok(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, "", "")


def test_checkout_decode_rejection_removes_staged_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = GitCache(tmp_path)
    sha = "a" * 40
    staged: list[Path] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if "checkout" in argv:
            stage = Path(argv[argv.index("-C") + 1])
            (stage / "partial.md").write_text("x\n", encoding="utf-8")
            staged.append(stage)
            raise _decode_error(argv)
        return _ok(argv)

    monkeypatch.setattr(git_cache_module, "run_git_text", fake_run)

    with pytest.raises(RuntimeError, match="not valid UTF-8"):
        cache._create_checkout("https://example.com/o/r", "shard", sha)

    assert staged
    assert not staged[0].exists()
    sha_dir = cache._checkouts_root / "shard" / sha
    assert [p.name for p in sha_dir.iterdir() if not p.name.endswith(".lock")] == []


@pytest.mark.parametrize("partial", [False, True])
def test_bare_clone_decode_rejection_removes_staged_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial: bool
) -> None:
    cache = GitCache(tmp_path)
    staged: list[Path] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        stage = Path(argv[-1])
        (stage / "packed-refs").write_text("x\n", encoding="utf-8")
        staged.append(stage)
        if partial and "--filter=blob:none" in argv:
            raise subprocess.CalledProcessError(
                128, argv, stderr="fatal: server does not support filter"
            )
        raise _decode_error(argv)

    monkeypatch.setattr(git_cache_module, "run_git_text", fake_run)

    with pytest.raises(RuntimeError, match="not valid UTF-8"):
        cache._ensure_bare_repo("https://example.com/o/r", "shard", "a" * 40, partial=partial)

    assert len(staged) == (2 if partial else 1)
    assert not any(stage.exists() for stage in staged)
    assert [p.name for p in cache._db_root.iterdir() if not p.name.endswith(".lock")] == []


def test_sparse_probe_decode_rejection_counts_as_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise _decode_error(argv)

    monkeypatch.setattr(git_cache_module, "run_git_text", fake_run)

    # An unpinned sparse shard whose ls-files output is not UTF-8 is rebuilt,
    # not trusted, and the decode error does not escape the cache-hit path.
    assert not git_cache_module._sparse_checkout_is_complete(tmp_path, ["skills"], env=None)
