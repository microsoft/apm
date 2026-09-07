"""Real local-Git regressions for successful checkout access and pruning."""

from __future__ import annotations

import errno
import logging
import os
import subprocess
import sys
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


@pytest.fixture
def recency_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Keep nested sparse-checkout metadata below Git for Windows' MAX_PATH."""
    return tmp_path_factory.mktemp("recency")


def _populated_cache(
    tmp_path: Path, sparse_paths: list[str] | None
) -> tuple[GitCache, dict[str, str], tuple[Path, Path, Path]]:
    """Populate three real revisions in an isolated Git cache."""
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
    return cache, environment, (used, stale, fresh)


@pytest.mark.parametrize(
    ("refresh", "sparse_paths"),
    [
        pytest.param(False, None, id="hit-full", marks=pytest.mark.windows_compat),
        pytest.param(False, ["skills"], id="hit-sparse", marks=pytest.mark.windows_compat),
        pytest.param(True, None, id="write-dedup-full"),
        pytest.param(True, ["skills"], id="write-dedup-sparse", marks=pytest.mark.windows_compat),
    ],
)
def test_successful_checkout_reuse_survives_prune(
    recency_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    sparse_paths: list[str] | None,
    refresh: bool,
) -> None:
    """Access refreshes the shared SHA root, not merely its variant directory."""
    cache, environment, (used, stale, fresh) = _populated_cache(recency_root, sparse_paths)
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

    reused = GitCache(cache._cache_root, refresh=refresh).get_checkout(
        _REMOTE, None, locked_sha=used.parent.name, env=environment, sparse_paths=sparse_paths
    )

    assert reused == used
    assert reused.stat().st_ino == original_inode
    assert (reused / "skills/content.txt").read_text(encoding="ascii") == "used"
    assert lock_path.is_file()
    assert cache.prune(max_age_days=30) == 1
    assert used.is_dir()
    assert not stale.parent.exists()
    assert fresh.is_dir()


@pytest.mark.parametrize("refresh", [False, True], ids=["hit", "write-dedup"])
@pytest.mark.parametrize("error_type", [ValueError, PermissionError])
def test_failed_sparse_validation_does_not_refresh_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refresh: bool, error_type: type[Exception]
) -> None:
    """A rejected sparse hit must not become recent merely by being inspected."""
    cache = GitCache(tmp_path, refresh=refresh)
    sha = "a" * 40
    checkout = cache._checkouts_root / cache_shard_key(_REMOTE) / sha / _variant_key(["skills"])
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git/HEAD").write_text(sha, encoding="ascii")

    def reject_sparse(*args: object, **kwargs: object) -> Path:
        raise error_type("Invalid sparse symlink")

    monkeypatch.setattr(cache, "_ensure_bare_repo", MagicMock())
    monkeypatch.setattr(cache, "_finalize_sparse_checkout", reject_sparse)
    record_access = MagicMock(wraps=cache._record_checkout_access)
    monkeypatch.setattr(cache, "_record_checkout_access", record_access)
    with pytest.raises(error_type, match="Invalid sparse symlink"):
        cache.get_checkout(_REMOTE, None, locked_sha=sha, sparse_paths=["skills"])

    record_access.assert_not_called()


@pytest.mark.parametrize("refresh", [False, True], ids=["hit", "write-dedup"])
@pytest.mark.parametrize("sparse_paths", [None, ["skills"]], ids=["full", "sparse"])
@pytest.mark.parametrize(
    ("error_type", "error_number", "message"),
    [
        pytest.param(PermissionError, errno.EACCES, "Permission denied", id="permission"),
        pytest.param(FileNotFoundError, errno.ENOENT, "Missing checkout", id="missing"),
        pytest.param(OSError, errno.EIO, "Input/output error", id="io-error"),
    ],
)
def test_checkout_recency_error_contract(
    recency_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    refresh: bool,
    sparse_paths: list[str] | None,
    error_type: type[OSError],
    error_number: int,
    message: str,
) -> None:
    """Only denied recency metadata is non-fatal, with a visible recovery hint."""
    cache, environment, (used, _stale, _fresh) = _populated_cache(recency_root, sparse_paths)
    error = error_type(error_number, message)
    original_inode = used.stat().st_ino
    original_utime = os.utime

    def fail_recency(path: Path, *args: object, **kwargs: object) -> None:
        if Path(path) == used.parent:
            raise error
        original_utime(path, *args, **kwargs)

    monkeypatch.setattr(os, "utime", fail_recency)
    reader = GitCache(cache._cache_root, refresh=refresh)
    with caplog.at_level(logging.WARNING, logger="apm_cli.cache.git_cache"):
        if isinstance(error, PermissionError):
            reused = reader.get_checkout(
                _REMOTE,
                None,
                locked_sha=used.parent.name,
                env=environment,
                sparse_paths=sparse_paths,
            )
            assert reused == used
            assert reused.stat().st_ino == original_inode
            assert (reused / "skills/content.txt").read_text(encoding="ascii") == "used"
            assert str(used.parent) in caplog.text
            assert "Permission denied" in caplog.text
            assert "cache prune may evict" in caplog.text
            assert "Check cache permissions or set APM_CACHE_DIR" in caplog.text
        else:
            with pytest.raises(type(error)) as raised:
                reader.get_checkout(
                    _REMOTE,
                    None,
                    locked_sha=used.parent.name,
                    env=environment,
                    sparse_paths=sparse_paths,
                )
            assert raised.value is error
            assert not caplog.records


@pytest.mark.windows_compat
def test_recency_permission_warning_reaches_default_cli_stderr(recency_root: Path) -> None:
    """The real CLI logging configuration exposes the backend warning by default."""
    cache, environment, (used, _stale, _fresh) = _populated_cache(recency_root, None)
    environment.pop("APM_LOG_LEVEL", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from pathlib import Path
from unittest.mock import patch
from apm_cli.cache.git_cache import GitCache
from apm_cli.cli import _configure_logging

_configure_logging()
with patch("apm_cli.cache.git_cache.os.utime", side_effect=PermissionError("Denied timestamp")):
    checkout = GitCache(Path(sys.argv[1])).get_checkout(sys.argv[2], None, locked_sha=sys.argv[3])
print(checkout)
""",
            str(cache._cache_root),
            _REMOTE,
            used.parent.name,
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert str(used) in result.stdout
    assert "[!] Cannot update Git cache recency" in result.stderr
    assert "Denied timestamp" in result.stderr
    assert "cache prune may evict" in result.stderr
    assert "APM_CACHE_DIR" in result.stderr
