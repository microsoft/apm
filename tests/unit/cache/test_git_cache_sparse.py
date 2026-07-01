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

        url = bare.as_uri()
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

        url = bare.as_uri()
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

        url = bare.as_uri()
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

        url = bare.as_uri()
        a = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])
        b = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["beta"])
        assert a != b
        assert a.parent == b.parent
        assert (a / "alpha").is_dir()
        assert not (a / "beta").exists()
        assert (b / "beta").is_dir()
        assert not (b / "alpha").exists()


class TestPartialBareFlavor:
    """Partial-clone (perf #1433 follow-up): sparse callers should
    use the ``__p`` bare flavor and the consumer should be configured
    as a promisor."""

    def test_sparse_caller_uses_partial_bare_dir(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = bare.as_uri()
        cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])

        # The partial-flavor bare lives at <shard>__p.
        bare_root = cache_root / "git" / "db_v1"
        partial_bares = [p for p in bare_root.iterdir() if p.is_dir() and p.name.endswith("__p")]
        assert len(partial_bares) == 1
        full_bares = [p for p in bare_root.iterdir() if p.is_dir() and not p.name.endswith("__p")]
        assert len(full_bares) == 0

    def test_full_caller_uses_non_partial_bare_dir(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = bare.as_uri()
        cache.get_checkout(url, "main", locked_sha=sha)

        bare_root = cache_root / "git" / "db_v1"
        partial_bares = [p for p in bare_root.iterdir() if p.is_dir() and p.name.endswith("__p")]
        assert len(partial_bares) == 0
        full_bares = [p for p in bare_root.iterdir() if p.is_dir() and not p.name.endswith("__p")]
        assert len(full_bares) == 1

    def test_full_and_sparse_callers_coexist_as_separate_bare_flavors(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = bare.as_uri()
        cache.get_checkout(url, "main", locked_sha=sha)
        cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])

        bare_root = cache_root / "git" / "db_v1"
        names = sorted(p.name for p in bare_root.iterdir() if p.is_dir())
        assert len(names) == 2
        assert sum(1 for n in names if n.endswith("__p")) == 1
        assert sum(1 for n in names if not n.endswith("__p")) == 1

    def test_promisor_config_set_on_sparse_consumer(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = bare.as_uri()
        result = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])

        # Consumer's remote.origin.url must point at the promisor URL,
        # not the local bare path, so lazy blob fetch can reach upstream.
        cfg = subprocess.run(
            ["git", "-C", str(result), "config", "remote.origin.url"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert cfg == url
        promisor = subprocess.run(
            ["git", "-C", str(result), "config", "remote.origin.promisor"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert promisor == "true"
        pfilter = subprocess.run(
            ["git", "-C", str(result), "config", "remote.origin.partialclonefilter"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert pfilter == "blob:none"

    def test_promisor_setup_uses_single_config_spawn(self, tmp_path: Path, monkeypatch):
        """The promisor keys ride on the clone via ``-c``; only ``remote.origin.url``
        needs a post-clone ``git config`` (clone always rewrites url to the source).

        Guards the spawn-count reduction: three ``git config`` subprocesses
        collapsed to one, which matters on hosts where each process exec is
        expensive.
        """
        bare, sha = _build_local_bare_repo(tmp_path)
        cache = GitCache(tmp_path / "cache")

        real_run = subprocess.run
        clone_cmds: list[list[str]] = []
        config_cmds: list[list[str]] = []

        def _spy(cmd, *args, **kwargs):
            if isinstance(cmd, list) and "clone" in cmd:
                clone_cmds.append(cmd)
            if isinstance(cmd, list) and "config" in cmd:
                config_cmds.append(cmd)
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _spy)

        url = bare.as_uri()
        cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])

        # The consumer clone carries the two clone-survivable promisor keys.
        consumer_clone = next(c for c in clone_cmds if "--no-checkout" in c and "--shared" in c)
        joined = " ".join(consumer_clone)
        assert "remote.origin.promisor=true" in joined
        assert "remote.origin.partialclonefilter=blob:none" in joined
        # Exactly one post-clone `git config` -- for remote.origin.url only.
        url_configs = [c for c in config_cmds if "remote.origin.url" in c]
        assert len(url_configs) == 1
        assert not any("remote.origin.promisor" in c for c in config_cmds)
        assert not any("remote.origin.partialclonefilter" in c for c in config_cmds)

    def test_full_consumer_has_no_promisor_config(self, tmp_path: Path):
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        url = bare.as_uri()
        result = cache.get_checkout(url, "main", locked_sha=sha)

        # Full path: no promisor config; remote.origin.url points at
        # local bare (default `clone --local` behavior).
        rc = subprocess.run(
            ["git", "-C", str(result), "config", "remote.origin.promisor"],
            capture_output=True,
            text=True,
        )
        assert rc.returncode != 0  # config key not set

    def test_partial_clone_fallback_to_full_on_server_rejection(self, tmp_path: Path, monkeypatch):
        """Server rejects --filter=blob:none -> retry without filter succeeds.

        Older Gerrit / pre-2.20 GHE do not support filter v2. The cache
        must transparently degrade to a full bare clone (baseline
        behavior) rather than fail the install.
        """
        bare, sha = _build_local_bare_repo(tmp_path)
        cache_root = tmp_path / "cache"
        cache = GitCache(cache_root)

        import apm_cli.cache.git_cache as git_cache_mod

        real_run = subprocess.run
        rejected: list[list[str]] = []
        retried: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and "--filter=blob:none" in cmd:
                rejected.append(list(cmd))
                raise subprocess.CalledProcessError(
                    128, cmd, output=b"", stderr=b"fatal: server does not support filter"
                )
            if (
                isinstance(cmd, list)
                and "clone" in cmd
                and "--bare" in cmd
                and "--filter=blob:none" not in cmd
            ):
                retried.append(list(cmd))
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(git_cache_mod.subprocess, "run", fake_run)

        url = bare.as_uri()
        result = cache.get_checkout(url, "main", locked_sha=sha, sparse_paths=["alpha"])

        assert rejected, "partial clone (with --filter) should have been attempted"
        assert retried, "fallback retry (without --filter) should have been issued"
        assert all("--filter=blob:none" not in c for c in retried)
        assert (result / "alpha" / "file.txt").is_file()
