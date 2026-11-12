"""Git-object link materialization owner (#3457, #2707).

Links are resolved against the checked-out commit's tree, never the host
filesystem, so these tests run identically on hosts without OS symlink
capability. Fixtures commit links as mode-120000 index entries.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.utils import git_sparse
from apm_cli.utils.git_sparse import (
    apply_sparse_cone,
    materialize_tracked_symlinks,
    validate_materialized_symlinks,
)
from apm_cli.utils.path_security import PathTraversalError
from tests.utils.git_link_fixture import (
    EXPECTED_PKG,
    EXPECTED_PKG_EXECUTABLE,
    build_link_origin,
    executable_files,
    git,
    neutral_git_env,
    tree_bytes,
)

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]

_PIN = ["-c", "core.symlinks=false", "-c", "core.autocrlf=false"]


def _checkout(tmp_path: Path, extra_links: dict[str, str] | None = None, *, sparse: bool = False):
    origin, _ = build_link_origin(tmp_path, extra_links)
    checkout = tmp_path / "checkout"
    git(*_PIN, "clone", "-q", "--no-checkout", str(origin), str(checkout))
    if sparse:
        apply_sparse_cone("git", checkout, ["pkg"], env=neutral_git_env())
    git(*_PIN, "-C", str(checkout), "checkout", "-q", "main")
    return checkout


def _materialize(checkout: Path, paths: list[str] | None = None, **kwargs: object) -> list[str]:
    return materialize_tracked_symlinks(
        "git", checkout, paths, env=neutral_git_env(), extra_git_args=_PIN, **kwargs
    )


@pytest.mark.parametrize("sparse", [False, True], ids=["full", "sparse"])
def test_contained_links_become_posix_content(tmp_path: Path, sparse: bool) -> None:
    checkout = _checkout(tmp_path, sparse=sparse)
    assert (checkout / "pkg" / "AGENTS.md").read_bytes() == b"CLAUDE.md"

    done = _materialize(checkout, ["pkg"])

    assert done == [
        "pkg/AGENTS.md",
        "pkg/chain.md",
        "pkg/notes.crlf",
        "pkg/run",
        "pkg/shared",
    ]
    assert tree_bytes(checkout / "pkg") == EXPECTED_PKG
    if os.name != "nt":
        assert executable_files(checkout / "pkg") == EXPECTED_PKG_EXECUTABLE
    # Out-of-cone targets came from Git objects; the cone did not widen (#2707).
    assert (checkout / "shared").exists() is not sparse


def test_scope_limits_materialization(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)

    assert _materialize(checkout, ["shared"]) == ["shared/inner/up.md"]
    assert (checkout / "pkg" / "AGENTS.md").read_bytes() == b"CLAUDE.md"
    assert _materialize(checkout, None)[-1] == "shared/inner/up.md"


def test_package_without_links_costs_one_ls_tree(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)
    real_run = subprocess.run
    calls: list[list[str]] = []

    def run(args: list[str], *pargs: object, **kwargs: object) -> object:
        calls.append(list(args))
        return real_run(args, *pargs, **kwargs)

    with patch.object(git_sparse.subprocess, "run", side_effect=run):
        assert _materialize(checkout, [".gitattributes"]) == []

    assert len(calls) == 1
    assert "ls-tree" in calls[0]


@pytest.mark.parametrize(
    ("target", "error", "message"),
    [
        ("../../outside.md", PathTraversalError, "resolves outside the repository"),
        ("/etc/passwd", PathTraversalError, "resolves outside the repository"),
        ("C:/Windows/win.ini", PathTraversalError, "resolves outside the repository"),
        ("../.git/config", RuntimeError, "is not a tracked file or directory"),
        ("missing.md", RuntimeError, "is not a tracked file or directory"),
        ("CLAUDE.md/child", RuntimeError, "is not a tracked file or directory"),
        ("CLAUDE.md/", RuntimeError, "is not a tracked file or directory"),
        ("CLAUDE.md/../tool.sh", RuntimeError, "is not a tracked file or directory"),
        ("CLAUDE.md/.", RuntimeError, "is not a tracked file or directory"),
        (".", RuntimeError, "forms a cycle"),
        ("..", RuntimeError, "forms a cycle"),
        ("bad.md", RuntimeError, "forms a cycle"),
    ],
)
def test_unresolvable_links_are_rejected(
    tmp_path: Path, target: str, error: type[Exception], message: str
) -> None:
    checkout = _checkout(tmp_path, {"pkg/bad.md": target})

    with pytest.raises(error, match=message) as raised:
        validate_materialized_symlinks("git", checkout, ["pkg"], env=neutral_git_env())
    assert "pkg/bad.md" in str(raised.value)
    assert str(raised.value).isascii()
    with pytest.raises(error, match=message):
        _materialize(checkout, ["pkg"])
    # Strict mode raises before writing anything.
    assert (checkout / "pkg" / "AGENTS.md").read_bytes() == b"CLAUDE.md"


@pytest.mark.parametrize(
    "links",
    [
        {"pkg/loop-a": "loop-b", "pkg/loop-b": "loop-a"},
        {"pkg/dir/back": "../dir-link", "pkg/dir-link": "dir", "pkg/dir/file.md": "../CLAUDE.md"},
    ],
    ids=["mutual-file-links", "nested-directory-cycle"],
)
def test_cycles_are_bounded(tmp_path: Path, links: dict[str, str]) -> None:
    checkout = _checkout(tmp_path, links)

    with pytest.raises(RuntimeError, match="forms a cycle"):
        validate_materialized_symlinks("git", checkout, ["pkg"], env=neutral_git_env())


def test_lenient_mode_keeps_link_text_for_unresolvable_links(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, {"pkg/bad.md": "../../outside.md"})

    done = _materialize(checkout, ["pkg"], strict=False)

    assert "pkg/bad.md" not in done
    assert (checkout / "pkg" / "bad.md").read_bytes() == b"../../outside.md"
    assert (checkout / "pkg" / "AGENTS.md").read_bytes() == b"Package instructions\n"


def test_materialization_is_idempotent(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)
    _materialize(checkout, ["pkg"])

    _materialize(checkout, ["pkg"])

    assert tree_bytes(checkout / "pkg") == EXPECTED_PKG


def test_never_writes_through_a_linked_directory(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Planting the hostile directory link needs OS symlink capability")
    checkout = _checkout(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    real_pkg = checkout / "pkg"
    real_pkg.rename(tmp_path / "moved")
    real_pkg.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathTraversalError):
        _materialize(checkout, ["pkg"])
    assert list(outside.iterdir()) == []


def test_fan_out_expansion_is_rejected_by_a_shared_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acyclic links that double per level must not grow exponentially."""
    monkeypatch.setattr(git_sparse, "_MAX_LINK_WRITES", 200)
    links = {"pkg/fan/l0/file.md": "../../CLAUDE.md"}
    for level in range(1, 12):
        links[f"pkg/fan/l{level}/a"] = f"../l{level - 1}"
        links[f"pkg/fan/l{level}/b"] = f"../l{level - 1}"
    checkout = _checkout(tmp_path, links)

    with pytest.raises(RuntimeError, match="expands to more than 200 entries"):
        validate_materialized_symlinks("git", checkout, ["pkg/fan/l11"], env=neutral_git_env())


@pytest.mark.parametrize("name", ["..\\..\\..\\outside.txt", "c:stream.txt"])
def test_entries_unsafe_on_windows_are_rejected_before_any_write(tmp_path: Path, name: str) -> None:
    """A directory link must not copy a host-unsafe entry name (``..\\`` or ``:``)."""
    origin, _ = build_link_origin(tmp_path, {"pkg/assets": "../evil"})

    # mktree bypasses index path checks, so a POSIX-authored name can be
    # committed even where Git for Windows refuses to stage it.
    def mktree(listing: str) -> str:
        # Bytes, not text: Windows text-mode stdin would append CR to names.
        return (
            subprocess.run(
                ["git", "-C", str(origin), "mktree"],
                input=listing.encode("utf-8"),
                env=neutral_git_env(),
                capture_output=True,
                check=True,
                timeout=60,
            )
            .stdout.decode("ascii")
            .strip()
        )

    blob = git("-C", str(origin), "hash-object", "-w", "--stdin", input="payload")
    evil = mktree(f"100644 blob {blob}\t{name}\n")
    tree = mktree(git("-C", str(origin), "ls-tree", "HEAD") + f"\n040000 tree {evil}\tevil\n")
    commit = git("-C", str(origin), "commit-tree", tree, "-p", "HEAD", "-m", "unsafe entry")
    git("-C", str(origin), "update-ref", "refs/heads/main", commit)
    # Planning reads only the commit tree, so no checkout is needed (Git for
    # Windows would refuse to check this tree out at all).
    checkout = tmp_path / "checkout"
    git(*_PIN, "clone", "-q", "--no-checkout", str(origin), str(checkout))

    with pytest.raises(PathTraversalError, match="not portable to every host"):
        _materialize(checkout, ["pkg"])
    assert not (tmp_path / "outside.txt").exists()
