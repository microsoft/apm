"""Tests for persistent git cache."""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.cache.git_cache import GitCache


class TestGitCacheInit:
    """Test GitCache initialization."""

    def test_creates_bucket_directories(self, tmp_path: Path) -> None:
        GitCache(tmp_path)
        assert (tmp_path / "git" / "db_v1").is_dir()
        assert (tmp_path / "git" / "checkouts_v1").is_dir()


class TestGitCacheResolveSha:
    """Test SHA resolution logic."""

    def test_locked_sha_used_directly(self, tmp_path: Path) -> None:
        cache = GitCache(tmp_path)
        sha = "a" * 40
        result = cache._resolve_sha("https://github.com/owner/repo", "main", locked_sha=sha)
        assert result == sha

    def test_ref_that_looks_like_sha(self, tmp_path: Path) -> None:
        cache = GitCache(tmp_path)
        sha = "b" * 40
        result = cache._resolve_sha("https://github.com/owner/repo", sha)
        assert result == sha

    @patch("subprocess.run")
    def test_ls_remote_resolution(self, mock_run: MagicMock, tmp_path: Path) -> None:
        cache = GitCache(tmp_path)
        expected_sha = "c" * 40
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f"{expected_sha}\trefs/heads/main\n",
        )
        result = cache._resolve_sha("https://github.com/owner/repo", "main")
        assert result == expected_sha


class TestGitCacheGetCheckout:
    """Test the full cache hit/miss flow."""

    @patch("subprocess.run")
    def test_cache_hit_with_integrity_pass(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Cache hit with valid integrity returns the checkout path."""
        cache = GitCache(tmp_path)
        sha = "d" * 40

        # Pre-populate a fake checkout
        from apm_cli.cache.url_normalize import cache_shard_key

        url = "https://github.com/owner/repo"
        real_shard = cache_shard_key(url)
        checkout_dir = tmp_path / "git" / "checkouts_v1" / real_shard / sha
        checkout_dir.mkdir(parents=True)
        (checkout_dir / ".git").mkdir()

        # Mock git rev-parse HEAD to return the expected SHA
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f"{sha}\n",
        )

        result = cache.get_checkout(url, None, locked_sha=sha)
        assert result == checkout_dir

    @patch("subprocess.run")
    def test_cache_hit_integrity_failure_evicts(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Cache hit with integrity failure evicts and re-fetches."""
        cache = GitCache(tmp_path)
        sha = "e" * 40
        wrong_sha = "f" * 40
        url = "https://github.com/owner/repo"

        from apm_cli.cache.url_normalize import cache_shard_key

        real_shard = cache_shard_key(url)
        checkout_dir = tmp_path / "git" / "checkouts_v1" / real_shard / sha
        checkout_dir.mkdir(parents=True)

        # First call: rev-parse returns wrong SHA (integrity failure)
        # Subsequent calls: clone and checkout operations
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            cmd = args[0] if args else kwargs.get("args", [])
            if "rev-parse" in cmd:
                return MagicMock(returncode=0, stdout=f"{wrong_sha}\n")
            elif "cat-file" in cmd:
                return MagicMock(returncode=0, stdout="commit\n")
            else:
                return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        # This should evict the bad entry and attempt a fresh clone
        cache.get_checkout(url, None, locked_sha=sha)

        # The corrupt checkout should have been evicted (then recreated)
        # Verify subprocess was called for clone/checkout after eviction
        assert mock_run.call_count >= 2


class TestGitCacheStats:
    """Test cache statistics."""

    def test_empty_cache(self, tmp_path: Path) -> None:
        cache = GitCache(tmp_path)
        stats = cache.get_cache_stats()
        assert stats["db_count"] == 0
        assert stats["checkout_count"] == 0
        assert stats["total_size_bytes"] == 0

    def test_counts_entries(self, tmp_path: Path) -> None:
        cache = GitCache(tmp_path)
        # Create fake entries
        (tmp_path / "git" / "db_v1" / "shard1").mkdir(parents=True)
        (tmp_path / "git" / "db_v1" / "shard2").mkdir(parents=True)
        (tmp_path / "git" / "checkouts_v1" / "shard1" / "sha1").mkdir(parents=True)

        stats = cache.get_cache_stats()
        assert stats["db_count"] == 2
        assert stats["checkout_count"] == 1


class TestGitCachePrune:
    """Test cache pruning."""

    def test_prune_old_entries(self, tmp_path: Path) -> None:
        cache = GitCache(tmp_path)
        # Create a checkout with old mtime
        shard_dir = tmp_path / "git" / "checkouts_v1" / "shard1"
        old_checkout = shard_dir / "sha_old"
        old_checkout.mkdir(parents=True)
        # Set mtime to 60 days ago
        old_time = time.time() - (60 * 86400)
        os.utime(str(old_checkout), (old_time, old_time))

        # Create a recent checkout
        new_checkout = shard_dir / "sha_new"
        new_checkout.mkdir(parents=True)

        pruned = cache.prune(max_age_days=30)
        assert pruned == 1
        assert not old_checkout.exists()
        assert new_checkout.exists()
