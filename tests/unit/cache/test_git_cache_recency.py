"""Real local-Git regressions for successful checkout access and pruning."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.cache.git_cache import GitCache, _variant_key
from apm_cli.cache.locking import shard_lock
from apm_cli.cache.url_normalize import cache_shard_key
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = pytest.mark.component

_STALE_NS = 946684800000000000
_FRESH_NS = 4102444800000000000
_REMOTE = "https://gitlab.example.invalid/cache/recency.git"


@pytest.mark.windows_compat
@pytest.mark.parametrize("sparse_paths", [None, ["skills"]], ids=["full", "sparse"])
@pytest.mark.parametrize("refresh", [False, True], ids=["hit", "write-dedup"])
def test_successful_checkout_reuse_survives_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sparse_paths: list[str] | None, refresh: bool
) -> None:
    """Access refreshes the shared SHA root, not merely its variant directory."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    repository = repositories.create("recency")
    skills = repository.worktree / "skills"
    skills.mkdir()
    commits = []
    for name in ("used", "stale", "fresh"):
        (skills / "content.txt").write_text(name, encoding="ascii")
        commits.append(repositories.commit(repository, message=name))
    environment = repositories.url_rewrite_subprocess_env(repository, _REMOTE)
    cache = GitCache(isolated.cache_root)
    used, stale, fresh = (
        cache.get_checkout(
            _REMOTE, None, locked_sha=commit.sha, env=environment, sparse_paths=sparse_paths
        )
        for commit in commits
    )
    original_inode = used.stat().st_ino
    lock_path = Path(shard_lock(used).lock_file)
    lock_path.touch()
    original_unlink = Path.unlink

    def retain_lock(path: Path, missing_ok: bool = False) -> None:
        # FileLock suppresses this on Windows when another handle prevents
        # deletion. Older supported Unix filelock versions also retain locks.
        if path == lock_path:
            raise PermissionError("Fixture retains the checkout lock file")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", retain_lock)
    for checkout in (used, stale):
        os.utime(checkout.parent, ns=(_STALE_NS, _STALE_NS))
    os.utime(fresh.parent, ns=(_FRESH_NS, _FRESH_NS))

    reused = GitCache(isolated.cache_root, refresh=refresh).get_checkout(
        _REMOTE, None, locked_sha=commits[0].sha, env=environment, sparse_paths=sparse_paths
    )

    assert reused == used
    assert reused.stat().st_ino == original_inode
    assert (reused / "skills/content.txt").read_text(encoding="ascii") == "used"
    assert lock_path.is_file()
    assert cache.prune(max_age_days=30) == 1
    assert used.is_dir()
    assert not stale.parent.exists()
    assert fresh.is_dir()


def test_failed_sparse_validation_does_not_refresh_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected sparse hit must not become recent merely by being inspected."""
    cache = GitCache(tmp_path)
    sha = "a" * 40
    checkout = cache._checkouts_root / cache_shard_key(_REMOTE) / sha / _variant_key(["skills"])
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git/HEAD").write_text(sha, encoding="ascii")

    def reject_sparse(*args: object, **kwargs: object) -> Path:
        raise ValueError("Invalid sparse symlink")

    monkeypatch.setattr(cache, "_finalize_sparse_checkout", reject_sparse)
    record_access = MagicMock(wraps=cache._record_checkout_access)
    monkeypatch.setattr(cache, "_record_checkout_access", record_access)
    with pytest.raises(ValueError, match="Invalid sparse symlink"):
        cache.get_checkout(_REMOTE, None, locked_sha=sha, sparse_paths=["skills"])

    record_access.assert_not_called()
