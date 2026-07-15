"""Unit tests for run-scoped RefResolver reuse in ``_maybe_resolve_git_semver``.

Multiple semver deps that resolve against the same upstream repo should share
one ``RefResolver`` instance (and therefore one ``git ls-remote`` tag listing)
instead of constructing a fresh resolver -- and a fresh ls-remote -- per dep.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "src"))

from apm_cli.deps.transport_selection import ProtocolPreference
from apm_cli.install.helpers.ref_reuse import resolve_dep_auth
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
        def __init__(self, *, host=None, token=None, auth_scheme="basic", **kwargs):
            made.append((host, token, auth_scheme, kwargs))

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
        def __init__(self, *, host=None, token=None, auth_scheme="basic", **kwargs):
            # Small delay to widen the construction window. With a working
            # lock only one thread ever reaches here; without it, several
            # would slip in during this sleep and append multiple entries.
            time.sleep(0.02)
            made.append((host, token, auth_scheme, kwargs))

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


def test_semver_resolution_propagates_prefer_ssh_to_ref_resolver():
    """The canonical transport selector must drive semver tag enumeration."""
    made, fake_ref, fake_semver = _patched_resolver_env()
    dep = _semver_dep("owner/repo", "packages/a")

    with (
        patch("apm_cli.marketplace.ref_resolver.RefResolver", fake_ref),
        patch("apm_cli.deps.git_semver_resolver.GitSemverResolver", fake_semver),
    ):
        _maybe_resolve_git_semver(
            dep_ref=dep,
            existing_lockfile=None,
            update_refs=False,
            protocol_pref=ProtocolPreference.SSH,
        )

    assert len(made) == 1
    assert made[0][3]["transport_scheme"] == "ssh"


def test_https_semver_resolution_preserves_custom_port():
    """Custom HTTPS ports must survive the shared RefResolver boundary."""
    made, fake_ref, fake_semver = _patched_resolver_env()
    dep = DependencyReference(
        repo_url="owner/repo",
        host="git.example.com",
        port=8443,
        explicit_scheme="https",
        reference="^1.0.0",
    )

    with (
        patch("apm_cli.marketplace.ref_resolver.RefResolver", fake_ref),
        patch("apm_cli.deps.git_semver_resolver.GitSemverResolver", fake_semver),
    ):
        _maybe_resolve_git_semver(
            dep_ref=dep,
            existing_lockfile=None,
            update_refs=False,
        )

    assert len(made) == 1
    assert made[0][3]["port"] == 8443


def test_cache_separates_transport_identity_for_same_host_and_token():
    """Scheme, SSH user, and port must each select a distinct resolver."""
    from apm_cli.install.helpers.ref_reuse import get_shared_ref_resolver

    class _FakeRefResolver:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    cache: dict = {}
    with patch("apm_cli.marketplace.ref_resolver.RefResolver", _FakeRefResolver):
        https = get_shared_ref_resolver("github.com", "token", cache)
        ssh = get_shared_ref_resolver(
            "github.com",
            "token",
            cache,
            transport_scheme="ssh",
        )
        ssh_port = get_shared_ref_resolver(
            "github.com",
            "token",
            cache,
            transport_scheme="ssh",
            port=2222,
        )
        ssh_user = get_shared_ref_resolver(
            "github.com",
            "token",
            cache,
            transport_scheme="ssh",
            ssh_user="deploy",
        )

    assert len({id(https), id(ssh), id(ssh_port), id(ssh_user)}) == 4
    assert len(cache) == 4


def test_cache_key_does_not_contain_raw_token():
    """The raw PAT must never appear in a cache key (leak prevention)."""
    from apm_cli.install.helpers.ref_reuse import get_shared_ref_resolver

    secret = "ghp_SUPERSECRETTOKENVALUE1234567890"
    cache: dict = {}

    class _FakeRefResolver:
        def __init__(self, *, host=None, token=None, auth_scheme="basic"):
            self.token = token
            self.auth_scheme = auth_scheme

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


def test_cache_separates_basic_and_bearer_for_same_host_and_token():
    """The scheme is part of resolver identity even when credentials match."""
    from apm_cli.install.helpers.ref_reuse import get_shared_ref_resolver

    class _FakeRefResolver:
        def __init__(self, *, host=None, token=None, auth_scheme="basic"):
            self.auth_scheme = auth_scheme

    cache: dict = {}
    with patch("apm_cli.marketplace.ref_resolver.RefResolver", _FakeRefResolver):
        basic = get_shared_ref_resolver("dev.azure.com", "dummy", cache)
        bearer = get_shared_ref_resolver("dev.azure.com", "dummy", cache, auth_scheme="bearer")

    assert basic is not bearer
    assert {basic.auth_scheme, bearer.auth_scheme} == {"basic", "bearer"}
    assert len(cache) == 2


def test_semver_resolution_preserves_bearer_and_basic_auth_schemes():
    """Semver tag listing must use the complete per-dependency auth context."""
    bearer_token = "dummy-ado-bearer"
    basic_token = "dummy-github-basic"
    calls = []

    class _AuthResolver:
        def resolve_for_dep(self, dep_ref):
            if dep_ref.host == "dev.azure.com":
                return SimpleNamespace(token=bearer_token, auth_scheme="bearer")
            return SimpleNamespace(token=basic_token, auth_scheme="basic")

    def _run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=f"{'a' * 40}\trefs/tags/v1.0.0\n",
            stderr="",
        )

    deps = [
        DependencyReference(
            host="dev.azure.com",
            repo_url="example/project/_git/package",
            reference="^1.0.0",
            source="git",
            explicit_scheme="https",
        ),
        DependencyReference(
            host="github.com",
            repo_url="example/package",
            reference="^1.0.0",
            source="git",
            explicit_scheme="https",
        ),
    ]

    cache = {}
    with patch("apm_cli.marketplace.ref_resolver.subprocess.run", side_effect=_run):
        for dep in deps:
            _maybe_resolve_git_semver(
                dep_ref=dep,
                existing_lockfile=None,
                update_refs=True,
                auth_resolver=_AuthResolver(),
                ref_resolver_cache=cache,
            )

    assert {resolver._auth_scheme for resolver in cache.values()} == {"basic", "bearer"}
    ado_args, ado_kwargs = calls[0]
    github_args, github_kwargs = calls[1]
    ado_auth_values = {
        value for key, value in ado_kwargs["env"].items() if key.startswith("GIT_CONFIG_VALUE_")
    }
    assert ado_auth_values == {f"Authorization: Bearer {bearer_token}"}
    assert urlparse(ado_args[-1]).username is None
    assert urlparse(github_args[-1]).password == basic_token
    assert not {
        value
        for key, value in github_kwargs["env"].items()
        if key.startswith("GIT_CONFIG_VALUE_") and value.startswith("Authorization:")
    }


def test_resolve_dep_auth_falls_back_to_basic_when_token_missing():
    """A token-less context must not forward a bearer scheme.

    Forwarding ``auth_scheme="bearer"`` with an empty token would make
    RefResolver attempt a bearer request on what is effectively the
    unauthenticated public-repo path. The resolver must degrade to
    ``(None, "basic", None)`` so the legacy best-effort behaviour is preserved.
    """

    class _NoTokenBearerResolver:
        def resolve_for_dep(self, dep_ref):
            return SimpleNamespace(token=None, auth_scheme="bearer")

    class _EmptyTokenBearerResolver:
        def resolve_for_dep(self, dep_ref):
            return SimpleNamespace(token="", auth_scheme="bearer")

    dep = DependencyReference(
        host="dev.azure.com",
        repo_url="example/project/_git/package",
        reference="^1.0.0",
        source="git",
        explicit_scheme="https",
    )

    assert resolve_dep_auth(dep, _NoTokenBearerResolver()) == (None, "basic", None)
    assert resolve_dep_auth(dep, _EmptyTokenBearerResolver()) == (None, "basic", None)
    assert resolve_dep_auth(dep, None) == (None, "basic", None)


def test_resolve_dep_auth_preserves_sanitized_git_environment():
    sanitized = {"PATH": "/usr/bin", "GIT_TERMINAL_PROMPT": "0"}

    class _Resolver:
        def resolve_for_dep(self, dep_ref):
            return SimpleNamespace(
                token="pat",
                auth_scheme="basic",
                git_env=sanitized,
            )

    dep = DependencyReference(
        host="dev.azure.com",
        repo_url="example/project/_git/package",
        reference="^1.0.0",
        source="git",
        explicit_scheme="https",
    )

    assert resolve_dep_auth(dep, _Resolver()) == ("pat", "basic", sanitized)


def test_semver_ref_resolution_retries_rejected_ado_pat_with_bearer():
    """A stale ADO PAT retries tag listing with the canonical bearer scheme."""
    from apm_cli.core.auth import BearerFallbackOutcome

    calls = []

    class _AuthResolver:
        def resolve_for_dep(self, dep_ref):
            return SimpleNamespace(token="stale-pat", auth_scheme="basic")

        def execute_with_bearer_fallback(
            self,
            dep_ref,
            primary_op,
            bearer_op,
            is_auth_failure,
        ):
            primary = primary_op()
            assert is_auth_failure(primary)
            fallback = bearer_op("fresh-bearer")
            return BearerFallbackOutcome(fallback, True)

    def _run(args, **kwargs):
        calls.append((args, kwargs))
        auth_values = {
            value for key, value in kwargs["env"].items() if key.startswith("GIT_CONFIG_VALUE_")
        }
        if any("Bearer " in value for value in auth_values):
            return SimpleNamespace(
                returncode=0,
                stdout=f"{'a' * 40}\trefs/tags/v1.2.0\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=128,
            stdout="",
            stderr="fatal: The requested URL returned error: 401",
        )

    dep = DependencyReference(
        host="dev.azure.com",
        repo_url="example/project/_git/package",
        reference="^1.0.0",
        source="git",
        explicit_scheme="https",
    )

    with patch("apm_cli.marketplace.ref_resolver.subprocess.run", side_effect=_run):
        resolution = _maybe_resolve_git_semver(
            dep_ref=dep,
            existing_lockfile=None,
            update_refs=True,
            auth_resolver=_AuthResolver(),
        )

    assert resolution.resolved_tag == "v1.2.0"
    assert len(calls) == 2
    auth_headers = [
        {value for key, value in call_kwargs["env"].items() if key.startswith("GIT_CONFIG_VALUE_")}
        for _call_args, call_kwargs in calls
    ]
    assert len(auth_headers[0]) == 1
    assert next(iter(auth_headers[0])).startswith("Authorization: Basic ")
    assert len(auth_headers[1]) == 1
    assert next(iter(auth_headers[1])).split(maxsplit=2)[:2] == [
        "Authorization:",
        "Bearer",
    ]
