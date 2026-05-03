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


class TestGitCacheBlobsPresent:
    """Regression: cache must contain file blobs, not just trees.

    A previous iteration used ``--filter=blob:none`` for the bare clone,
    which left the checkout working tree empty after ``git clone --local
    --shared`` + ``git checkout``.  Subdirectory extraction then found
    empty directories and validation failed with "no SKILL.md found".
    """

    def test_bare_clone_does_not_use_blob_filter(self, tmp_path: Path) -> None:
        """The bare clone command must not strip blobs.

        Inspect the actual command issued to git clone --bare and assert
        no ``--filter`` argument is present.  Catching this at the
        command-construction layer avoids a slow real-network test while
        still preventing regression of the empty-checkout bug.
        """
        from unittest.mock import MagicMock as MM
        from unittest.mock import patch as p

        cache = GitCache(tmp_path)
        url = "https://github.com/owner/repo"
        sha = "a" * 40

        captured: list[list[str]] = []

        def _fake_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            captured.append(list(cmd))
            return MM(returncode=0, stdout="", stderr="")

        from contextlib import suppress

        with p("subprocess.run", side_effect=_fake_run):
            with suppress(RuntimeError):
                cache._ensure_bare_repo(url, "shard1", sha)

        clone_cmds = [c for c in captured if "clone" in c and "--bare" in c]
        assert clone_cmds, "Expected at least one bare clone command"
        for cmd in clone_cmds:
            assert not any(arg.startswith("--filter") for arg in cmd), (
                f"Bare clone must not use --filter (would strip blobs and "
                f"break checkout extraction). Got: {cmd}"
            )


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


class TestGitCacheEnvForwarding:
    """Verify the env dict reaches every git subprocess invocation.

    Regression-trap for a class of bugs where the cache layer drops
    the auth-aware env on the floor and silently falls back to an
    unauthenticated default (which would defeat private-repo access
    AND cause silent cache misses on Windows / NixOS where ``git`` is
    not on the bare PATH that ``subprocess`` sees).
    """

    @patch("subprocess.run")
    def test_env_forwarded_to_ls_remote(self, mock_run: MagicMock, tmp_path: Path) -> None:
        cache = GitCache(tmp_path)
        sentinel = {"APM_TEST_TOKEN": "sentinel-value", "PATH": "/usr/bin:/bin"}
        sha = "d" * 40
        mock_run.return_value = MagicMock(returncode=0, stdout=f"{sha}\trefs/heads/main\n")
        cache._resolve_sha("https://github.com/owner/repo", "main", env=sentinel)
        # Assert env was passed through verbatim
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs.get("env") is sentinel

    @patch("subprocess.run")
    def test_env_forwarded_to_get_checkout_miss(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Cache miss path: bare clone + checkout must both receive env."""
        cache = GitCache(tmp_path)
        sha = "e" * 40
        sentinel = {"APM_TEST_TOKEN": "miss-path-value", "PATH": "/usr/bin:/bin"}

        # Stub subprocess.run so it ALWAYS succeeds; cache layer will
        # call clone, fetch, checkout in some order.
        def _run_stub(*args, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _run_stub

        # Lay down a bare-repo marker so _ensure_bare_repo skips clone
        # (we want to focus this test on the checkout path's env-forward)
        from apm_cli.cache.url_normalize import cache_shard_key

        shard = cache_shard_key("https://github.com/owner/repo")
        bare_dir = tmp_path / "git" / "db_v1" / shard
        bare_dir.mkdir(parents=True)
        (bare_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        import contextlib

        # We don't care if the checkout fails to materialise on
        # disk -- this test only verifies env propagation.
        with contextlib.suppress(Exception):
            cache.get_checkout(
                "https://github.com/owner/repo", "main", locked_sha=sha, env=sentinel
            )

        # Every subprocess call should carry the sentinel env
        assert mock_run.called
        for call in mock_run.call_args_list:
            assert call.kwargs.get("env") is sentinel, (
                f"env not forwarded to: {call.args[0] if call.args else call.kwargs.get('args')}"
            )


class TestCheckoutWriteDedup:
    """_create_checkout must short-circuit when a concurrent process
    populated the shard while we were waiting on the shard lock.

    This is the cross-process write-deduplication pattern: the lock
    winner clones; lock losers see a populated shard at re-probe time
    and return immediately without doing any clone work themselves.
    """

    def test_short_circuits_when_final_exists_under_lock(self, tmp_path: Path) -> None:
        """If final_dir is already populated when the lock is acquired,
        no git subprocess is invoked."""
        from apm_cli.cache.url_normalize import cache_shard_key

        cache = GitCache(tmp_path)
        url = "https://github.com/owner/repo"
        sha = "1" * 40
        shard = cache_shard_key(url)

        # Simulate "another process already landed this shard": create
        # the final_dir BEFORE _create_checkout runs.
        final_dir = tmp_path / "git" / "checkouts_v1" / shard / sha
        final_dir.mkdir(parents=True)
        (final_dir / ".git").mkdir()

        with (
            patch("subprocess.run") as mock_run,
            patch(
                "apm_cli.cache.git_cache.verify_checkout_sha",
                return_value=True,
            ) as mock_verify,
        ):
            result = cache._create_checkout(url, shard, sha)
            mock_run.assert_not_called()
            mock_verify.assert_called_with(final_dir, sha)
        assert result == final_dir

    def test_proceeds_with_clone_when_final_missing(self, tmp_path: Path) -> None:
        """If final_dir does not exist on lock entry, clone happens."""
        from apm_cli.cache.url_normalize import cache_shard_key

        cache = GitCache(tmp_path)
        url = "https://github.com/owner/repo"
        sha = "2" * 40
        shard = cache_shard_key(url)

        # Pre-create the bare repo dir so _create_checkout can target it
        (tmp_path / "git" / "db_v1" / shard).mkdir(parents=True)

        def _populate(*args, **kwargs):
            # On the `git clone --local --shared` invocation, materialise
            # the staged dir with a minimal .git so the rename succeeds.
            cmd = args[0] if args else kwargs.get("args", [])
            if "clone" in cmd and "--local" in cmd:
                staged = Path(cmd[-1])
                staged.mkdir(parents=True, exist_ok=True)
                (staged / ".git").mkdir(exist_ok=True)
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("subprocess.run", side_effect=_populate) as mock_run,
            patch(
                "apm_cli.cache.git_cache.verify_checkout_sha",
                return_value=True,
            ),
        ):
            result = cache._create_checkout(url, shard, sha)
            # Two git invocations: clone + checkout.
            assert mock_run.call_count >= 2
        assert result.is_dir()

    def test_short_circuits_on_integrity_pass_only(self, tmp_path: Path) -> None:
        """A populated final_dir with FAILING integrity is not a hit:
        we must proceed to re-clone rather than serve a corrupt shard."""
        from apm_cli.cache.url_normalize import cache_shard_key

        cache = GitCache(tmp_path)
        url = "https://github.com/owner/repo"
        sha = "3" * 40
        shard = cache_shard_key(url)

        # Populate final_dir BUT integrity will report failure.
        final_dir = tmp_path / "git" / "checkouts_v1" / shard / sha
        final_dir.mkdir(parents=True)
        (tmp_path / "git" / "db_v1" / shard).mkdir(parents=True)

        def _populate(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if "clone" in cmd and "--local" in cmd:
                staged = Path(cmd[-1])
                staged.mkdir(parents=True, exist_ok=True)
                (staged / ".git").mkdir(exist_ok=True)
            return MagicMock(returncode=0, stdout="", stderr="")

        # First verify call (re-probe under lock) returns False; subsequent
        # calls (after atomic_land) return True so we don't blow up on
        # the post-rename verification.
        verify_calls = [False, True, True]

        def _verify(*_args, **_kwargs):
            return verify_calls.pop(0) if verify_calls else True

        with (
            patch("subprocess.run", side_effect=_populate) as mock_run,
            patch(
                "apm_cli.cache.git_cache.verify_checkout_sha",
                side_effect=_verify,
            ),
        ):
            cache._create_checkout(url, shard, sha)
            # We did NOT short-circuit -- clone happened.
            assert mock_run.called
