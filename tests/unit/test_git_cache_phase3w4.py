"""Unit tests for apm_cli.cache.git_cache -- phase 3 w4.

Covers the significant coverage gaps in git_cache.py.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cache(tmp_path):
    """Return a GitCache with patched subprocess/lock helpers."""
    with (
        patch("apm_cli.cache.git_cache.cleanup_incomplete"),
        patch("apm_cli.cache.git_cache.shard_lock", return_value=MagicMock()),
        patch("apm_cli.cache.git_cache.atomic_land", return_value=True),
    ):
        from apm_cli.cache.git_cache import GitCache

        return GitCache(tmp_path)


# ---------------------------------------------------------------------------
# _resolve_sha
# ---------------------------------------------------------------------------


class TestResolveSha:
    def test_locked_sha_returns_directly(self, cache):
        sha = "a" * 40
        result = cache._resolve_sha("https://example.com/r.git", "main", locked_sha=sha)
        assert result == sha

    def test_ref_looks_like_sha(self, cache):
        sha = "b" * 40
        result = cache._resolve_sha("https://example.com/r.git", sha)
        assert result == sha

    def test_delegates_to_ls_remote(self, cache):
        sha = "c" * 40
        with patch.object(cache, "_ls_remote_resolve", return_value=sha) as mock_ls:
            result = cache._resolve_sha("https://example.com/r.git", "main")
        mock_ls.assert_called_once_with("https://example.com/r.git", "main", env=None)
        assert result == sha

    def test_locked_sha_lowercased(self, cache):
        sha = ("A" * 40).upper()
        result = cache._resolve_sha("https://example.com/r.git", None, locked_sha=sha)
        assert result == sha.lower()


# ---------------------------------------------------------------------------
# _ls_remote_resolve
# ---------------------------------------------------------------------------


class TestLsRemoteResolve:
    def _make_proc(self, returncode=0, stdout="", stderr=""):
        proc = MagicMock()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = stderr
        return proc

    def test_resolves_head_when_no_ref(self, cache):
        sha = "d" * 40
        proc = self._make_proc(stdout=f"{sha}\tHEAD\n")
        with (
            patch("subprocess.run", return_value=proc),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git", create=True),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}, create=True),
        ):
            result = cache._ls_remote_resolve("https://example.com/r.git", None)
        assert result == sha

    def test_timeout_raises_runtime_error(self, cache):
        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30)),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git", create=True),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}, create=True),
        ):
            with pytest.raises(RuntimeError, match="Failed to resolve ref"):
                cache._ls_remote_resolve("https://example.com/r.git", "main")

    def test_nonzero_returncode_raises_runtime_error(self, cache):
        proc = self._make_proc(returncode=128, stderr="fatal: not found")
        with (
            patch("subprocess.run", return_value=proc),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git", create=True),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}, create=True),
        ):
            with pytest.raises(RuntimeError, match="git ls-remote failed"):
                cache._ls_remote_resolve("https://example.com/r.git", "main")

    def test_resolves_exact_ref_match(self, cache):
        sha = "e" * 40
        proc = self._make_proc(stdout=f"{sha}\trefs/heads/main\n")
        with (
            patch("subprocess.run", return_value=proc),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git", create=True),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}, create=True),
        ):
            result = cache._ls_remote_resolve("https://example.com/r.git", "main")
        assert result == sha

    def test_resolves_tag_ref(self, cache):
        sha = "f" * 40
        proc = self._make_proc(stdout=f"{sha}\trefs/tags/v1.0\n")
        with (
            patch("subprocess.run", return_value=proc),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git", create=True),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}, create=True),
        ):
            result = cache._ls_remote_resolve("https://example.com/r.git", "v1.0")
        assert result == sha

    def test_falls_back_to_first_sha_in_output(self, cache):
        sha = "a1b2c3d4e5" + "0" * 30
        # ref doesn't match but a SHA is present -- fall back
        proc = self._make_proc(stdout=f"{sha}\trefs/heads/other\n")
        with (
            patch("subprocess.run", return_value=proc),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git", create=True),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}, create=True),
        ):
            result = cache._ls_remote_resolve("https://example.com/r.git", "nonexistent-ref")
        assert result == sha.lower()

    def test_no_sha_raises_runtime_error(self, cache):
        proc = self._make_proc(stdout="")
        with (
            patch("subprocess.run", return_value=proc),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git", create=True),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}, create=True),
        ):
            with pytest.raises(RuntimeError, match="Could not resolve ref"):
                cache._ls_remote_resolve("https://example.com/r.git", "missing")

    def test_no_ref_returns_first_sha(self, cache):
        sha = "1" * 40
        proc = self._make_proc(stdout=f"{sha}\tHEAD\n")
        with (
            patch("subprocess.run", return_value=proc),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git", create=True),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}, create=True),
        ):
            result = cache._ls_remote_resolve("https://example.com/r.git", None)
        assert result == sha

    def test_oserror_raises_runtime_error(self, cache):
        with (
            patch("subprocess.run", side_effect=OSError("git not found")),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git", create=True),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}, create=True),
        ):
            with pytest.raises(RuntimeError, match="Failed to resolve ref"):
                cache._ls_remote_resolve("https://example.com/r.git", "main")


# ---------------------------------------------------------------------------
# _bare_has_sha
# ---------------------------------------------------------------------------


class TestBareHasSha:
    def test_returns_true_when_commit_present(self, cache, tmp_path):
        bare_dir = tmp_path / "bare.git"
        bare_dir.mkdir()
        sha = "a" * 40
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "commit\n"
        with (
            patch("subprocess.run", return_value=proc),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git", create=True),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}, create=True),
        ):
            assert cache._bare_has_sha(bare_dir, sha) is True

    def test_returns_false_when_not_present(self, cache, tmp_path):
        bare_dir = tmp_path / "bare2.git"
        bare_dir.mkdir()
        sha = "b" * 40
        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = ""
        with (
            patch("subprocess.run", return_value=proc),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git", create=True),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}, create=True),
        ):
            assert cache._bare_has_sha(bare_dir, sha) is False

    def test_returns_false_on_timeout(self, cache, tmp_path):
        bare_dir = tmp_path / "bare3.git"
        bare_dir.mkdir()
        sha = "c" * 40
        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10)),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git", create=True),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}, create=True),
        ):
            assert cache._bare_has_sha(bare_dir, sha) is False

    def test_returns_false_on_oserror(self, cache, tmp_path):
        bare_dir = tmp_path / "bare4.git"
        bare_dir.mkdir()
        sha = "d" * 40
        with (
            patch("subprocess.run", side_effect=OSError("exec fail")),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git", create=True),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}, create=True),
        ):
            assert cache._bare_has_sha(bare_dir, sha) is False


# ---------------------------------------------------------------------------
# _fetch_into_bare
# ---------------------------------------------------------------------------


class TestFetchIntoBare:
    def test_skips_fetch_when_sha_already_present(self, cache, tmp_path):
        bare_dir = tmp_path / "bare.git"
        bare_dir.mkdir()
        sha = "a" * 40
        lock_mock = MagicMock()
        lock_mock.__enter__ = MagicMock(return_value=lock_mock)
        lock_mock.__exit__ = MagicMock(return_value=False)

        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=lock_mock),
            patch.object(cache, "_bare_has_sha", return_value=True),
            patch.object(cache, "_fetch_into_bare_locked") as mock_fetch,
        ):
            cache._fetch_into_bare(bare_dir, "https://example.com/r.git", sha)
        mock_fetch.assert_not_called()

    def test_calls_fetch_when_sha_absent(self, cache, tmp_path):
        bare_dir = tmp_path / "bare.git"
        bare_dir.mkdir()
        sha = "b" * 40
        lock_mock = MagicMock()
        lock_mock.__enter__ = MagicMock(return_value=lock_mock)
        lock_mock.__exit__ = MagicMock(return_value=False)

        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=lock_mock),
            patch.object(cache, "_bare_has_sha", return_value=False),
            patch.object(cache, "_fetch_into_bare_locked") as mock_fetch,
        ):
            cache._fetch_into_bare(bare_dir, "https://example.com/r.git", sha)
        mock_fetch.assert_called_once()


# ---------------------------------------------------------------------------
# _fetch_into_bare_locked
# ---------------------------------------------------------------------------


class TestFetchIntoBareLoced:
    def test_fetches_by_sha(self, cache, tmp_path):
        bare_dir = tmp_path / "bare.git"
        bare_dir.mkdir()
        sha = "a" * 40
        with (
            patch("subprocess.run") as mock_run,
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git", create=True),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}, create=True),
        ):
            cache._fetch_into_bare_locked(bare_dir, "https://example.com/r.git", sha)
        mock_run.assert_called_once()

    def test_fallback_to_fetch_all_on_error(self, cache, tmp_path):
        bare_dir = tmp_path / "bare.git"
        bare_dir.mkdir()
        sha = "b" * 40

        call_count = [0]

        def _run_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise subprocess.CalledProcessError(1, "git", stderr="not allowed")
            return MagicMock()

        with (
            patch("subprocess.run", side_effect=_run_side_effect),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git", create=True),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}, create=True),
        ):
            cache._fetch_into_bare_locked(bare_dir, "https://example.com/r.git", sha)
        assert call_count[0] == 2  # first failed, then fetch --all


# ---------------------------------------------------------------------------
# _evict_checkout
# ---------------------------------------------------------------------------


class TestEvictCheckout:
    def test_evicts_directory(self, cache, tmp_path):
        checkout_dir = tmp_path / "checkout"
        checkout_dir.mkdir()
        with patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rm:
            cache._evict_checkout(checkout_dir)
        mock_rm.assert_called_once()

    def test_evict_swallows_exception(self, cache, tmp_path):
        checkout_dir = tmp_path / "checkout"
        with patch("apm_cli.utils.file_ops.robust_rmtree", side_effect=OSError("perm denied")):
            # Should not raise
            cache._evict_checkout(checkout_dir)


# ---------------------------------------------------------------------------
# get_cache_stats
# ---------------------------------------------------------------------------


class TestGetCacheStats:
    def test_empty_cache_returns_zeros(self, cache):
        stats = cache.get_cache_stats()
        assert stats["db_count"] == 0
        assert stats["checkout_count"] == 0
        assert stats["total_size_bytes"] == 0

    def test_counts_db_entries(self, cache, tmp_path):
        # Create a fake shard directory in db_root
        shard = cache._db_root / "abc123"
        shard.mkdir(parents=True)
        stats = cache.get_cache_stats()
        assert stats["db_count"] == 1

    def test_counts_checkout_entries(self, cache, tmp_path):
        shard_dir = cache._checkouts_root / "abc123"
        sha_dir = shard_dir / ("a" * 40)
        sha_dir.mkdir(parents=True)
        stats = cache.get_cache_stats()
        assert stats["checkout_count"] == 1


# ---------------------------------------------------------------------------
# clean_all
# ---------------------------------------------------------------------------


class TestCleanAll:
    def test_removes_db_and_checkouts(self, cache, tmp_path):
        shard = cache._db_root / "shard1"
        shard.mkdir(parents=True)
        checkout = cache._checkouts_root / "shard2"
        checkout.mkdir(parents=True)
        # clean_all should call robust_rmtree for each
        removed = []
        with patch(
            "apm_cli.utils.file_ops.robust_rmtree", side_effect=lambda p, **_: removed.append(p)
        ):
            cache.clean_all()
        assert len(removed) >= 2

    def test_removes_lock_files(self, cache, tmp_path):
        lock_file = cache._db_root / "foo.lock"
        lock_file.touch()
        # Should not raise; suppress is used
        cache.clean_all()


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


class TestPrune:
    def test_returns_zero_when_no_checkouts_root(self, cache):
        # If checkouts root doesn't exist as dir, returns 0
        import shutil

        shutil.rmtree(str(cache._checkouts_root))
        result = cache.prune(max_age_days=1)
        assert result == 0

    def test_prunes_old_entries(self, cache, tmp_path):
        import time

        shard = cache._checkouts_root / "shard1"
        sha_dir = shard / ("b" * 40)
        sha_dir.mkdir(parents=True)

        # Make the directory mtime very old
        old_time = time.time() - (90 * 86400)

        with patch("os.scandir") as mock_scandir:
            shard_entry = MagicMock()
            shard_entry.is_dir.return_value = True
            shard_entry.path = str(shard)

            sha_entry = MagicMock()
            sha_entry.is_dir.return_value = True
            sha_entry.path = str(sha_dir)
            sha_stat = MagicMock()
            sha_stat.st_mtime = old_time
            sha_entry.stat.return_value = sha_stat

            # scandir is called twice: once for checkouts_root, once for shard
            mock_scandir.side_effect = [iter([shard_entry]), iter([sha_entry])]

            with patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rm:
                result = cache.prune(max_age_days=30)

        assert result == 1
        mock_rm.assert_called_once()

    def test_skips_recent_entries(self, cache):
        import time

        shard = cache._checkouts_root / "shard1"
        sha_dir = shard / ("c" * 40)
        sha_dir.mkdir(parents=True)

        recent_time = time.time() - (1 * 86400)

        with patch("os.scandir") as mock_scandir:
            shard_entry = MagicMock()
            shard_entry.is_dir.return_value = True
            shard_entry.path = str(shard)

            sha_entry = MagicMock()
            sha_entry.is_dir.return_value = True
            sha_entry.path = str(sha_dir)
            sha_stat = MagicMock()
            sha_stat.st_mtime = recent_time
            sha_entry.stat.return_value = sha_stat

            mock_scandir.side_effect = [iter([shard_entry]), iter([sha_entry])]

            with patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rm:
                result = cache.prune(max_age_days=30)

        assert result == 0
        mock_rm.assert_not_called()


# ---------------------------------------------------------------------------
# _dir_size
# ---------------------------------------------------------------------------


class TestDirSize:
    def test_empty_dir_is_zero(self, tmp_path):
        from apm_cli.cache.git_cache import _dir_size

        d = tmp_path / "empty"
        d.mkdir()
        assert _dir_size(d) == 0

    def test_counts_file_sizes(self, tmp_path):
        from apm_cli.cache.git_cache import _dir_size

        d = tmp_path / "data"
        d.mkdir()
        (d / "file.txt").write_bytes(b"hello")
        result = _dir_size(d)
        assert result == 5

    def test_nonexistent_dir_returns_zero(self, tmp_path):
        from apm_cli.cache.git_cache import _dir_size

        result = _dir_size(tmp_path / "nonexistent")
        assert result == 0


# ---------------------------------------------------------------------------
# _sanitize_url
# ---------------------------------------------------------------------------


class TestSanitizeUrl:
    def test_no_credentials_unchanged(self):
        from apm_cli.cache.git_cache import _sanitize_url

        url = "https://github.com/org/repo.git"
        assert _sanitize_url(url) == url

    def test_password_replaced(self):
        from apm_cli.cache.git_cache import _sanitize_url

        url = "https://user:secret@github.com/org/repo.git"
        result = _sanitize_url(url)
        assert "secret" not in result
        assert "***" in result
        assert "user" in result

    def test_bad_url_returns_original(self):
        from apm_cli.cache.git_cache import _sanitize_url

        result = _sanitize_url("not a url")
        assert result == "not a url"


# ---------------------------------------------------------------------------
# get_checkout -- cache hit with integrity failure
# ---------------------------------------------------------------------------


class TestGetCheckoutIntegrityFailure:
    def test_evicts_on_integrity_failure(self, tmp_path):
        """When checkout exists but integrity check fails, evict and refetch."""
        with (
            patch("apm_cli.cache.git_cache.cleanup_incomplete"),
            patch("apm_cli.cache.git_cache.shard_lock", return_value=MagicMock()),
            patch("apm_cli.cache.git_cache.atomic_land", return_value=True),
        ):
            from apm_cli.cache.git_cache import GitCache

            gc = GitCache(tmp_path)

        sha = "a" * 40
        shard_key = "someshard"
        checkout_dir = gc._checkouts_root / shard_key / sha
        checkout_dir.mkdir(parents=True)

        with (
            patch.object(gc, "_resolve_sha", return_value=sha),
            patch("apm_cli.cache.git_cache.cache_shard_key", return_value=shard_key),
            patch("apm_cli.cache.git_cache.verify_checkout_sha", return_value=False),
            patch.object(gc, "_evict_checkout") as mock_evict,
            patch.object(gc, "_ensure_bare_repo"),
            patch.object(gc, "_create_checkout", return_value=checkout_dir),
        ):
            result = gc.get_checkout("https://example.com/r.git", "main")
            del result  # return value not checked; side effect (evict) is what matters

        mock_evict.assert_called_once_with(checkout_dir)
