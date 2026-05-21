"""Tests for sparse-cone checkout support in GitCache (perf #1433)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from apm_cli.cache.git_cache import GitCache, _variant_key


@pytest.fixture(autouse=True)
def _allow_bare_repos(monkeypatch):
    """Override safe.bareRepository so `git -C <bare>` works in test env."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.bareRepository")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "all")


class TestVariantKey:
    """Variant key derivation must be deterministic and order-independent."""

    def test_empty_or_none_is_full(self):
        assert _variant_key(None) == "full"
        assert _variant_key([]) == "full"

    def test_sparse_paths_produce_sparse_prefix(self):
        v = _variant_key(["plugins/x"])
        assert v.startswith("sparse-")
        # 16 hex chars after the prefix
        assert len(v) == len("sparse-") + 16

    def test_order_independent(self):
        assert _variant_key(["a", "b"]) == _variant_key(["b", "a"])

    def test_distinct_sets_distinct_keys(self):
        assert _variant_key(["a"]) != _variant_key(["b"])
        assert _variant_key(["a", "b"]) != _variant_key(["a"])

    def test_deterministic_across_calls(self):
        v1 = _variant_key(["plugins/x", "tools/y"])
        v2 = _variant_key(["tools/y", "plugins/x"])
        assert v1 == v2


def _build_local_bare_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a local git repo with multiple top-level subdirs and a bare clone.

    Returns (bare_path, head_sha).
    """
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)

    for sub in ("alpha", "beta", "gamma"):
        d = work / sub
        d.mkdir()
        (d / "file.txt").write_text(f"{sub}\n", encoding="utf-8")
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


class TestGetCheckoutLayout:
    """get_checkout must land checkouts at <shard>/<sha>/<variant>/."""

    def test_full_variant_layout(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = f"file://{bare}"
        result = cache.get_checkout(url, "main", locked_sha=sha)
        assert result.name == "full"
        assert result.parent.name == sha
        assert (result / "alpha" / "file.txt").is_file()
        assert (result / "beta" / "file.txt").is_file()
        assert (result / "gamma" / "file.txt").is_file()

    def test_sparse_variant_layout_only_requested_subdir(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = f"file://{bare}"
        result = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])
        assert result.name.startswith("sparse-")
        assert result.parent.name == sha
        assert (result / "alpha" / "file.txt").is_file()
        # Sparse-cone excludes other top-level dirs:
        assert not (result / "beta").exists()
        assert not (result / "gamma").exists()

    def test_full_and_sparse_coexist(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = f"file://{bare}"
        full = cache.get_checkout(url, "main", locked_sha=sha)
        sparse = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])
        # Both live under same SHA parent, different variant subdirs.
        assert full.parent == sparse.parent
        assert full != sparse
        assert full.is_dir()
        assert sparse.is_dir()

    def test_two_distinct_sparse_sets_separate_shards(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = f"file://{bare}"
        a = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])
        b = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["beta"])
        assert a != b
        assert a.parent == b.parent
        assert (a / "alpha").is_dir()
        assert not (a / "beta").exists()
        assert (b / "beta").is_dir()
        assert not (b / "alpha").exists()
