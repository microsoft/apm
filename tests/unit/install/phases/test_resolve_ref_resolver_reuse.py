"""Unit tests for run-scoped RefResolver reuse in ``_maybe_resolve_git_semver``.

Multiple semver deps that resolve against the same upstream repo should share
one ``RefResolver`` instance (and therefore one ``git ls-remote`` tag listing)
instead of constructing a fresh resolver -- and a fresh ls-remote -- per dep.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "src"))

from apm_cli.install.phases.resolve import _maybe_resolve_git_semver
from apm_cli.models.dependency.reference import DependencyReference


def _semver_dep(repo_url: str, virtual_path: str) -> DependencyReference:
    """A git-source semver-range dep (ref_kind == 'semver')."""
    return DependencyReference(
        repo_url=repo_url,
        reference=">=0.0.1",
        virtual_path=virtual_path,
        is_virtual=True,
    )


def _patched_resolver_env():
    """Patch RefResolver (counting ctor) + GitSemverResolver (no-op resolve)."""
    made = []

    class _FakeRefResolver:
        def __init__(self, *, host=None, token=None):
            made.append((host, token))

    fake_semver = MagicMock()
    fake_semver.return_value.resolve.return_value = "RESOLUTION"
    return made, _FakeRefResolver, fake_semver


def test_same_repo_deps_share_one_ref_resolver():
    made, fake_ref, fake_semver = _patched_resolver_env()
    cache: dict = {}
    deps = [
        _semver_dep("owner/repo", "packages/a"),
        _semver_dep("owner/repo", "packages/b"),
        _semver_dep("owner/repo", "packages/c"),
    ]
    with (
        patch("apm_cli.marketplace.ref_resolver.RefResolver", fake_ref),
        patch("apm_cli.deps.git_semver_resolver.GitSemverResolver", fake_semver),
    ):
        for d in deps:
            _maybe_resolve_git_semver(
                dep_ref=d,
                existing_lockfile=None,
                update_refs=False,
                ref_resolver_cache=cache,
            )
    # Three deps, same (host, token) -> exactly one RefResolver constructed.
    assert len(made) == 1
    assert len(cache) == 1


def test_no_cache_constructs_one_resolver_per_dep():
    """Default (cache=None) preserves the legacy one-resolver-per-dep path."""
    made, fake_ref, fake_semver = _patched_resolver_env()
    deps = [
        _semver_dep("owner/repo", "packages/a"),
        _semver_dep("owner/repo", "packages/b"),
    ]
    with (
        patch("apm_cli.marketplace.ref_resolver.RefResolver", fake_ref),
        patch("apm_cli.deps.git_semver_resolver.GitSemverResolver", fake_semver),
    ):
        for d in deps:
            _maybe_resolve_git_semver(
                dep_ref=d,
                existing_lockfile=None,
                update_refs=False,
                ref_resolver_cache=None,
            )
    assert len(made) == 2


def test_concurrent_same_repo_deps_share_one_resolver_under_lock():
    """Under a lock, parallel first-touch resolves still build one resolver.

    Mirrors the level-batched worker pool: many threads call
    _maybe_resolve_git_semver for the same (host, token) at once. With the
    lock threaded through, exactly one RefResolver is constructed.
    """
    import threading
    import time

    n_threads = 8
    made = []
    all_started = threading.Barrier(n_threads)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    class _SlowFakeRefResolver:
        def __init__(self, *, host=None, token=None):
            # Small delay to widen the construction window. With a working
            # lock only one thread ever reaches here; without it, several
            # would slip in during this sleep and append multiple entries.
            time.sleep(0.02)
            made.append((host, token))

    fake_semver = MagicMock()
    fake_semver.return_value.resolve.return_value = "R"
    cache: dict = {}
    lock = threading.Lock()

    def worker(i):
        # Release all threads at once so they contend on the cache together.
        # Capture any exception so a failure inside a worker thread surfaces
        # as a test failure instead of a lost/warning-only thread error.
        try:
            all_started.wait(timeout=5)
            _maybe_resolve_git_semver(
                dep_ref=_semver_dep("owner/repo", f"packages/p{i}"),
                existing_lockfile=None,
                update_refs=False,
                ref_resolver_cache=cache,
                ref_resolver_cache_lock=lock,
            )
        except BaseException as exc:  # re-raised on the main thread
            with errors_lock:
                errors.append(exc)

    with (
        patch("apm_cli.marketplace.ref_resolver.RefResolver", _SlowFakeRefResolver),
        patch("apm_cli.deps.git_semver_resolver.GitSemverResolver", fake_semver),
    ):
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

    # Every worker must have terminated (no hang / deadlock).
    assert not any(t.is_alive() for t in threads), "worker thread(s) did not terminate"
    # Any exception raised inside a worker must fail the test, not be swallowed.
    assert not errors, f"worker thread(s) raised: {errors!r}"
    # Exactly one resolver despite n concurrent first-touches.
    assert len(made) == 1
    assert len(cache) == 1


def test_distinct_hosts_get_distinct_resolvers():
    made, fake_ref, fake_semver = _patched_resolver_env()
    cache: dict = {}
    deps = [
        _semver_dep("owner/repo", "packages/a"),  # host defaults
        DependencyReference(
            repo_url="owner/repo",
            reference=">=0.0.1",
            virtual_path="packages/b",
            is_virtual=True,
            host="example.com",
        ),
    ]
    with (
        patch("apm_cli.marketplace.ref_resolver.RefResolver", fake_ref),
        patch("apm_cli.deps.git_semver_resolver.GitSemverResolver", fake_semver),
    ):
        for d in deps:
            _maybe_resolve_git_semver(
                dep_ref=d,
                existing_lockfile=None,
                update_refs=False,
                ref_resolver_cache=cache,
            )
    # Different host -> different cache key -> two resolvers.
    assert len(made) == 2
    assert len(cache) == 2


def test_cache_key_does_not_contain_raw_token():
    """The raw PAT must never appear in a cache key (leak prevention)."""
    from apm_cli.install.helpers.ref_reuse import get_shared_ref_resolver

    secret = "ghp_SUPERSECRETTOKENVALUE1234567890"
    cache: dict = {}

    class _FakeRefResolver:
        def __init__(self, *, host=None, token=None):
            self.token = token

    with patch("apm_cli.marketplace.ref_resolver.RefResolver", _FakeRefResolver):
        resolver = get_shared_ref_resolver("github.com", secret, cache)

    # The resolver still receives the real token (auth works)...
    assert resolver.token == secret
    # ...but no cache key exposes it.
    for key in cache:
        assert secret not in repr(key)
    # Distinct tokens still map to distinct buckets.
    with patch("apm_cli.marketplace.ref_resolver.RefResolver", _FakeRefResolver):
        get_shared_ref_resolver("github.com", "ghp_a_different_token_000000", cache)
    assert len(cache) == 2
