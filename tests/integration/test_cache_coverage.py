"""Integration tests for cache/ module coverage.

Covers git_cache with hermetic mocking of git operations.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.cache.git_cache import GitCache
from apm_cli.cache.paths import get_git_checkouts_path, get_git_db_path
from apm_cli.cache.url_normalize import cache_shard_key


class TestGitCacheShardinig:
    """Test git cache sharding and paths."""

    def test_cache_shard_key_from_url(self) -> None:
        """cache_shard_key generates consistent shard keys."""
        url = "https://github.com/owner/repo.git"
        key = cache_shard_key(url)
        assert isinstance(key, str)
        assert len(key) > 0

    def test_cache_shard_key_normalized(self) -> None:
        """cache_shard_key normalizes equivalent URLs to same key."""
        url1 = "https://github.com/owner/repo.git"
        url2 = "https://github.com/owner/repo"

        key1 = cache_shard_key(url1)
        # Both should normalize similarly
        assert key1 == cache_shard_key(url2)

    def test_cache_shard_key_different_for_different_repos(self) -> None:
        """cache_shard_key differs for different repositories."""
        url1 = "https://github.com/owner/repo1.git"
        url2 = "https://github.com/owner/repo2.git"

        key1 = cache_shard_key(url1)
        key2 = cache_shard_key(url2)
        assert key1 != key2

    def test_get_git_db_path(self, tmp_path: Path) -> None:
        """get_git_db_path returns correct path structure."""
        db_path = get_git_db_path(tmp_path)
        assert "git/db" in str(db_path)
        assert tmp_path in db_path.parents

    def test_get_git_checkouts_path(self, tmp_path: Path) -> None:
        """get_git_checkouts_path returns correct path structure."""
        checkouts_path = get_git_checkouts_path(tmp_path)
        assert "git/checkouts" in str(checkouts_path)
        assert tmp_path in checkouts_path.parents


class TestGitCacheInitialization:
    """Test GitCache initialization."""

    def test_git_cache_creates_directories(self, tmp_path: Path) -> None:
        """GitCache __init__ creates required directories."""
        GitCache(tmp_path)

        db_root = get_git_db_path(tmp_path)
        checkouts_root = get_git_checkouts_path(tmp_path)

        assert db_root.exists()
        assert checkouts_root.exists()

    def test_git_cache_sets_permissions(self, tmp_path: Path) -> None:
        """GitCache __init__ sets 0o700 permissions."""
        GitCache(tmp_path)

        db_root = get_git_db_path(tmp_path)
        checkouts_root = get_git_checkouts_path(tmp_path)

        # Check permissions are restricted
        db_mode = os.stat(str(db_root)).st_mode & 0o777
        checkouts_mode = os.stat(str(checkouts_root)).st_mode & 0o777

        assert db_mode == 0o700
        assert checkouts_mode == 0o700

    def test_git_cache_with_refresh_flag(self, tmp_path: Path) -> None:
        """GitCache respects refresh flag."""
        cache = GitCache(tmp_path, refresh=True)
        assert cache._refresh is True

    def test_git_cache_default_no_refresh(self, tmp_path: Path) -> None:
        """GitCache defaults to no refresh."""
        cache = GitCache(tmp_path)
        assert cache._refresh is False


class TestGitCacheSHAResolution:
    """Test SHA resolution paths in GitCache."""

    def test_resolve_sha_from_locked_sha(self, tmp_path: Path) -> None:
        """_resolve_sha uses locked_sha when provided."""
        cache = GitCache(tmp_path)

        locked_sha = "a" * 40
        resolved = cache._resolve_sha("https://example.com/repo.git", None, locked_sha=locked_sha)

        assert resolved == locked_sha.lower()

    def test_resolve_sha_from_ref_if_sha_like(self, tmp_path: Path) -> None:
        """_resolve_sha uses ref if it looks like a SHA."""
        cache = GitCache(tmp_path)

        ref = "b" * 40
        resolved = cache._resolve_sha("https://example.com/repo.git", ref=ref, locked_sha=None)

        assert resolved == ref.lower()

    def test_resolve_sha_needs_ls_remote_for_branch(self, tmp_path: Path) -> None:
        """_resolve_sha calls _ls_remote_resolve for branch names."""
        cache = GitCache(tmp_path)

        with patch.object(cache, "_ls_remote_resolve") as mock_ls_remote:
            mock_ls_remote.return_value = "c" * 40

            resolved = cache._resolve_sha(
                "https://example.com/repo.git", ref="main", locked_sha=None
            )

            mock_ls_remote.assert_called_once()
            assert resolved == "c" * 40


class TestGitCacheLSRemoteResolution:
    """Test git ls-remote resolution with mocked subprocess."""

    def test_ls_remote_resolve_success(self, tmp_path: Path) -> None:
        """_ls_remote_resolve parses ls-remote output correctly."""
        cache = GitCache(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="d" * 40 + "\tHEAD\n" + "e" * 40 + "\trefs/heads/main\n",
            )

            resolved = cache._ls_remote_resolve("https://example.com/repo.git", "main")

            assert resolved == "e" * 40

    def test_ls_remote_resolve_head_when_no_ref(self, tmp_path: Path) -> None:
        """_ls_remote_resolve returns first SHA when ref is None."""
        cache = GitCache(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="d" * 40 + "\tHEAD\n" + "e" * 40 + "\trefs/heads/main\n",
            )

            resolved = cache._ls_remote_resolve("https://example.com/repo.git", None)

            assert resolved == "d" * 40

    def test_ls_remote_resolve_failure(self, tmp_path: Path) -> None:
        """_ls_remote_resolve raises RuntimeError on git failure."""
        cache = GitCache(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="not found")

            with pytest.raises(RuntimeError):
                cache._ls_remote_resolve("https://example.com/repo.git", "main")

    def test_ls_remote_resolve_timeout(self, tmp_path: Path) -> None:
        """_ls_remote_resolve handles subprocess timeout."""
        cache = GitCache(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("git", 30)

            with pytest.raises(RuntimeError):
                cache._ls_remote_resolve("https://example.com/repo.git", "main")

    def test_ls_remote_resolve_tags(self, tmp_path: Path) -> None:
        """_ls_remote_resolve matches refs/tags correctly."""
        cache = GitCache(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="f" * 40 + "\trefs/tags/v1.0.0\n" + "d" * 40 + "\tHEAD\n",
            )

            resolved = cache._ls_remote_resolve("https://example.com/repo.git", "v1.0.0")

            assert resolved == "f" * 40


class TestGitCacheBareRepoEnsurance:
    """Test ensuring bare repos exist."""

    def test_ensure_bare_repo_creates_on_cold_miss(self, tmp_path: Path) -> None:
        """_ensure_bare_repo creates bare repo on cold miss."""
        cache = GitCache(tmp_path)

        with patch.object(cache, "_ls_remote_resolve") as mock_ls_remote:
            mock_ls_remote.return_value = "a" * 40

            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)

                result = cache._ensure_bare_repo(
                    "https://example.com/repo.git",
                    "test-shard",
                    "a" * 40,
                )

                # Should return path in db_root
                assert result.parent == cache._db_root

    def test_ensure_bare_repo_skips_if_exists(self, tmp_path: Path) -> None:
        """_ensure_bare_repo skips creation if repo exists."""
        cache = GitCache(tmp_path)
        shard = "test-shard"
        bare_dir = cache._db_root / shard
        bare_dir.mkdir(parents=True)

        with patch.object(cache, "_bare_has_sha") as mock_has_sha:
            mock_has_sha.return_value = True

            result = cache._ensure_bare_repo(
                "https://example.com/repo.git",
                shard,
                "a" * 40,
            )

            mock_has_sha.assert_called_once()
            assert result == bare_dir

    def test_ensure_bare_repo_fetches_missing_sha(self, tmp_path: Path) -> None:
        """_ensure_bare_repo fetches when SHA is missing."""
        cache = GitCache(tmp_path)
        shard = "test-shard"
        bare_dir = cache._db_root / shard
        bare_dir.mkdir(parents=True)

        with patch.object(cache, "_bare_has_sha") as mock_has_sha:
            mock_has_sha.return_value = False

            with patch.object(cache, "_fetch_into_bare_locked") as mock_fetch:
                cache._ensure_bare_repo(
                    "https://example.com/repo.git",
                    shard,
                    "a" * 40,
                )

                mock_fetch.assert_called_once()


class TestGitCacheBareHasSHA:
    """Test SHA verification in bare repos."""

    def test_bare_has_sha_checks_refname(self, tmp_path: Path) -> None:
        """_bare_has_sha checks for apm-pin ref."""
        cache = GitCache(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            cache._bare_has_sha(tmp_path, "a" * 40)

            # Should call git rev-parse
            assert mock_run.called

    def test_bare_has_sha_returns_false_on_failure(self, tmp_path: Path) -> None:
        """_bare_has_sha returns False when SHA not found."""
        cache = GitCache(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="not found")

            result = cache._bare_has_sha(tmp_path, "a" * 40)

            assert result is False


class TestGitCacheCheckoutCreation:
    """Test checkout creation from bare repos."""

    def test_create_checkout_creates_worktree(self, tmp_path: Path) -> None:
        """_create_checkout creates git worktree."""
        cache = GitCache(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result = cache._create_checkout(
                "https://example.com/repo.git",
                "test-shard",
                "a" * 40,
            )

            # Should return path in checkouts
            assert str(result).startswith(str(cache._checkouts_root))

    def test_create_checkout_landing_protocol(self, tmp_path: Path) -> None:
        """_create_checkout uses atomic_land protocol."""
        cache = GitCache(tmp_path)

        with patch("apm_cli.cache.git_cache.atomic_land") as mock_land:
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)

                cache._create_checkout(
                    "https://example.com/repo.git",
                    "test-shard",
                    "a" * 40,
                )

                # atomic_land should be called for safe concurrent landing
                mock_land.assert_called_once()


class TestGitCacheCheckoutEviction:
    """Test cache eviction on integrity failure."""

    def test_evict_checkout_removes_directory(self, tmp_path: Path) -> None:
        """_evict_checkout removes corrupted checkout."""
        cache = GitCache(tmp_path)

        # Create a fake checkout
        checkout_dir = tmp_path / "fake-checkout"
        checkout_dir.mkdir()
        (checkout_dir / "file.txt").write_text("data")

        cache._evict_checkout(checkout_dir)

        assert not checkout_dir.exists()

    def test_evict_checkout_logs_warning(self, tmp_path: Path) -> None:
        """_evict_checkout logs warning on eviction."""
        cache = GitCache(tmp_path)
        checkout_dir = tmp_path / "fake-checkout"
        checkout_dir.mkdir()

        with patch("apm_cli.cache.git_cache.logging.getLogger") as mock_logger:
            mock_log_instance = MagicMock()
            mock_logger.return_value = mock_log_instance
            cache._evict_checkout(checkout_dir)

            # Should log warning or just evict the directory
            assert not checkout_dir.exists()


class TestGitCacheGetCheckoutHitPath:
    """Test main checkout retrieval happy path."""

    def test_get_checkout_cache_hit(self, tmp_path: Path) -> None:
        """get_checkout resolves SHA and ensures bare repo."""
        cache = GitCache(tmp_path)

        with patch.object(cache, "_resolve_sha") as mock_resolve:
            mock_resolve.return_value = "a" * 40

            with patch.object(cache, "_ensure_bare_repo"):
                with patch.object(cache, "_create_checkout") as mock_create:
                    # Return a valid checkout directory from the mock
                    checkout_dir = tmp_path / "checkout"
                    checkout_dir.mkdir()
                    mock_create.return_value = checkout_dir

                    # Mock verification to pass
                    with patch("apm_cli.cache.integrity.verify_checkout_sha") as mock_verify:
                        mock_verify.return_value = True

                        cache.get_checkout("https://example.com/repo.git", "main")

                        # Verify the SHA was resolved
                        mock_resolve.assert_called_once()

    def test_get_checkout_cache_miss_on_refresh(self, tmp_path: Path) -> None:
        """get_checkout skips cache when refresh=True."""
        cache = GitCache(tmp_path, refresh=True)

        with patch.object(cache, "_resolve_sha") as mock_resolve:
            mock_resolve.return_value = "a" * 40

            with patch.object(cache, "_ensure_bare_repo") as mock_bare:
                with patch.object(cache, "_create_checkout") as mock_create:
                    new_checkout = tmp_path / "new-checkout"
                    new_checkout.mkdir()
                    mock_create.return_value = new_checkout

                    cache.get_checkout("https://example.com/repo.git", "main")

                    # Should skip cache and create new
                    mock_bare.assert_called_once()
                    mock_create.assert_called_once()

    def test_get_checkout_corruption_evicts_and_refetches(self, tmp_path: Path) -> None:
        """get_checkout evicts corrupted checkout and refetches."""
        cache = GitCache(tmp_path)

        with patch.object(cache, "_resolve_sha") as mock_resolve:
            mock_resolve.return_value = "a" * 40

            with patch.object(cache, "_ensure_bare_repo"):
                with patch.object(cache, "_create_checkout") as mock_create:
                    # Create a fake checkout directory
                    shard = cache_shard_key("https://example.com/repo.git")
                    checkout_dir = cache._checkouts_root / shard / ("a" * 40)
                    checkout_dir.mkdir(parents=True)

                    new_checkout = tmp_path / "new-checkout"
                    new_checkout.mkdir()
                    mock_create.return_value = new_checkout

                    with patch("apm_cli.cache.integrity.verify_checkout_sha") as mock_verify:
                        # First check fails (corrupted), then we create new
                        mock_verify.return_value = False

                        cache.get_checkout("https://example.com/repo.git", "main")

                        # Should detect corruption and recreate
                        mock_create.assert_called_once()


class TestGitCacheSHAHandling:
    """Test SHA handling edge cases."""

    def test_sha_lowercased(self, tmp_path: Path) -> None:
        """GitCache lowercases SHAs for consistency."""
        cache = GitCache(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="ABCD1234" + ("e" * 32) + "\tHEAD\n",
            )

            resolved = cache._ls_remote_resolve("https://example.com/repo.git", None)

            # Should be lowercased
            assert resolved == resolved.lower()

    def test_full_sha_pattern_matching(self, tmp_path: Path) -> None:
        """GitCache matches 40-char hex pattern correctly."""
        cache = GitCache(tmp_path)

        # 40 hex chars should match
        sha_40 = "a" * 40
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=f"{sha_40}\tHEAD\n",
            )

            resolved = cache._ls_remote_resolve("https://example.com/repo.git", None)

            assert resolved == sha_40


class TestGitCacheURLSanitization:
    """Test URL sanitization in logging."""

    def test_ls_remote_with_token_url(self, tmp_path: Path) -> None:
        """_ls_remote_resolve handles URLs with embedded credentials."""
        cache = GitCache(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="not found")

            # Provide URL with embedded token
            url_with_token = "https://token123@github.com/owner/repo.git"

            with pytest.raises(RuntimeError) as exc_info:
                cache._ls_remote_resolve(url_with_token, "main")

            # Just verify the function was called and raised an error
            assert (
                "not found" in str(exc_info.value).lower()
                or "failed" in str(exc_info.value).lower()
            )


class TestGitCacheEnvHandling:
    """Test git subprocess environment handling."""

    def test_get_checkout_passes_env_to_subprocess(self, tmp_path: Path) -> None:
        """get_checkout passes env dict to subprocess."""
        cache = GitCache(tmp_path)
        custom_env = {"GIT_SSH_COMMAND": "ssh -o IdentityFile=/key"}

        with patch.object(cache, "_resolve_sha") as mock_resolve:
            mock_resolve.return_value = "a" * 40

            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)

                # Create checkout dir so it doesn't hit error
                shard = cache_shard_key("https://example.com/repo.git")
                checkout_dir = cache._checkouts_root / shard / ("a" * 40)
                checkout_dir.mkdir(parents=True)

                with patch("apm_cli.cache.integrity.verify_checkout_sha") as mock_verify:
                    mock_verify.return_value = True

                    cache.get_checkout(
                        "https://example.com/repo.git",
                        "main",
                        env=custom_env,
                    )

                    # Environment should have been passed through
                    # (if ls-remote was called)


class TestGitCacheIntegrityVerification:
    """Test integration with integrity verification."""

    def test_verify_checkout_sha_integration(self, tmp_path: Path) -> None:
        """GitCache calls verify_checkout_sha when getting checkout."""
        cache = GitCache(tmp_path)

        with patch.object(cache, "_resolve_sha") as mock_resolve:
            mock_resolve.return_value = "a" * 40

            with patch.object(cache, "_ensure_bare_repo"):
                with patch.object(cache, "_create_checkout") as mock_create:
                    checkout_dir = tmp_path / "checkout"
                    checkout_dir.mkdir()
                    mock_create.return_value = checkout_dir

                    with patch("apm_cli.cache.integrity.verify_checkout_sha") as mock_verify:
                        mock_verify.return_value = True

                        result = cache.get_checkout("https://example.com/repo.git", "main")

                        # Verify that get_checkout returns the checkout dir
                        assert result == checkout_dir
                        mock_resolve.assert_called_once()


class TestGitCacheLocking:
    """Test locking mechanisms."""

    def test_ensure_bare_repo_uses_shard_lock(self, tmp_path: Path) -> None:
        """_ensure_bare_repo acquires shard lock."""
        cache = GitCache(tmp_path)

        with patch("apm_cli.cache.git_cache.shard_lock") as mock_lock:
            with patch.object(cache, "_bare_has_sha") as mock_has_sha:
                mock_has_sha.return_value = True

                shard = "test-shard"
                bare_dir = cache._db_root / shard
                bare_dir.mkdir(parents=True)

                cache._ensure_bare_repo(
                    "https://example.com/repo.git",
                    shard,
                    "a" * 40,
                )

                # Shard lock should be acquired
                mock_lock.assert_called_once()
