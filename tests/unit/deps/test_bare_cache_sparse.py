"""Tests for sparse-cone path in bare_cache.materialize_from_bare (perf #1433)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from apm_cli.deps.bare_cache import materialize_from_bare


def _build_local_bare_repo(tmp_path: Path) -> tuple[Path, str]:
    """Build a local repo with multiple subdirs and return (bare_path, sha)."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    for sub in ("plugins", "tools", "docs"):
        d = work / sub
        d.mkdir()
        (d / "f.txt").write_text(f"{sub}\n", encoding="utf-8")
    # nested fixture for the nested-path test
    (work / "plugins" / "nested").mkdir()
    (work / "plugins" / "nested" / "leaf.txt").write_text("leaf\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "init"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare, sha


def test_default_full_tree_materialized(tmp_path: Path):
    bare, sha = _build_local_bare_repo(tmp_path)
    consumer = tmp_path / "consumer"
    resolved = materialize_from_bare(bare, consumer, ref=None, env=os.environ.copy(), known_sha=sha)
    assert resolved == sha
    assert (consumer / "plugins" / "f.txt").is_file()
    assert (consumer / "tools" / "f.txt").is_file()
    assert (consumer / "docs" / "f.txt").is_file()


def test_sparse_paths_only_materializes_requested_subdir(tmp_path: Path):
    bare, sha = _build_local_bare_repo(tmp_path)
    consumer = tmp_path / "consumer"
    resolved = materialize_from_bare(
        bare,
        consumer,
        ref=None,
        env=os.environ.copy(),
        known_sha=sha,
        sparse_paths=["plugins"],
    )
    assert resolved == sha
    assert (consumer / "plugins" / "f.txt").is_file()
    # Sparse-cone excludes sibling top-level dirs:
    assert not (consumer / "tools").exists()
    assert not (consumer / "docs").exists()
    # .git is always present
    assert (consumer / ".git").is_dir()


def test_nested_subdir_path_materializes_nested(tmp_path: Path):
    bare, sha = _build_local_bare_repo(tmp_path)
    consumer = tmp_path / "consumer"
    materialize_from_bare(
        bare,
        consumer,
        ref=None,
        env=os.environ.copy(),
        known_sha=sha,
        sparse_paths=["plugins/nested"],
    )
    assert (consumer / "plugins" / "nested" / "leaf.txt").is_file()
    assert not (consumer / "tools").exists()


def test_nonexistent_sparse_subdir_fails_loud_or_empty(tmp_path: Path):
    """A subdir that doesn't exist must NOT silently materialize a full tree.

    git sparse-checkout does not error on missing paths (it just leaves
    the working tree empty for the missing entry). The invariant we
    enforce is: no sibling subdir leaks in.
    """
    bare, sha = _build_local_bare_repo(tmp_path)
    consumer = tmp_path / "consumer"
    materialize_from_bare(
        bare,
        consumer,
        ref=None,
        env=os.environ.copy(),
        known_sha=sha,
        sparse_paths=["nonexistent/path"],
    )
    # Critical invariant: no full-tree leak.
    assert not (consumer / "plugins").exists()
    assert not (consumer / "tools").exists()
    assert not (consumer / "docs").exists()
