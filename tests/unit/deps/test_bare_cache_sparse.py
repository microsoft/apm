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


def _build_repo_with_out_of_cone_symlink_target(tmp_path: Path) -> tuple[Path, str]:
    """Repro shape for #2707: a symlink inside the cone, target outside it.

    ``plugins/skill/ref.md`` stands in for a symlink whose target lives
    in the sibling ``shared/`` dir, which the ``plugins`` cone excludes.
    """
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)

    cone_dir = work / "plugins" / "skill"
    cone_dir.mkdir(parents=True)
    (cone_dir / "ref.md").write_text("stand-in for a symlink entry\n")

    shared_dir = work / "shared"
    shared_dir.mkdir()
    (shared_dir / "ref.md").write_text("the real target content\n")

    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "-m", "test: init fixture repo"], check=True
    )
    sha = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    bare = tmp_path / "bare-symlink.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare, sha


def test_dangling_symlink_in_cone_is_repaired(tmp_path: Path, monkeypatch):
    """#2707: a symlink whose target sits outside the requested cone must
    still resolve after ``materialize_from_bare`` returns.

    This box can't create a real symlink (WinError 1314) and a real
    checkout here writes a mode-120000 entry as a plain file (verified:
    ``core.symlinks`` defaults to false on this filesystem), so the
    ``ref.md`` stand-in above is monkeypatched to LOOK dangling the way
    a real symlink pointing at ``../../shared/ref.md`` would on a
    filesystem that honors ``core.symlinks`` -- see
    tests/unit/utils/test_git_sparse.py for the same technique applied
    to the detection helper in isolation.
    """
    bare, sha = _build_repo_with_out_of_cone_symlink_target(tmp_path)
    consumer = tmp_path / "consumer"

    fake_link = consumer / "plugins" / "skill" / "ref.md"
    real_islink = os.path.islink

    def fake_islink(path):
        return True if Path(path) == fake_link else real_islink(path)

    def fake_exists(path):
        if Path(path) == fake_link:
            return (consumer / "shared" / "ref.md").exists()
        return os.path.lexists(path)

    monkeypatch.setattr(os.path, "islink", fake_islink)
    monkeypatch.setattr(os.path, "exists", fake_exists)
    monkeypatch.setattr(
        "apm_cli.utils.git_sparse._tracked_symlinks",
        lambda *args, **kwargs: [fake_link],
    )

    materialize_from_bare(
        bare,
        consumer,
        ref=None,
        env=os.environ.copy(),
        known_sha=sha,
        sparse_paths=["plugins/skill"],
    )

    # The would-be symlink's target must be reachable: the fallback
    # widened the tree instead of leaving it dangling.
    assert (consumer / "shared" / "ref.md").is_file()
    assert (consumer / "shared" / "ref.md").read_text() == "the real target content\n"
