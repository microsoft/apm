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

from apm_cli.cache.git_cache import (
    CachePruneError,
    GitCache,
    _safe_git_args,
    _sanitize_url,
    dir_size,
)
from apm_cli.cache.url_normalize import cache_shard_key
from apm_cli.utils.git_env import _GitConfigSnapshot
from tests.utils.cached_repo_trust import trust_cached_repos
from tests.utils.checkout_trust import trust_checkout_contents
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

_REAL_RUN = subprocess.run


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every git child, setup included, under a bounded environment.

    The runner's GIT_DIR, global config (signing, transport policy) and
    credential state never reach the fixtures, probes or cache calls.
    """
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    for name in list(os.environ):
        monkeypatch.delenv(name)
    for name, value in isolated.process_environment.items():
        monkeypatch.setenv(name, value)


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
        patch("os.chmod"),
    ):
        return GitCache(tmp_path)


class TestGitCacheInit:
    def test_init_creates_directories_and_calls_cleanup(self, tmp_path: Path) -> None:
        with (
            patch("apm_cli.cache.git_cache.cleanup_incomplete") as mock_cleanup,
            patch("os.chmod") as mock_chmod,
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
            patch("os.chmod"),
        ):
            cache = GitCache(tmp_path)

        assert cache._db_root == db_root
        assert cache._checkouts_root == checkouts_root


@trust_cached_repos()
@trust_checkout_contents()
class TestGetCheckout:
    @pytest.fixture(autouse=True)
    def _reachable_pins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Scripted git children; the reachability check has its own cells.
        monkeypatch.setattr(
            "apm_cli.cache.commit_reachability.require_reachable_commit",
            lambda *_args, **_kwargs: None,
        )

    def test_cache_hit_returns_existing_checkout(self, cache: GitCache) -> None:
        url = "https://github.com/owner/repo.git"
        sha = "a" * 40
        checkout_dir = cache._checkouts_root / cache_shard_key(url) / sha / "full"
        checkout_dir.mkdir(parents=True)
        (checkout_dir / ".git").mkdir()
        (checkout_dir / ".git" / "config").write_text(
            "[core]\n\tautocrlf = false\n\teol = lf\n\tsymlinks = false\n"
            "[apm]\n\tlinkMaterialization = git-objects-checkout\n",
            encoding="ascii",
        )

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
        checkout_dir = cache._checkouts_root / cache_shard_key(url) / sha / "full"
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
        mock_ensure.assert_called_once_with(url, cache_shard_key(url), sha, env=None, partial=False)
        mock_create.assert_called_once_with(
            url, cache_shard_key(url), sha, env=None, sparse_paths=None, promisor_url=None
        )

    def test_refresh_ignores_existing_checkout(self, tmp_path: Path) -> None:
        with (
            patch("apm_cli.cache.git_cache.cleanup_incomplete"),
            patch("os.chmod"),
        ):
            cache = GitCache(tmp_path, refresh=True)
        url = "https://github.com/owner/repo.git"
        sha = "c" * 40
        checkout_dir = cache._checkouts_root / cache_shard_key(url) / sha / "full"
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
        checkout_dir = cache._checkouts_root / cache_shard_key(url) / sha / "full"

        with (
            patch.object(cache, "_resolve_sha", return_value=sha),
            patch.object(cache, "_ensure_bare_repo") as mock_ensure,
            patch.object(cache, "_create_checkout", return_value=checkout_dir) as mock_create,
        ):
            result = cache.get_checkout(url, None, locked_sha=sha, env={"A": "1"})

        assert result == checkout_dir
        mock_ensure.assert_called_once_with(
            url, cache_shard_key(url), sha, env={"A": "1"}, partial=False
        )
        mock_create.assert_called_once_with(
            url, cache_shard_key(url), sha, env={"A": "1"}, sparse_paths=None, promisor_url=None
        )


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


class _Remote:
    """A real local repository served to the cache over ``file://``.

    ``main`` and ``v1.0.0`` point at the first commit; ``feature/topic`` holds a
    second commit no ``main`` or tag ref names.
    """

    def __init__(self, root: Path) -> None:
        self.work = root / "work"
        self.work.mkdir()
        self._git("init", "-q", "-b", "main")
        (self.work / "README.md").write_text("first\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-qm", "first")
        self._git("tag", "v1.0.0")
        self.main = self._git("rev-parse", "HEAD")
        self._git("checkout", "-qb", "feature/topic")
        (self.work / "README.md").write_text("second\n", encoding="utf-8")
        self._git("commit", "-qam", "second")
        self.topic = self._git("rev-parse", "HEAD")
        self._git("checkout", "-q", "main")
        self.path = root / "remote.git"
        _REAL_RUN(["git", "clone", "-q", "--bare", str(self.work), str(self.path)], check=True)
        self.url = self.path.as_uri()

    def _git(self, *args: str) -> str:
        result = _REAL_RUN(
            ["git", "-c", "user.name=APM Test", "-c", "user.email=apm@example.com", *args],
            cwd=self.work,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()


def _shard_dirs(root: Path) -> list[str]:
    """Directories under a cache root: landed shards and any staged ``.inc.`` leftovers."""
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir())


def _git_dir_has(bare_dir: Path, sha: str) -> bool:
    probe = ["git", "--git-dir", str(bare_dir), "cat-file", "-e", f"{sha}^{{commit}}"]
    return _REAL_RUN(probe, capture_output=True).returncode == 0


@pytest.fixture()
def remote(tmp_path: Path) -> _Remote:
    return _Remote(tmp_path)


class _GitChild:
    """Stand-in for git child processes at ``subprocess.run``.

    Records every child's argv and env. Each scripted reply is a
    ``CompletedProcess`` or an exception; ``None`` runs the real git.
    """

    def __init__(self, *replies: object) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), dict(kwargs.get("env") or {})))
        reply = self._replies.pop(0) if self._replies else None
        if reply is None:
            return _REAL_RUN(args, **kwargs)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def subcommands(self) -> list[str]:
        verbs = {"ls-remote", "clone", "fetch", "cat-file"}
        return [next(arg for arg in argv if arg in verbs) for argv, _env in self.calls]


def _done(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git"], returncode, stdout, stderr)


class TestLsRemoteResolve:
    """``_ls_remote_resolve`` against a real remote; parser edge cases via the child."""

    def test_returns_head_sha_when_ref_is_none(self, cache: GitCache, remote: _Remote) -> None:
        assert cache._ls_remote_resolve(remote.url, None) == remote.main

    def test_matches_refs_heads_prefix(self, cache: GitCache, remote: _Remote) -> None:
        assert cache._ls_remote_resolve(remote.url, "feature/topic") == remote.topic

    def test_matches_refs_tags_prefix(self, cache: GitCache, remote: _Remote) -> None:
        assert cache._ls_remote_resolve(remote.url, "v1.0.0") == remote.main

    def test_a_tail_match_never_resolves(self, cache: GitCache, remote: _Remote) -> None:
        # ``topic`` matches refs/heads/feature/topic by tail only, never exactly.
        with pytest.raises(RuntimeError, match="not a branch or tag"):
            cache._ls_remote_resolve(remote.url, "topic")

    def test_unknown_ref_raises_runtime_error(self, cache: GitCache, remote: _Remote) -> None:
        with pytest.raises(RuntimeError, match="Could not resolve ref 'missing'"):
            cache._ls_remote_resolve(remote.url, "missing")

    def test_non_zero_exit_raises_runtime_error(self, cache: GitCache, tmp_path: Path) -> None:
        gone = (tmp_path / "gone.git").as_uri()
        with pytest.raises(RuntimeError, match="git ls-remote failed"):
            cache._ls_remote_resolve(gone, "main")

    def test_appends_ref_to_ls_remote_command(
        self, cache: GitCache, remote: _Remote, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        child = _GitChild(None)
        monkeypatch.setattr("subprocess.run", child)

        assert cache._ls_remote_resolve(remote.url, "main") == remote.main
        (argv, _env), *_rest = child.calls
        # The peeled pattern lists an annotated tag's commit.
        assert argv[1:] == [*_safe_git_args(), "ls-remote", remote.url, "main", "main^{}"]

    def test_uses_explicit_env_when_provided(
        self, cache: GitCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sha = "2" * 40
        child = _GitChild(_done(stdout=f"{sha}\trefs/heads/main\n"))
        monkeypatch.setattr("subprocess.run", child)
        explicit_env = {**os.environ, "APM_MARKER": "explicit", "GIT_DIR": "/x"}

        result = cache._ls_remote_resolve("https://example.com/repo.git", "main", env=explicit_env)

        assert result == sha
        (_argv, env), *_rest = child.calls
        assert env["APM_MARKER"] == "explicit"
        assert "GIT_DIR" not in env, "the child env is sanitized, never passed raw"

    def test_uses_default_git_env_when_env_not_provided(
        self, cache: GitCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sha = "3" * 40
        child = _GitChild(_done(stdout=f"{sha}\trefs/heads/main\n"))
        monkeypatch.setattr("subprocess.run", child)
        monkeypatch.setenv("APM_MARKER", "ambient")

        assert cache._ls_remote_resolve("https://example.com/repo.git", "main") == sha
        (_argv, env), *_rest = child.calls
        assert env["APM_MARKER"] == "ambient"

    @pytest.mark.parametrize(
        ("stdout", "ref", "expected"),
        [
            pytest.param(
                f"{'5' * 40}\trefs/heads/main\n", "main", "5" * 40, id="exact-branch-name"
            ),
            pytest.param(
                f"not-a-sha\trefs/heads/main\n{'a' * 40}\trefs/heads/other\n",
                "other",
                "a" * 40,
                id="skips-non-sha-lines",
            ),
        ],
    )
    def test_parses_child_output(
        self,
        cache: GitCache,
        monkeypatch: pytest.MonkeyPatch,
        stdout: str,
        ref: str,
        expected: str,
    ) -> None:
        monkeypatch.setattr("subprocess.run", _GitChild(_done(stdout=stdout)))
        assert cache._ls_remote_resolve("https://example.com/repo.git", ref) == expected

    @pytest.mark.parametrize(
        ("reply", "message"),
        [
            pytest.param(
                subprocess.TimeoutExpired("git", 30), "Failed to resolve ref 'main'", id="timeout"
            ),
            pytest.param(OSError("git missing"), "Failed to resolve ref 'main'", id="oserror"),
            pytest.param(_done(stdout="nonsense\n"), "Could not resolve ref 'main'", id="no-sha"),
        ],
    )
    def test_child_failure_raises_runtime_error(
        self,
        cache: GitCache,
        monkeypatch: pytest.MonkeyPatch,
        reply: object,
        message: str,
    ) -> None:
        monkeypatch.setattr("subprocess.run", _GitChild(reply))
        with pytest.raises(RuntimeError, match=message):
            cache._ls_remote_resolve("https://example.com/repo.git", "main")


@trust_cached_repos()
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
        # The trust check also accepts the effective contact URL of this origin.
        mock_has_sha.assert_called_once_with(
            bare_dir, "a" * 40, env=None, url="https://example.com/repo.git"
        )
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

    def test_cold_miss_clones_and_lands_atomically(self, cache: GitCache, remote: _Remote) -> None:
        shard_key = cache_shard_key(remote.url)

        result = cache._ensure_bare_repo(remote.url, shard_key, remote.main)

        assert result == cache._db_root / shard_key
        assert _git_dir_has(result, remote.main)
        # Only the landed shard remains: no staged sibling is left behind.
        assert _shard_dirs(cache._db_root) == [shard_key]

    def test_clone_failure_cleans_staged_directory(self, cache: GitCache, tmp_path: Path) -> None:
        gone = (tmp_path / "gone.git").as_uri()

        with pytest.raises(RuntimeError, match="Failed to clone"):
            cache._ensure_bare_repo(gone, cache_shard_key(gone), "d" * 40)

        assert _shard_dirs(cache._db_root) == [], "the staged clone is removed"

    @staticmethod
    def _race(winner_argv: list[str]):
        """Let a competing process land *winner_argv*'s repo first; the real land then loses."""
        from apm_cli.cache.locking import atomic_land

        def land(staged: Path, final: Path, lock: object) -> bool:
            _REAL_RUN([*winner_argv, str(final)], check=True)
            return atomic_land(staged, final, lock)

        return patch("apm_cli.cache.git_cache.atomic_land", side_effect=land)

    def test_atomic_land_false_fetches_when_winner_lacks_sha(
        self, cache: GitCache, remote: _Remote
    ) -> None:
        shard_key = cache_shard_key(remote.url)
        winner = cache._db_root / shard_key

        with self._race(["git", "init", "-q", "--bare"]):
            result = cache._ensure_bare_repo(remote.url, shard_key, remote.main)

        assert result == winner
        assert _git_dir_has(winner, remote.main), "the empty winner must be fetched to the sha"
        assert _shard_dirs(cache._db_root) == [shard_key]

    def test_atomic_land_false_skips_fetch_when_winner_has_sha(
        self, cache: GitCache, remote: _Remote, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shard_key = cache_shard_key(remote.url)
        winner = cache._db_root / shard_key
        child = _GitChild()
        monkeypatch.setattr("subprocess.run", child)

        with self._race(["git", "clone", "-q", "--bare", str(remote.path)]):
            result = cache._ensure_bare_repo(remote.url, shard_key, remote.main)

        assert result == winner
        assert _git_dir_has(winner, remote.main)
        # The staged clone is probed for a tag-only commit before it lands.
        assert child.subcommands() == ["clone", "cat-file", "cat-file"], (
            "a winner holding the sha is not fetched"
        )


@trust_cached_repos()
class TestCreateCheckout:
    @pytest.fixture(autouse=True)
    def _validated_rewrites(self):
        """Keep subprocess mocks focused on checkout after policy validation.

        Link materialization has its own real-git tests; here it would feed
        the text-mode subprocess mocks to a bytes parser.
        """
        with (
            patch(
                "apm_cli.utils.git_env._validated_git_url_rewrite_policy",
                return_value=(None, _GitConfigSnapshot((), (), ())),
            ),
            patch("apm_cli.cache.git_cache.materialize_tracked_symlinks", return_value=[]),
        ):
            yield

    def test_write_dedup_hit_under_lock_returns_existing_checkout(self, cache: GitCache) -> None:
        url = "https://example.com/repo.git"
        shard_key = cache_shard_key(url)
        final_dir = cache._checkouts_root / shard_key / ("a" * 40) / "full"
        final_dir.mkdir(parents=True)
        (final_dir / ".git").mkdir()
        (final_dir / ".git" / "config").write_text(
            "[core]\n\tautocrlf = false\n\teol = lf\n\tsymlinks = false\n"
            "[apm]\n\tlinkMaterialization = git-objects-checkout\n",
            encoding="ascii",
        )

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
        final_dir = cache._checkouts_root / shard_key / ("b" * 40) / "full"
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
            patch("os.chmod"),
        ):
            result = cache._create_checkout(url, shard_key, "b" * 40)

        assert result == final_dir
        # clone (carries the persisted pins), checkout
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
            patch("os.chmod"),
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
            patch("os.chmod"),
            patch("apm_cli.utils.file_ops.robust_rmtree") as mock_rmtree,
            pytest.raises(RuntimeError, match="Failed to create checkout"),
        ):
            cache._create_checkout(url, shard_key, "d" * 40)

        mock_rmtree.assert_called_once()

    def test_atomic_land_false_accepts_valid_winner(self, cache: GitCache) -> None:
        url = "https://example.com/repo.git"
        shard_key = cache_shard_key(url)
        final_dir = cache._checkouts_root / shard_key / ("e" * 40) / "full"
        final_dir.mkdir(parents=True)
        (final_dir / ".git").mkdir()
        (final_dir / ".git" / "config").write_text(
            "[core]\n\tautocrlf = false\n\teol = lf\n\tsymlinks = false\n"
            "[apm]\n\tlinkMaterialization = git-objects-checkout\n",
            encoding="ascii",
        )
        (cache._db_root / shard_key).mkdir(parents=True)

        verify_results = [False, True]
        with (
            patch("apm_cli.cache.git_cache.shard_lock", return_value=nullcontext()),
            patch("apm_cli.cache.git_cache.verify_checkout_sha", side_effect=verify_results),
            patch("subprocess.run", return_value=_proc()) as mock_run,
            patch("apm_cli.utils.git_env.get_git_executable", return_value="git"),
            patch("apm_cli.utils.git_env.git_subprocess_env", return_value={}),
            patch("apm_cli.cache.git_cache.atomic_land", return_value=False),
            patch("os.chmod"),
        ):
            result = cache._create_checkout(url, shard_key, "e" * 40)

        assert result == final_dir
        # clone (carries the persisted pins), checkout
        assert mock_run.call_count == 2

    def test_atomic_land_false_with_invalid_winner_evicts_and_raises(self, cache: GitCache) -> None:
        url = "https://example.com/repo.git"
        shard_key = cache_shard_key(url)
        final_dir = cache._checkouts_root / shard_key / ("f" * 40) / "full"
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
            patch("os.chmod"),
            pytest.raises(RuntimeError, match="Race condition"),
        ):
            cache._create_checkout(url, shard_key, "f" * 40)

        mock_evict.assert_called_once_with(final_dir)


@trust_cached_repos()
class TestBareHasSha:
    def test_returns_true_when_commit_exists(self, cache: GitCache, remote: _Remote) -> None:
        assert cache._bare_has_sha(remote.path, remote.main) is True

    def test_returns_false_when_object_is_not_a_commit(
        self, cache: GitCache, remote: _Remote
    ) -> None:
        tree = _REAL_RUN(
            ["git", "--git-dir", str(remote.path), "rev-parse", f"{remote.main}^{{tree}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert cache._bare_has_sha(remote.path, tree) is False

    def test_returns_false_when_commit_is_absent(self, cache: GitCache, remote: _Remote) -> None:
        assert cache._bare_has_sha(remote.path, "c" * 40) is False

    @pytest.mark.parametrize(
        "failure",
        [subprocess.TimeoutExpired("git", 10), OSError("boom")],
        ids=["timeout", "oserror"],
    )
    def test_returns_false_when_the_child_fails(
        self,
        cache: GitCache,
        remote: _Remote,
        monkeypatch: pytest.MonkeyPatch,
        failure: BaseException,
    ) -> None:
        monkeypatch.setattr("subprocess.run", _GitChild(failure))
        assert cache._bare_has_sha(remote.path, remote.main) is False


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
    def test_fetches_specific_sha_successfully(
        self, cache: GitCache, remote: _Remote, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bare_dir = tmp_path / "bare.git"
        _REAL_RUN(["git", "init", "-q", "--bare", str(bare_dir)], check=True)
        child = _GitChild()
        monkeypatch.setattr("subprocess.run", child)

        cache._fetch_into_bare_locked(bare_dir, remote.url, remote.topic)

        assert _git_dir_has(bare_dir, remote.topic)
        (argv, _env), *_rest = child.calls
        assert argv[1:] == [
            *_safe_git_args(),
            "--git-dir",
            str(bare_dir),
            "fetch",
            remote.url,
            remote.topic,
        ]

    def test_falls_back_to_explicit_remote_refs_when_fetch_by_sha_fails(
        self, cache: GitCache, remote: _Remote, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bare_dir = tmp_path / "bare.git"
        _REAL_RUN(["git", "init", "-q", "--bare", str(bare_dir)], check=True)
        # The server refuses a fetch by sha; the ref fallback then runs for real.
        child = _GitChild(subprocess.CalledProcessError(128, "git"), None)
        monkeypatch.setattr("subprocess.run", child)

        cache._fetch_into_bare_locked(bare_dir, remote.url, remote.topic)

        assert _git_dir_has(bare_dir, remote.topic)
        assert child.calls[1][0][1:] == [
            *_safe_git_args(),
            "--git-dir",
            str(bare_dir),
            "fetch",
            remote.url,
            "+refs/heads/*:refs/remotes/apm-fallback/*",
            "+refs/tags/*:refs/tags/*",
        ]
        heads = _REAL_RUN(
            ["git", "--git-dir", str(bare_dir), "for-each-ref", "--format=%(refname)"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        assert "refs/remotes/apm-fallback/feature/topic" in heads
        assert "refs/tags/v1.0.0" in heads


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

    def test_prune_reports_stat_errors(
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
        with pytest.raises(CachePruneError, match=r"0 SHA group.*1 failed") as caught:
            cache.prune(max_age_days=30)
        assert caught.value.pruned == 0
        assert str(caught.value.failures[0][1]) == "stat failed"
        assert old_dir.is_dir()


class TestHelperFunctions:
    def test_dir_size_sums_all_files(self, tmp_path: Path) -> None:
        root = tmp_path / "payload"
        root.mkdir()
        (root / "one.txt").write_bytes(b"abcd")
        nested = root / "nested"
        nested.mkdir()
        (nested / "two.bin").write_bytes(b"xyz")

        assert dir_size(root) == 7

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
            assert dir_size(root) == 0

    def test_dir_size_ignores_walk_errors(self, tmp_path: Path) -> None:
        root = tmp_path / "payload"
        root.mkdir()

        def _raise(_path: str):
            raise OSError("walk failed")
            yield from ()

        with patch("os.walk", side_effect=_raise):
            assert dir_size(root) == 0

    def test_sanitize_url_redacts_userinfo_and_preserves_port(self) -> None:
        sanitized = _sanitize_url("https://alice:secret@example.com:8443/repo.git")
        assert sanitized == "https://***@example.com:8443/repo.git"

    def test_sanitize_url_redacts_username_only_url(self) -> None:
        url = "https://alice@example.com/repo.git"
        assert _sanitize_url(url) == "https://***@example.com/repo.git"

    def test_sanitize_url_removes_query_fragment_and_embedded_credentials(self) -> None:
        diagnostic = (
            "fatal: fetch https://user:pass@example.com/repo.git?access_token=SECRET123#main failed"
        )

        sanitized = _sanitize_url(diagnostic)

        assert "SECRET123" not in sanitized
        assert "user:pass" not in sanitized
        assert sanitized == "fatal: fetch https://***@example.com/repo.git?access_token=*** failed"

    def test_sanitize_url_redacts_parser_errors(self) -> None:
        assert _sanitize_url("https://example.com:bad/repo.git") == "<redacted-git-url>"
