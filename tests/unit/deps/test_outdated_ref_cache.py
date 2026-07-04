"""Tests for per-invocation ``ls-remote`` dedup in ``apm outdated``.

Two axes are exercised:

1. **Dedup (the optimization).** Multiple locked deps sharing one upstream
   repo issue exactly one ``git ls-remote`` per ref-family, both directly on
   :class:`OutdatedRefCache` and end-to-end through ``apm outdated``. Each
   dedup assertion is written to *fail* if the wrapper were removed (the raw
   per-dep path issues one ls-remote per dep).
2. **Correctness (no degradation).** Distinct repos never share a cached
   listing, tag-only and tags+branches families never collide, failures are
   not cached, and ``apm outdated`` still reports accurate
   up-to-date/outdated status for same-repo deps -- a stale cache must never
   surface a false "up to date".
"""

from __future__ import annotations

import os
import sys
import threading
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from apm_cli.deps.outdated_ref_cache import OutdatedRefCache, _identity_key
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, RemoteRef


def _dep(repo_url: str = "owner/repo", host=None, virtual_path=None) -> DependencyReference:
    return DependencyReference(
        repo_url=repo_url,
        reference="v1.0.0",
        host=host,
        virtual_path=virtual_path,
        is_virtual=bool(virtual_path),
    )


def _tag(name: str, sha: str = "abc123") -> RemoteRef:
    return RemoteRef(name=name, ref_type=GitReferenceType.TAG, commit_sha=sha)


# ---------------------------------------------------------------------------
# Dedup on OutdatedRefCache directly
# ---------------------------------------------------------------------------
def test_same_repo_deps_issue_one_ls_remote():
    """Three same-repo (different-subdir) deps -> one underlying ls-remote.

    Traps the regression: the un-wrapped downloader lists refs once per dep,
    so this asserts call_count == 1 (not 3).
    """
    inner = MagicMock()
    inner.list_remote_refs.return_value = [_tag("v1.0.0")]
    cache = OutdatedRefCache(inner)

    deps = [
        _dep(virtual_path="packages/a"),
        _dep(virtual_path="packages/b"),
        _dep(virtual_path="packages/c"),
    ]
    results = [cache.list_remote_refs(d) for d in deps]

    assert inner.list_remote_refs.call_count == 1
    # Every caller still gets the correct refs back.
    assert all(r == [_tag("v1.0.0")] for r in results)


def test_distinct_repos_each_fetch():
    """Different repos must not share a cached listing (no cross-repo bleed)."""
    inner = MagicMock()
    inner.list_remote_refs.side_effect = [[_tag("v1.0.0")], [_tag("v2.0.0")]]
    cache = OutdatedRefCache(inner)

    a = cache.list_remote_refs(_dep("owner/alpha"))
    b = cache.list_remote_refs(_dep("owner/beta"))

    assert inner.list_remote_refs.call_count == 2
    assert a == [_tag("v1.0.0")]
    assert b == [_tag("v2.0.0")]


def test_distinct_hosts_each_fetch():
    """Same repo path on different hosts are distinct upstreams."""
    inner = MagicMock()
    inner.list_remote_refs.side_effect = [[_tag("v1.0.0")], [_tag("v9.9.9")]]
    cache = OutdatedRefCache(inner)

    cache.list_remote_refs(_dep("owner/repo", host=None))
    cache.list_remote_refs(_dep("owner/repo", host="example.com"))

    assert inner.list_remote_refs.call_count == 2


def test_tag_family_and_heads_family_do_not_collide():
    """tags-only and tags+branches are different listings; both fetched once."""
    inner = MagicMock()
    inner.list_remote_refs.return_value = [_tag("v1.0.0")]
    inner.list_remote_tag_refs.return_value = [_tag("v1.0.0")]
    cache = OutdatedRefCache(inner)

    cache.list_remote_refs(_dep())
    cache.list_remote_refs(_dep(virtual_path="packages/b"))
    cache.list_remote_tag_refs(_dep())
    cache.list_remote_tag_refs(_dep(virtual_path="packages/b"))

    assert inner.list_remote_refs.call_count == 1
    assert inner.list_remote_tag_refs.call_count == 1


def test_failures_are_not_cached():
    """A raised ls-remote must not poison the cache (per-dep unknown preserved)."""
    inner = MagicMock()
    inner.list_remote_refs.side_effect = [RuntimeError("boom"), [_tag("v1.0.0")]]
    cache = OutdatedRefCache(inner)

    try:
        cache.list_remote_refs(_dep())
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass

    # Second attempt for the same repo re-fetches (failure was not cached).
    assert cache.list_remote_refs(_dep()) == [_tag("v1.0.0")]
    assert inner.list_remote_refs.call_count == 2


def test_delegates_unknown_attributes_to_wrapped_downloader():
    """Non-ref methods/attrs pass through untouched (registry/marketplace paths)."""
    inner = MagicMock()
    inner.persistent_git_cache = "sentinel"
    inner.some_method.return_value = 42
    cache = OutdatedRefCache(inner)

    assert cache.persistent_git_cache == "sentinel"
    assert cache.some_method() == 42


def test_identity_key_contains_no_token():
    """Auth-lens: the cache key is repo identity, never a credential."""
    secret = "ghp_SUPERSECRETTOKENVALUE1234567890"
    dep = _dep()
    dep.token = secret  # even if a token rode along, it must not enter the key
    key = _identity_key(dep, include_heads=True)
    assert secret not in repr(key)


def test_concurrent_same_repo_coalesces_to_one_fetch():
    """Parallel first-touches for one repo issue exactly one ls-remote."""
    import time

    n_threads = 8
    calls: list[int] = []
    calls_lock = threading.Lock()
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    class _SlowInner:
        def list_remote_refs(self, dep_ref):
            # Widen the fetch window so an unlocked implementation would let
            # several threads in and record multiple calls.
            time.sleep(0.02)
            with calls_lock:
                calls.append(1)
            return [_tag("v1.0.0")]

        def list_remote_tag_refs(self, dep_ref):  # pragma: no cover - unused here
            return []

    cache = OutdatedRefCache(_SlowInner())

    def worker(i):
        try:
            barrier.wait(timeout=5)
            cache.list_remote_refs(_dep(virtual_path=f"packages/p{i}"))
        except BaseException as exc:  # surfaced on main thread
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not any(t.is_alive() for t in threads), "worker thread(s) did not terminate"
    assert not errors, f"worker thread(s) raised: {errors!r}"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# End-to-end through ``apm outdated``: dedup AND correctness preserved
# ---------------------------------------------------------------------------
_PATCH_LOCKFILE = "apm_cli.deps.lockfile.LockFile"
_PATCH_GET_LOCKFILE_PATH = "apm_cli.deps.lockfile.get_lockfile_path"
_PATCH_MIGRATE = "apm_cli.deps.lockfile.migrate_lockfile_if_needed"
_PATCH_DOWNLOADER = "apm_cli.deps.github_downloader.GitHubPackageDownloader"
_PATCH_AUTH = "apm_cli.core.auth.AuthResolver"
_PATCH_GET_APM_DIR = "apm_cli.core.scope.get_apm_dir"


def _locked(repo_url, virtual_path, resolved_ref):
    from apm_cli.deps.lockfile import LockedDependency

    return LockedDependency(
        repo_url=repo_url,
        resolved_ref=resolved_ref,
        resolved_commit="deadbee",
        virtual_path=virtual_path,
        is_virtual=True,
    )


def _make_lockfile(deps_dict):
    from apm_cli.deps.lockfile import LockFile

    lf = LockFile()
    lf.dependencies = deps_dict
    return lf


def test_outdated_command_dedupes_and_preserves_correctness(tmp_path):
    """Two same-repo virtual deps -> one ls-remote, and outdated stays accurate.

    Locked at v1.0.0 while upstream's highest tag is v2.0.0: both rows must
    report ``outdated`` (a stale/misapplied cache would wrongly say
    ``up-to-date``), while the shared upstream is listed exactly once.
    """
    from click.testing import CliRunner

    from apm_cli.cli import cli

    deps = {
        "org/mono#packages/a": _locked("org/mono", "packages/a", "v1.0.0"),
        "org/mono#packages/b": _locked("org/mono", "packages/b", "v1.0.0"),
    }

    inner = MagicMock()
    # Same upstream repo -> one listing; highest tag is v2.0.0 (newer).
    inner.list_remote_refs.return_value = [_tag("v1.0.0"), _tag("v2.0.0")]

    cwd = os.getcwd()
    with (
        patch(_PATCH_MIGRATE),
        patch(_PATCH_GET_LOCKFILE_PATH, return_value=tmp_path / "apm.lock.yaml"),
        patch(_PATCH_GET_APM_DIR, return_value=tmp_path),
        patch(_PATCH_LOCKFILE) as mock_lf_cls,
        patch(_PATCH_DOWNLOADER, return_value=inner),
        patch(_PATCH_AUTH),
    ):
        mock_lf_cls.read.return_value = _make_lockfile(deps)
        try:
            os.chdir(tmp_path)
            # -j 0 => sequential, so the dedup count is deterministic.
            result = CliRunner().invoke(cli, ["outdated", "-j", "0"])
        finally:
            os.chdir(cwd)

    assert result.exit_code == 0, result.output
    # Dedup: one shared upstream listed exactly once (would be 2 un-wrapped).
    assert inner.list_remote_refs.call_count == 1
    # Correctness: both same-repo deps still reported outdated (v1 -> v2).
    assert "outdated" in result.output.lower()
    assert "up-to-date" not in result.output.lower()
