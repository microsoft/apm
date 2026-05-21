"""Integration tests for ``apm_cli.cache.git_cache``.

All tests are hermetic: git subprocesses, locking, integrity checks, and
cleanup helpers are mocked while still exercising real filesystem paths under
``tmp_path``.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.cache.git_cache import GitCache, _dir_size, _sanitize_url
from apm_cli.cache.url_normalize import cache_shard_key


def _proc(*, returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


@pytest.fixture()
def cache(tmp_path: Path) -> GitCache:
    with (
        patch("apm_cli.cache.git_cache.cleanup_incomplete"),
        patch("apm_cli.cache.git_cache.os.chmod"),
    ):
        return GitCache(tmp_path)


class TestGitCacheInit:
    def test_init_creates_directories_and_calls_cleanup(self, tmp_path: Path) -> None:
        with (
            patch("apm_cli.cache.git_cache.cleanup_incomplete") as mock_cleanup,
            patch("apm_cli.cache.git_cache.os.chmod") as mock_chmod,
        ):
            cache = GitCache(tmp_path)

        assert cache._db_root.is_dir()
        assert cache._checkouts_root.is_dir()
        assert mock_cleanup.call_args_list == [
            ((cache._db_root,), {}),
            ((cache._checkouts_root,), {}),
        ]
        assert mock_chmod.call_args_list == [
            ((str(cache._db_root), 0o700), {}),
            ((str(cache._checkouts_root), 0o700), {}),
        ]

    def test_init_reuses_existing_directories(self, tmp_path: Path) -> None:
        db_root = tmp_path / "git" / "db_v1"
        checkouts_root = tmp_path / "git" / "checkouts_v1"
        db_root.mkdir(parents=True)
        checkouts_root.mkdir(parents=True)

        with (
            patch("apm_cli.cache.git_cache.cleanup_incomplete"),
            patch("apm_cli.cache.git_cache.os.chmod"),
        ):
            cache = GitCache(tmp_path)

        assert cache._db_root == db_root
        assert cache._checkouts_root == checkouts_root


class TestGetCheckout:
    def test_cache_hit_returns_existing_checkout(self, cache: GitCache) -> None:
        url = "https://github.com/owner/repo.git"
        sha = "a" * 40
        checkout_dir = cache._checkouts_root / cache_shard_key(url) / sha
        checkout_dir.mkdir(parents=True)

        with (
            patch.object(cache, "_resolve_sha", return_value=sha),
            patch("apm_cli.cache.git_cache.verify_checkout_sha", return_value=True),
            patch.object(cache, "_ensure_bare_repo") as mock_ensure,
            patch.object(cache, "_create_checkout") as mock_create,
        ):
            result = cache.get_checkout(url, "main")

        assert result == checkout_dir
        mock_ensure.assert_not_called()
        mock_create.assert_not_called()

    def test_cache_hit_with_failed_integrity_evicts_and_recreates(self, cache: GitCache) -> None:
        url = "https://github.com/owner/repo.git"
        sha = "b" * 40
        checkout_dir = cache._checkouts_root / cache_shard_key(url) / sha
        checkout_dir.mkdir(parents=True)
        recreated = checkout_dir.parent / "recreated"

        with (
            patch.object(cache, "_resolve_sha", return_value=sha),
            patch("apm_cli.cache.git_cache.verify_checkout_sha", return_value=False),
            patch.object(cache, "_evict_checkout") as mock_evict,
            patch.object(cache, "_ensure_bare_repo") as mock_ensure,
            patch.object(cache, "_create_checkout", return_value=recreated) as mock_create,
        ):
            result = cache.get_checkout(url, "main")

        assert result == recreated
        mock_evict.assert_called_once_with(checkout_dir)
        mock_ensure.assert_called_once_with(url, cache_shard_key(url), sha, env=None)
        mock_create.assert_called_once_with(url, cache_shard_key(url), sha, env=None)

    def test_refresh_ignores_existing_checkout(self, tmp_path: Path) -> None:
        with (
            patch("apm_cli.cache.git_cache.cleanup_incomplete"),
            patch("apm_cli.cache.git_cache.os.chmod"),
        ):
            cache = GitCache(tmp_path, refresh=True)
        url = "https://github.com/owner/repo.git"
        sha = "c" * 40
        checkout_dir = cache._checkouts_root / cache_shard_key(url) / sha
        checkout_dir.mkdir(parents=True)

        with (
            patch.object(cache, "_resolve_sha", return_value=sha),
            patch("apm_cli.cache.git_cache.verify_checkout_sha") as mock_verify,
            patch.object(cache, "_ensure_bare_repo") as mock_ensure,
            patch.object(cache, "_create_checkout", return_value=checkout_dir) as mock_create,
        ):
            result = cache.get_checkout(url, "main")

        assert result == checkout_dir
        mock_verify.assert_not_called()
        mock_ensure.assert_called_once()
        mock_create.assert_called_once()

    def test_cache_miss_creates_checkout(self, cache: GitCache) -> None:
        url = "https://github.com/owner/repo.git"
        sha = "d" * 40
        checkout_dir = cache._checkouts_root / cache_shard_key(url) / sha

        with (
            patch.object(cache, "_resolve_sha", return_value=sha),
            patch.object(cache, "_ensure_bare_repo") as mock_ensure,
            patch.object(cache, "_create_checkout", return_value=checkout_dir) as mock_create,
        ):
            result = cache.get_checkout(url, None, locked_sha=sha, env={"A": "1"})

        assert result == checkout_dir
        mock_ensure.assert_called_once_with(url, cache_shard_key(url), sha, env={"A": "1"})
        mock_create.assert_called_once_with(url, cache_shard_key(url), sha, env={"A": "1"})


class TestResolveSha:
    def test_locked_sha_takes_priority_and_is_lowercased(self, cache: GitCache) -> None:
        sha = "A" * 40
        assert (
            cache._resolve_sha("https://example.com/repo.git", "main", locked_sha=sha)
            == sha.lower()
        )

    def test_invalid_locked_sha_falls_back_to_ref_sha(self, cache: GitCache) -> None:
        sha = "b" * 40
        assert cache._resolve_sha("https://example.com/repo.git", sha, locked_sha="short") == sha

    def test_ref_that_is_full_sha_is_lowercased(self, cache: GitCache) -> None:
        sha = "C" * 40
        assert cache._resolve_sha("https://example.com/repo.git", sha) == sha.lower()

    def test_non_sha_ref_uses_ls_remote(self, cache: GitCache) -> None:
        with patch.object(cache, "_ls_remote_resolve", return_value="d" * 40) as mock_resolve:
            result = cache._resolve_sha("https://example.com/repo.git", "main", env={"K": "V"})

        assert result == "d" * 40
        mock_resolve.assert_called_once_with("https://example.com/repo.git", "main", env={"K": "V"})


class TestLsRemoteResolve:
    def test_returns_head_sha_when_ref_is_none(self, cache: GitCache) -> None:
        sha = "1" * 40
        with (
            patch("subprocess.run", return_value=_proc(stdout=f"{sha}\tHEAD\n")),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={"DEFAULT": "1"}),
        ):
            result = cache._ls_remote_resolve("https://example.com/repo.git", None)

        assert result == sha

    def test_uses_explicit_env_when_provided(self, cache: GitCache) -> None:
        sha = "2" * 40
        explicit_env = {"TOKEN": "abc"}
        with (
            patch(
                "subprocess.run", return_value=_proc(stdout=f"{sha}\trefs/heads/main\n")
            ) as mock_run,
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={"DEFAULT": "1"}),
        ):
            result = cache._ls_remote_resolve(
                "https://example.com/repo.git", "main", env=explicit_env
            )

        assert result == sha
        assert mock_run.call_args.kwargs["env"] is explicit_env

    def test_uses_default_git_env_when_env_not_provided(self, cache: GitCache) -> None:
        sha = "3" * 40
        default_env = {"DEFAULT": "1"}
        with (
            patch(
                "subprocess.run", return_value=_proc(stdout=f"{sha}\trefs/heads/main\n")
            ) as mock_run,
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value=default_env),
        ):
            result = cache._ls_remote_resolve("https://example.com/repo.git", "main")

        assert result == sha
        assert mock_run.call_args.kwargs["env"] is default_env

    def test_appends_ref_to_ls_remote_command(self, cache: GitCache) -> None:
        sha = "4" * 40
        with (
            patch(
                "subprocess.run", return_value=_proc(stdout=f"{sha}\trefs/heads/main\n")
            ) as mock_run,
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
        ):
            cache._ls_remote_resolve("https://example.com/repo.git", "main")

        assert mock_run.call_args.args[0] == [
            "git",
            "ls-remote",
            "https://example.com/repo.git",
            "main",
        ]

    def test_matches_exact_remote_ref_name(self, cache: GitCache) -> None:
        sha = "5" * 40
        with (
            patch("subprocess.run", return_value=_proc(stdout=f"{sha}\tmain\n")),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
        ):
            assert cache._ls_remote_resolve("https://example.com/repo.git", "main") == sha

    def test_matches_refs_heads_prefix(self, cache: GitCache) -> None:
        sha = "6" * 40
        with (
            patch("subprocess.run", return_value=_proc(stdout=f"{sha}\trefs/heads/main\n")),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
        ):
            assert cache._ls_remote_resolve("https://example.com/repo.git", "main") == sha

    def test_matches_refs_tags_prefix(self, cache: GitCache) -> None:
        sha = "7" * 40
        with (
            patch("subprocess.run", return_value=_proc(stdout=f"{sha}\trefs/tags/v1.0.0\n")),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
        ):
            assert cache._ls_remote_resolve("https://example.com/repo.git", "v1.0.0") == sha

    def test_falls_back_to_first_sha_when_no_exact_match(self, cache: GitCache) -> None:
        first = "8" * 40
        second = "9" * 40
        stdout = f"{first}\trefs/heads/other\n{second}\trefs/tags/v1.0.0\n"
        with (
            patch("subprocess.run", return_value=_proc(stdout=stdout)),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
        ):
            assert cache._ls_remote_resolve("https://example.com/repo.git", "missing") == first

    def test_ignores_non_sha_lines_before_finding_valid_sha(self, cache: GitCache) -> None:
        sha = "a" * 40
        stdout = f"not-a-sha\trefs/heads/main\n{sha}\trefs/heads/other\n"
        with (
            patch("subprocess.run", return_value=_proc(stdout=stdout)),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
        ):
            assert cache._ls_remote_resolve("https://example.com/repo.git", "missing") == sha

    def test_non_zero_exit_raises_runtime_error(self, cache: GitCache) -> None:
        with (
            patch("subprocess.run", return_value=_proc(returncode=128, stderr="fatal: denied")),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
            pytest.raises(RuntimeError, match="git ls-remote failed"),
        ):
            cache._ls_remote_resolve("https://example.com/repo.git", "main")

    def test_timeout_raises_runtime_error(self, cache: GitCache) -> None:
        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30)),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
            pytest.raises(RuntimeError, match="Failed to resolve ref 'main'"),
        ):
            cache._ls_remote_resolve("https://example.com/repo.git", "main")

    def test_oserror_raises_runtime_error(self, cache: GitCache) -> None:
        with (
            patch("subprocess.run", side_effect=OSError("git missing")),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
            pytest.raises(RuntimeError, match="Failed to resolve ref 'main'"),
        ):
            cache._ls_remote_resolve("https://example.com/repo.git", "main")

    def test_no_sha_in_output_raises_runtime_error(self, cache: GitCache) -> None:
        with (
            patch("subprocess.run", return_value=_proc(stdout="nonsense\n")),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
            pytest.raises(RuntimeError, match="Could not resolve ref 'main'"),
        ):
            cache._ls_remote_resolve("https://example.com/repo.git", "main")


class TestEnsureBareRepo:
    def test_existing_bare_repo_with_sha_is_reused(self, cache: GitCache) -> None:
        shard_key = cache_shard_key("https://example.com/repo.git")
        bare_dir = cache._db_root / shard_key
        bare_dir.mkdir(parents=True)

        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=nullcontext()),
            patch.object(cache, "_bare_has_sha", return_value=True) as mock_has_sha,
            patch.object(cache, "_fetch_into_bare_locked") as mock_fetch,
        ):
            result = cache._ensure_bare_repo("https://example.com/repo.git", shard_key, "a" * 40)

        assert result == bare_dir
        mock_has_sha.assert_called_once_with(bare_dir, "a" * 40, env=None)
        mock_fetch.assert_not_called()

    def test_existing_bare_repo_without_sha_fetches(self, cache: GitCache) -> None:
        shard_key = cache_shard_key("https://example.com/repo.git")
        bare_dir = cache._db_root / shard_key
        bare_dir.mkdir(parents=True)

        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=nullcontext()),
            patch.object(cache, "_bare_has_sha", return_value=False),
            patch.object(cache, "_fetch_into_bare_locked") as mock_fetch,
        ):
            result = cache._ensure_bare_repo("https://example.com/repo.git", shard_key, "b" * 40)

        assert result == bare_dir
        mock_fetch.assert_called_once_with(
            bare_dir, "https://example.com/repo.git", "b" * 40, env=None
        )

    def test_cold_miss_clones_and_lands_atomically(self, cache: GitCache) -> None:
        shard_key = cache_shard_key("https://example.com/repo.git")
        bare_dir = cache._db_root / shard_key

        def _land(staged: Path, final: Path, _lock: object) -> bool:
            os.replace(staged, final)
            return True

        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=nullcontext()),
            patch("subprocess.run", return_value=_proc()) as mock_run,
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
            patch("apm_cli.cache.git_cache.atomic_land", side_effect=_land) as mock_land,
            patch("apm_cli.cache.git_cache.os.chmod"),
        ):
            result = cache._ensure_bare_repo("https://example.com/repo.git", shard_key, "c" * 40)

        assert result == bare_dir
        assert bare_dir.is_dir()
        mock_run.assert_called_once()
        mock_land.assert_called_once()

    def test_clone_failure_cleans_staged_directory(self, cache: GitCache) -> None:
        shard_key = cache_shard_key("https://example.com/repo.git")

        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=nullcontext()),
            patch(
                "subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "git", stderr="boom"),
            ),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
            patch("apm_cli.cache.git_cache.os.chmod"),
            patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rmtree,
            pytest.raises(RuntimeError, match="Failed to clone"),
        ):
            cache._ensure_bare_repo("https://example.com/repo.git", shard_key, "d" * 40)

        mock_rmtree.assert_called_once()

    def test_atomic_land_false_fetches_when_winner_lacks_sha(self, cache: GitCache) -> None:
        shard_key = cache_shard_key("https://example.com/repo.git")
        bare_dir = cache._db_root / shard_key
        bare_dir.mkdir(parents=True)

        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=nullcontext()),
            patch("subprocess.run", return_value=_proc()),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
            patch("apm_cli.cache.git_cache.atomic_land", return_value=False),
            patch.object(cache, "_bare_has_sha", return_value=False) as mock_has_sha,
            patch.object(cache, "_fetch_into_bare_locked") as mock_fetch,
            patch("apm_cli.cache.git_cache.os.chmod"),
        ):
            result = cache._ensure_bare_repo("https://example.com/repo.git", shard_key, "e" * 40)

        assert result == bare_dir
        mock_has_sha.assert_called_once_with(bare_dir, "e" * 40, env=None)
        mock_fetch.assert_called_once_with(
            bare_dir, "https://example.com/repo.git", "e" * 40, env=None
        )

    def test_atomic_land_false_skips_fetch_when_winner_has_sha(self, cache: GitCache) -> None:
        shard_key = cache_shard_key("https://example.com/repo.git")
        bare_dir = cache._db_root / shard_key
        bare_dir.mkdir(parents=True)

        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=nullcontext()),
            patch("subprocess.run", return_value=_proc()),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
            patch("apm_cli.cache.git_cache.atomic_land", return_value=False),
            patch.object(cache, "_bare_has_sha", return_value=True),
            patch.object(cache, "_fetch_into_bare_locked") as mock_fetch,
            patch("apm_cli.cache.git_cache.os.chmod"),
        ):
            result = cache._ensure_bare_repo("https://example.com/repo.git", shard_key, "f" * 40)

        assert result == bare_dir
        mock_fetch.assert_not_called()


class TestCreateCheckout:
    def test_write_dedup_hit_under_lock_returns_existing_checkout(self, cache: GitCache) -> None:
        url = "https://example.com/repo.git"
        shard_key = cache_shard_key(url)
        final_dir = cache._checkouts_root / shard_key / ("a" * 40)
        final_dir.mkdir(parents=True)

        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=nullcontext()),
            patch("apm_cli.cache.git_cache.verify_checkout_sha", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            result = cache._create_checkout(url, shard_key, "a" * 40)

        assert result == final_dir
        mock_run.assert_not_called()

    def test_invalid_existing_checkout_recreates_from_bare_repo(self, cache: GitCache) -> None:
        url = "https://example.com/repo.git"
        shard_key = cache_shard_key(url)
        bare_dir = cache._db_root / shard_key
        bare_dir.mkdir(parents=True)
        final_dir = cache._checkouts_root / shard_key / ("b" * 40)
        final_dir.mkdir(parents=True)

        def _land(staged: Path, final: Path, _lock: object) -> bool:
            if final.exists():
                from apm_cli.utils.file_ops import robust_rmtree

                robust_rmtree(final, ignore_errors=True)
            os.replace(staged, final)
            return True

        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=nullcontext()),
            patch("apm_cli.cache.git_cache.verify_checkout_sha", return_value=False),
            patch("subprocess.run", return_value=_proc()) as mock_run,
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
            patch("apm_cli.cache.git_cache.atomic_land", side_effect=_land),
            patch("apm_cli.cache.git_cache.os.chmod"),
        ):
            result = cache._create_checkout(url, shard_key, "b" * 40)

        assert result == final_dir
        assert mock_run.call_count == 2

    def test_clone_failure_cleans_staged_checkout(self, cache: GitCache) -> None:
        url = "https://example.com/repo.git"
        shard_key = cache_shard_key(url)
        (cache._db_root / shard_key).mkdir(parents=True)

        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=nullcontext()),
            patch(
                "subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "git", stderr="clone failed"),
            ),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
            patch("apm_cli.cache.git_cache.os.chmod"),
            patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rmtree,
            pytest.raises(RuntimeError, match="Failed to create checkout"),
        ):
            cache._create_checkout(url, shard_key, "c" * 40)

        mock_rmtree.assert_called_once()

    def test_checkout_failure_cleans_staged_checkout(self, cache: GitCache) -> None:
        url = "https://example.com/repo.git"
        shard_key = cache_shard_key(url)
        (cache._db_root / shard_key).mkdir(parents=True)

        clone_result = _proc()
        checkout_error = subprocess.CalledProcessError(1, "git", stderr="checkout failed")
        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=nullcontext()),
            patch("subprocess.run", side_effect=[clone_result, checkout_error]),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
            patch("apm_cli.cache.git_cache.os.chmod"),
            patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rmtree,
            pytest.raises(RuntimeError, match="Failed to create checkout"),
        ):
            cache._create_checkout(url, shard_key, "d" * 40)

        mock_rmtree.assert_called_once()

    def test_atomic_land_false_accepts_valid_winner(self, cache: GitCache) -> None:
        url = "https://example.com/repo.git"
        shard_key = cache_shard_key(url)
        final_dir = cache._checkouts_root / shard_key / ("e" * 40)
        final_dir.mkdir(parents=True)
        (cache._db_root / shard_key).mkdir(parents=True)

        verify_results = [False, True]
        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=nullcontext()),
            patch("apm_cli.cache.git_cache.verify_checkout_sha", side_effect=verify_results),
            patch("subprocess.run", return_value=_proc()) as mock_run,
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
            patch("apm_cli.cache.git_cache.atomic_land", return_value=False),
            patch("apm_cli.cache.git_cache.os.chmod"),
        ):
            result = cache._create_checkout(url, shard_key, "e" * 40)

        assert result == final_dir
        assert mock_run.call_count == 2

    def test_atomic_land_false_with_invalid_winner_evicts_and_raises(self, cache: GitCache) -> None:
        url = "https://example.com/repo.git"
        shard_key = cache_shard_key(url)
        final_dir = cache._checkouts_root / shard_key / ("f" * 40)
        final_dir.mkdir(parents=True)
        (cache._db_root / shard_key).mkdir(parents=True)

        verify_results = [False, False]
        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=nullcontext()),
            patch("apm_cli.cache.git_cache.verify_checkout_sha", side_effect=verify_results),
            patch("subprocess.run", return_value=_proc()),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
            patch("apm_cli.cache.git_cache.atomic_land", return_value=False),
            patch.object(cache, "_evict_checkout") as mock_evict,
            patch("apm_cli.cache.git_cache.os.chmod"),
            pytest.raises(RuntimeError, match="Race condition"),
        ):
            cache._create_checkout(url, shard_key, "f" * 40)

        mock_evict.assert_called_once_with(final_dir)


class TestBareHasSha:
    def test_returns_true_when_commit_exists(self, cache: GitCache, tmp_path: Path) -> None:
        bare_dir = tmp_path / "bare.git"
        bare_dir.mkdir()
        with (
            patch("subprocess.run", return_value=_proc(stdout="commit\n")),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
        ):
            assert cache._bare_has_sha(bare_dir, "a" * 40) is True

    def test_returns_false_when_git_reports_non_commit(
        self, cache: GitCache, tmp_path: Path
    ) -> None:
        bare_dir = tmp_path / "bare.git"
        bare_dir.mkdir()
        with (
            patch("subprocess.run", return_value=_proc(stdout="blob\n")),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
        ):
            assert cache._bare_has_sha(bare_dir, "b" * 40) is False

    def test_returns_false_on_timeout(self, cache: GitCache, tmp_path: Path) -> None:
        bare_dir = tmp_path / "bare.git"
        bare_dir.mkdir()
        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10)),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
        ):
            assert cache._bare_has_sha(bare_dir, "c" * 40) is False

    def test_returns_false_on_oserror(self, cache: GitCache, tmp_path: Path) -> None:
        bare_dir = tmp_path / "bare.git"
        bare_dir.mkdir()
        with (
            patch("subprocess.run", side_effect=OSError("boom")),
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
        ):
            assert cache._bare_has_sha(bare_dir, "d" * 40) is False


class TestFetchIntoBare:
    def test_skips_locked_fetch_when_sha_exists(self, cache: GitCache, tmp_path: Path) -> None:
        bare_dir = tmp_path / "bare.git"
        bare_dir.mkdir()

        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=nullcontext()),
            patch.object(cache, "_bare_has_sha", return_value=True),
            patch.object(cache, "_fetch_into_bare_locked") as mock_fetch,
        ):
            cache._fetch_into_bare(bare_dir, "https://example.com/repo.git", "a" * 40)

        mock_fetch.assert_not_called()

    def test_fetches_locked_when_sha_missing(self, cache: GitCache, tmp_path: Path) -> None:
        bare_dir = tmp_path / "bare.git"
        bare_dir.mkdir()

        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=nullcontext()),
            patch.object(cache, "_bare_has_sha", return_value=False),
            patch.object(cache, "_fetch_into_bare_locked") as mock_fetch,
        ):
            cache._fetch_into_bare(
                bare_dir, "https://example.com/repo.git", "b" * 40, env={"E": "1"}
            )

        mock_fetch.assert_called_once_with(
            bare_dir, "https://example.com/repo.git", "b" * 40, env={"E": "1"}
        )


class TestFetchIntoBareLocked:
    def test_fetches_specific_sha_successfully(self, cache: GitCache, tmp_path: Path) -> None:
        bare_dir = tmp_path / "bare.git"
        bare_dir.mkdir()

        with (
            patch("subprocess.run", return_value=_proc()) as mock_run,
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
        ):
            cache._fetch_into_bare_locked(bare_dir, "https://example.com/repo.git", "a" * 40)

        assert mock_run.call_args_list[0].args[0] == [
            "git",
            "-C",
            str(bare_dir),
            "fetch",
            "https://example.com/repo.git",
            "a" * 40,
        ]

    def test_falls_back_to_fetch_all_when_fetch_by_sha_fails(
        self, cache: GitCache, tmp_path: Path
    ) -> None:
        bare_dir = tmp_path / "bare.git"
        bare_dir.mkdir()

        with (
            patch(
                "subprocess.run",
                side_effect=[subprocess.CalledProcessError(1, "git"), _proc()],
            ) as mock_run,
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
        ):
            cache._fetch_into_bare_locked(bare_dir, "https://example.com/repo.git", "b" * 40)

        assert mock_run.call_args_list[1].args[0] == ["git", "-C", str(bare_dir), "fetch", "--all"]


class TestEvictCheckout:
    def test_evict_checkout_uses_robust_rmtree(self, cache: GitCache, tmp_path: Path) -> None:
        checkout_dir = tmp_path / "checkout"
        checkout_dir.mkdir()

        with patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rmtree:
            cache._evict_checkout(checkout_dir)

        mock_rmtree.assert_called_once_with(checkout_dir, ignore_errors=True)

    def test_evict_checkout_swallows_errors(self, cache: GitCache, tmp_path: Path) -> None:
        checkout_dir = tmp_path / "checkout"
        checkout_dir.mkdir()

        with patch("apm_cli.utils.file_ops.robust_rmtree", side_effect=OSError("denied")):
            cache._evict_checkout(checkout_dir)


class TestStatsCleanAndPrune:
    def test_get_cache_stats_counts_entries_and_sizes(self, cache: GitCache) -> None:
        db_dir = cache._db_root / "db-one"
        db_dir.mkdir()
        (db_dir / "objects.pack").write_bytes(b"1234")
        (cache._db_root / "skip.lock").mkdir()

        checkout_dir = cache._checkouts_root / "shard" / "sha"
        checkout_dir.mkdir(parents=True)
        (checkout_dir / "file.txt").write_bytes(b"abc")

        stats = cache.get_cache_stats()

        assert stats == {
            "db_count": 1,
            "checkout_count": 1,
            "total_size_bytes": 7,
        }

    def test_clean_all_removes_dirs_and_files(self, cache: GitCache) -> None:
        db_dir = cache._db_root / "db-one"
        db_dir.mkdir()
        extra_file = cache._db_root / "leftover.txt"
        extra_file.write_text("x", encoding="utf-8")
        checkout_dir = cache._checkouts_root / "shard" / "sha"
        checkout_dir.mkdir(parents=True)

        with patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rmtree:
            cache.clean_all()

        removed_paths = [call.args[0] for call in mock_rmtree.call_args_list]
        assert db_dir in removed_paths
        assert checkout_dir.parent in removed_paths or checkout_dir in removed_paths
        assert not extra_file.exists()

    def test_prune_returns_zero_when_checkout_root_missing(self, cache: GitCache) -> None:
        from apm_cli.utils.file_ops import robust_rmtree

        robust_rmtree(cache._checkouts_root, ignore_errors=True)
        assert cache.prune(max_age_days=1) == 0

    def test_prune_removes_only_old_checkout_directories(self, cache: GitCache) -> None:
        old_dir = cache._checkouts_root / "shard" / "old"
        new_dir = cache._checkouts_root / "shard" / "new"
        old_dir.mkdir(parents=True)
        new_dir.mkdir(parents=True)
        old_time = 1
        os.utime(old_dir, (old_time, old_time))

        with patch("time.time", return_value=100 * 86400):
            pruned = cache.prune(max_age_days=30)

        assert pruned == 1
        assert not old_dir.exists()
        assert new_dir.exists()

    def test_prune_ignores_non_directory_entries(self, cache: GitCache) -> None:
        shard_dir = cache._checkouts_root / "shard"
        shard_dir.mkdir(parents=True)
        (shard_dir / "note.txt").write_text("x", encoding="utf-8")

        with patch("time.time", return_value=100 * 86400):
            assert cache.prune(max_age_days=30) == 0

    def test_prune_ignores_stat_errors(
        self, cache: GitCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old_dir = cache._checkouts_root / "shard" / "broken"
        old_dir.mkdir(parents=True)
        original_scandir = os.scandir

        class _BrokenStat:
            def __init__(self, entry: os.DirEntry[str]) -> None:
                self._entry = entry
                self.path = entry.path

            def is_dir(self, *, follow_symlinks: bool = False) -> bool:
                return self._entry.is_dir(follow_symlinks=follow_symlinks)

            def stat(self, *, follow_symlinks: bool = False):
                raise OSError("stat failed")

        def _fake_scandir(path: str):
            entries = list(original_scandir(path))
            if path == str(cache._checkouts_root / "shard"):
                return iter([_BrokenStat(entries[0])])
            return iter(entries)

        monkeypatch.setattr(os, "scandir", _fake_scandir)
        assert cache.prune(max_age_days=30) == 0


class TestHelperFunctions:
    def test_dir_size_sums_all_files(self, tmp_path: Path) -> None:
        root = tmp_path / "payload"
        root.mkdir()
        (root / "one.txt").write_bytes(b"abcd")
        nested = root / "nested"
        nested.mkdir()
        (nested / "two.bin").write_bytes(b"xyz")

        assert _dir_size(root) == 7

    def test_dir_size_ignores_lstat_errors(self, tmp_path: Path) -> None:
        root = tmp_path / "payload"
        root.mkdir()
        file_path = root / "one.txt"
        file_path.write_bytes(b"abcd")
        original_lstat = os.lstat

        def _fake_lstat(path: str):
            if path == str(file_path):
                raise OSError("boom")
            return original_lstat(path)

        with patch("os.lstat", side_effect=_fake_lstat):
            assert _dir_size(root) == 0

    def test_dir_size_ignores_walk_errors(self, tmp_path: Path) -> None:
        root = tmp_path / "payload"
        root.mkdir()

        def _raise(_path: str):
            raise OSError("walk failed")
            yield from ()

        with patch("os.walk", side_effect=_raise):
            assert _dir_size(root) == 0

    def test_sanitize_url_strips_password_and_preserves_port(self) -> None:
        sanitized = _sanitize_url("https://alice:secret@example.com:8443/repo.git")
        assert sanitized == "https://alice:***@example.com:8443/repo.git"

    def test_sanitize_url_leaves_username_only_url_unchanged(self) -> None:
        url = "https://alice@example.com/repo.git"
        assert _sanitize_url(url) == url

    def test_sanitize_url_returns_original_on_parser_error(self) -> None:
        with patch("urllib.parse.urlparse", side_effect=ValueError("bad url")):
            assert _sanitize_url("https://example.com/repo.git") == "https://example.com/repo.git"
