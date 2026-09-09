"""Tests for the sparse-cone dangling-symlink repair helper (#2707).

Sparse-cone checkout (perf #1433) only materializes the requested
top-level paths. A repo whose payload contains a symlink pointing
OUTSIDE those paths ends up with a dangling symlink once checked
out on a filesystem that honors ``core.symlinks`` -- the entry itself
is inside the cone (so it gets checked out) but its target is not, so
any code that later dereferences it (a plain ``open()``,
``shutil.copytree`` without ``symlinks=True``) fails with
``FileNotFoundError``.

The platform-neutral unit tests use a stand-in entry so they also run
where Git has ``core.symlinks=false``. The real-symlink consumer path is
covered in ``tests/integration/test_sparse_cone_symlink_repair.py``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from apm_cli.utils.git_sparse import (
    apply_sparse_cone,
    repair_dangling_cone_symlinks,
)


def _build_local_bare_repo(tmp_path: Path) -> tuple[Path, str]:
    """Repo shaped like the #2707 repro: a cone dir plus an outside sibling."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)

    cone_dir = work / "skills" / "better-writing" / "references"
    cone_dir.mkdir(parents=True)
    (cone_dir / "genre-tells.md").write_text("stand-in for a symlink entry\n")
    (work / "skills" / "better-writing" / "SKILL.md").write_text("skill\n")

    outside_dir = work / "references"
    outside_dir.mkdir()
    (outside_dir / "genre-tells.md").write_text("the real target content\n")

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

    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare, sha


def _checkout_cone(tmp_path: Path, bare: Path, consumer_name: str) -> Path:
    consumer = tmp_path / consumer_name
    subprocess.run(
        ["git", "clone", "-q", "--local", "--shared", "--no-checkout", str(bare), str(consumer)],
        check=True,
    )
    apply_sparse_cone("git", consumer, ["skills/better-writing"], env=os.environ.copy())
    subprocess.run(["git", "-C", str(consumer), "checkout", "-q", "HEAD"], check=True)
    return consumer


class TestRepairDanglingConeSymlinks:
    def test_no_dangling_symlink_leaves_cone_untouched(self, tmp_path: Path):
        bare, _sha = _build_local_bare_repo(tmp_path)
        consumer = _checkout_cone(tmp_path, bare, "consumer-clean")

        result = repair_dangling_cone_symlinks(
            "git", consumer, ["skills/better-writing"], env=os.environ.copy()
        )

        assert result is None
        # Cone stays narrow: the outside sibling was never materialized.
        assert not (consumer / "references").exists()

    def test_dangling_symlink_falls_back_to_full_checkout(self, tmp_path: Path, monkeypatch):
        bare, _sha = _build_local_bare_repo(tmp_path)
        consumer = _checkout_cone(tmp_path, bare, "consumer-dangling")

        fake_link = consumer / "skills" / "better-writing" / "references" / "genre-tells.md"
        assert fake_link.is_file()  # the stand-in checked out fine

        real_islink = os.path.islink

        def fake_islink(path):
            return True if Path(path) == fake_link else real_islink(path)

        def fake_exists(path):
            if Path(path) == fake_link:
                return (consumer / "references" / "genre-tells.md").exists()
            return os.path.lexists(path)

        monkeypatch.setattr(os.path, "islink", fake_islink)
        monkeypatch.setattr(os.path, "exists", fake_exists)
        monkeypatch.setattr(
            "apm_cli.utils.git_sparse._tracked_symlinks",
            lambda *args, **kwargs: [fake_link],
        )

        result = repair_dangling_cone_symlinks(
            "git", consumer, ["skills/better-writing"], env=os.environ.copy()
        )

        assert result == fake_link
        # The repair must have actually widened the tree, not just
        # reported the problem: the previously cone-excluded sibling
        # directory holding the symlink's target now exists.
        assert (consumer / "references" / "genre-tells.md").is_file()
        assert (
            consumer / "references" / "genre-tells.md"
        ).read_text() == "the real target content\n"

    def test_empty_paths_is_a_noop(self, tmp_path: Path):
        bare, _sha = _build_local_bare_repo(tmp_path)
        consumer = _checkout_cone(tmp_path, bare, "consumer-empty-paths")
        assert repair_dangling_cone_symlinks("git", consumer, [], env=os.environ.copy()) is None
