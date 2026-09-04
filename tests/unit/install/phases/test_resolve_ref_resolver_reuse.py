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

from apm_cli.core.auth import AuthResolver
from apm_cli.deps.transport_selection import (
    ProtocolPreference,
    TransportAttempt,
    TransportPlan,
)
from apm_cli.install.helpers.ref_reuse import maybe_resolve_git_semver, resolve_dep_auth
from apm_cli.install.phases.resolve import _maybe_resolve_git_semver
from apm_cli.models.dependency.reference import DependencyReference


def _authorization_config_values(env: dict[str, str]) -> set[str]:
    """Return only indexed Git config values whose key is an auth header."""
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    return {
        env.get(f"GIT_CONFIG_VALUE_{index}", "")
        for index in range(count)
        if "extraheader" in env.get(f"GIT_CONFIG_KEY_{index}", "").lower()
        and env.get(f"GIT_CONFIG_VALUE_{index}", "").lower().startswith("authorization:")
    }


def _semver_dep(repo_url: str, virtual_path: str) -> DependencyReference:
    """A git-source semver-range dep (ref_kind == 'semver')."""
    return DependencyReference(
        repo_url=repo_url,
        reference=">=0.0.1",
        virtual_path=virtual_path,
        is_virtual=True,
    )


def _ado_semver_dep() -> DependencyReference:
    """Return a production-shaped ADO semver reference."""
    dep = DependencyReference.parse("https://dev.azure.com/example/project/_git/package#^1.0.0")
    dep.source = "git"
    return dep


def _git_config_values(environment: dict[str, str]) -> set[str]:
    """Return indexed Git config values, including non-auth policy entries."""
    return {value for key, value in environment.items() if key.startswith("GIT_CONFIG_VALUE_")}


def _set_noninteractive_git_policy(monkeypatch) -> None:
    """Arrange the indexed Git policy that auth overlays must preserve."""
    for name in tuple(os.environ):
        if name == "GIT_CONFIG_COUNT" or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.interactive")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "never")


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


def test_same_repo_deps_build_shared_git_environment_once() -> None:
    """Cached semver resolvers build their remote environment only on a miss."""
    made, fake_ref, fake_semver = _patched_resolver_env()
    context = SimpleNamespace(
        token="shared-token",
        auth_scheme="basic",
        host_info=SimpleNamespace(kind="github"),
    )
    auth_resolver = MagicMock()
    auth_resolver.uses_public_github_anonymous_first.return_value = False
    auth_resolver.resolve_for_dep.return_value = context
    auth_resolver.git_env_for_remote.return_value = {"SHARED": "1"}
    selector = MagicMock()
    selector.select.return_value = TransportPlan(
        attempts=[TransportAttempt(scheme="https", use_token=True, label="HTTPS")],
        strict=True,
    )
    cache: dict = {}

    with (
        patch("apm_cli.marketplace.ref_resolver.RefResolver", fake_ref),
        patch("apm_cli.deps.git_semver_resolver.GitSemverResolver", fake_semver),
    ):
        for index in range(10):
            maybe_resolve_git_semver(
                dep_ref=_semver_dep("owner/repo", f"packages/{index}"),
                existing_lockfile=None,
                update_refs=True,
                auth_resolver=auth_resolver,
                ref_resolver_cache=cache,
                transport_selector=selector,
            )

    assert len(made) == 1
    assert len(cache) == 1
    assert auth_resolver.git_env_for_remote.call_count == 1


def test_git_environment_builds_scale_with_contexts_not_dependency_count() -> None:
    """N and 10N dependencies build exactly N remote environments."""
    unique_contexts = 4

    def run_batch(dependencies_per_context: int) -> tuple[int, int, int]:
        made, fake_ref, fake_semver = _patched_resolver_env()
        auth_resolver = MagicMock()
        auth_resolver.uses_public_github_anonymous_first.return_value = False
        auth_resolver.resolve_for_dep.side_effect = lambda dep: SimpleNamespace(
            token=f"token-for-{dep.host}",
            auth_scheme="basic",
            host_info=SimpleNamespace(kind="github"),
        )
        auth_resolver.git_env_for_remote.side_effect = lambda _ctx, remote_url: {
            "REMOTE_URL": remote_url
        }
        selector = MagicMock()
        selector.select.return_value = TransportPlan(
            attempts=[TransportAttempt(scheme="https", use_token=True, label="HTTPS")],
            strict=True,
        )
        cache: dict = {}

        with (
            patch("apm_cli.marketplace.ref_resolver.RefResolver", fake_ref),
            patch("apm_cli.deps.git_semver_resolver.GitSemverResolver", fake_semver),
        ):
            for context_index in range(unique_contexts):
                for dependency_index in range(dependencies_per_context):
                    dep = _semver_dep(
                        f"owner-{context_index}/repo",
                        f"packages/{dependency_index}",
                    )
                    dep.host = f"git-{context_index}.example.test"
                    maybe_resolve_git_semver(
                        dep_ref=dep,
                        existing_lockfile=None,
                        update_refs=True,
                        auth_resolver=auth_resolver,
                        ref_resolver_cache=cache,
                        transport_selector=selector,
                    )

        return (
            len(made),
            len(cache),
            auth_resolver.git_env_for_remote.call_count,
        )

    assert run_batch(1) == (unique_contexts, unique_contexts, unique_contexts)
    assert run_batch(10) == (unique_contexts, unique_contexts, unique_contexts)


def test_git_environment_factory_is_single_flight_on_shared_cache_miss() -> None:
    """Concurrent first touches build one environment inside the cache lock."""
    import threading
    import time

    from apm_cli.install.helpers.ref_reuse import get_shared_ref_resolver

    thread_count = 8
    barrier = threading.Barrier(thread_count)
    cache: dict = {}
    cache_lock = threading.Lock()
    factory_calls: list[int] = []
    resolvers: list[object] = []
    errors: list[BaseException] = []

    class _FakeRefResolver:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def git_env_factory() -> dict[str, str]:
        time.sleep(0.02)
        factory_calls.append(1)
        return {"REMOTE": "shared"}

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            resolver = get_shared_ref_resolver(
                "github.com",
                "shared-token",
                cache,
                cache_lock,
                git_env_factory=git_env_factory,
            )
            resolvers.append(resolver)
        except BaseException as exc:
            errors.append(exc)

    with patch("apm_cli.marketplace.ref_resolver.RefResolver", _FakeRefResolver):
        threads = [threading.Thread(target=worker) for _ in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert len(factory_calls) == 1
    assert len({id(resolver) for resolver in resolvers}) == 1


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


def test_rewritten_semver_preserves_requested_url_for_git() -> None:
    """Tag resolution starts from the same URL Git must rewrite exactly once."""
    dep = _semver_dep("owner/repo", "packages/a")
    requested = "https://github.com/owner/repo.git"
    selector = MagicMock()
    selector.select.return_value = TransportPlan(
        attempts=[
            TransportAttempt(
                scheme="file",
                use_token=False,
                label="Git URL rewrite (file)",
                requested_url=requested,
                effective_url="file:///fixture/owner/repo",
            )
        ],
        strict=True,
    )
    fake_semver = MagicMock()
    fake_semver.return_value.resolve.return_value = "RESOLUTION"
    fake_ref = MagicMock()
    token_env = {
        "GIT_TOKEN": "semver-token",
        "GITHUB_TOKEN": "platform-token",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic sentinel",
    }

    with (
        patch(
            "apm_cli.install.helpers.ref_reuse.resolve_dep_auth",
            side_effect=[
                ("semver-token", "basic", token_env),
                (None, "basic", {"CLEAN": "1"}),
                (None, "basic", {"CLEAN": "1"}),
            ],
        ),
        patch("apm_cli.marketplace.ref_resolver.RefResolver", fake_ref),
        patch("apm_cli.deps.git_semver_resolver.GitSemverResolver", fake_semver),
    ):
        result = maybe_resolve_git_semver(
            dep_ref=dep,
            existing_lockfile=None,
            update_refs=True,
            transport_selector=selector,
        )

    assert result == "RESOLUTION"
    assert selector.select.call_args.kwargs["candidate_url"] == "https://github.com/owner/repo.git"
    assert fake_semver.return_value.resolve.call_args.kwargs["remote_url"] == requested
    child_env = fake_ref.call_args.kwargs["git_env"]
    assert child_env == {"CLEAN": "1"}


def test_same_origin_https_rewrite_preserves_managed_auth() -> None:
    """A rewritten HTTPS semver request keeps its selected GitHub credential."""
    dep = _semver_dep("owner/repo", "packages/a")
    requested = "https://github.com/owner/repo.git"
    selector = MagicMock()
    selector.select.return_value = TransportPlan(
        attempts=[
            TransportAttempt(
                scheme="https",
                use_token=True,
                label="Git URL rewrite (https)",
                requested_url=requested,
                effective_url="https://github.com/mirror/repo.git",
            )
        ],
        strict=True,
    )
    context = SimpleNamespace(
        token="semver-token",
        auth_scheme="basic",
        host_info=SimpleNamespace(kind="github"),
    )
    auth_resolver = MagicMock()
    auth_resolver.uses_public_github_anonymous_first.return_value = False
    auth_resolver.resolve_for_dep.return_value = context
    auth_resolver.git_env_for_remote.side_effect = lambda ctx, _url: (
        AuthResolver.git_env_for_context(ctx, base_env={})
    )
    fake_semver = MagicMock()
    fake_semver.return_value.resolve.return_value = "RESOLUTION"
    fake_ref = MagicMock()

    with (
        patch("apm_cli.marketplace.ref_resolver.RefResolver", fake_ref),
        patch("apm_cli.deps.git_semver_resolver.GitSemverResolver", fake_semver),
    ):
        result = maybe_resolve_git_semver(
            dep_ref=dep,
            existing_lockfile=None,
            update_refs=True,
            auth_resolver=auth_resolver,
            transport_selector=selector,
        )

    assert result == "RESOLUTION"
    assert fake_ref.call_args.kwargs["token"] == "semver-token"
    assert _authorization_config_values(fake_ref.call_args.kwargs["git_env"])
    assert fake_semver.return_value.resolve.call_args.kwargs["remote_url"] == requested


def test_generic_https_semver_preserves_native_helper() -> None:
    """Generic semver resolution keeps a helper and never promotes its credential."""
    dep = _semver_dep("owner/repo", "packages/a")
    dep.host = "git.example.com"
    candidate = "https://git.example.com/owner/repo.git"
    selector = MagicMock()
    selector.select.return_value = TransportPlan(
        attempts=[TransportAttempt(scheme="https", use_token=False, label="plain HTTPS")],
        strict=True,
    )
    context = SimpleNamespace(
        token="helper-credential",
        auth_scheme="basic",
        host_info=SimpleNamespace(kind="generic"),
    )
    helper_env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "fixture-helper",
    }
    auth_resolver = MagicMock()
    auth_resolver.uses_public_github_anonymous_first.return_value = False
    auth_resolver.resolve_for_dep.return_value = context
    auth_resolver.git_env_for_remote.return_value = helper_env
    fake_semver = MagicMock()
    fake_semver.return_value.resolve.return_value = "RESOLUTION"
    fake_ref = MagicMock()

    with (
        patch("apm_cli.marketplace.ref_resolver.RefResolver", fake_ref),
        patch("apm_cli.deps.git_semver_resolver.GitSemverResolver", fake_semver),
    ):
        result = maybe_resolve_git_semver(
            dep_ref=dep,
            existing_lockfile=None,
            update_refs=True,
            auth_resolver=auth_resolver,
            transport_selector=selector,
        )

    assert result == "RESOLUTION"
    assert selector.select.call_args.kwargs["candidate_url"] == candidate
    assert fake_ref.call_args.kwargs["token"] is None
    assert fake_ref.call_args.kwargs["git_env"] == helper_env


def test_public_github_semver_defers_auth_until_anonymous_failure() -> None:
    """Public GitHub tag discovery preserves AuthResolver's anonymous-first chain."""
    dep = _semver_dep("owner/repo", "packages/a")
    auth_resolver = MagicMock()
    auth_resolver.uses_public_github_anonymous_first.return_value = True
    auth_resolver.build_public_github_anonymous_git_env.return_value = {"ANONYMOUS": "1"}
    fake_semver = MagicMock()
    fake_semver.return_value.resolve.return_value = "RESOLUTION"
    fake_ref = MagicMock()

    with (
        patch("apm_cli.marketplace.ref_resolver.RefResolver", fake_ref),
        patch("apm_cli.deps.git_semver_resolver.GitSemverResolver", fake_semver),
    ):
        result = maybe_resolve_git_semver(
            dep_ref=dep,
            existing_lockfile=None,
            update_refs=True,
            auth_resolver=auth_resolver,
        )

    assert result == "RESOLUTION"
    auth_resolver.resolve_for_dep.assert_not_called()
    assert fake_ref.call_args.kwargs["token"] is None
    assert fake_ref.call_args.kwargs["git_env"] == {"ANONYMOUS": "1"}
    assert fake_ref.call_args.kwargs["unauth_first"] is True


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


def test_semver_resolution_preserves_bearer_and_basic_auth_schemes(monkeypatch):
    """Semver tag listing must use the complete per-dependency auth context."""
    _set_noninteractive_git_policy(monkeypatch)
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
        _ado_semver_dep(),
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
    ado_auth_values = _authorization_config_values(ado_kwargs["env"])
    assert ado_auth_values == {f"Authorization: Bearer {bearer_token}"}
    assert urlparse(ado_args[-1]).username is None
    assert "never" in _git_config_values(ado_kwargs["env"])
    assert urlparse(github_args[-1]).username is None
    assert urlparse(github_args[-1]).password is None
    assert any(
        value.startswith("Authorization: Basic ")
        for value in _authorization_config_values(github_kwargs["env"])
    )
    assert "never" in _git_config_values(github_kwargs["env"])


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

    dep = _ado_semver_dep()

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

    dep = _ado_semver_dep()

    assert resolve_dep_auth(dep, _Resolver()) == ("pat", "basic", sanitized)


def test_semver_ref_resolution_retries_rejected_ado_pat_with_bearer(monkeypatch):
    """A stale ADO PAT retries tag listing with the canonical bearer scheme."""
    _set_noninteractive_git_policy(monkeypatch)
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
        auth_values = _authorization_config_values(kwargs["env"])
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

    dep = _ado_semver_dep()

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
        _authorization_config_values(call_kwargs["env"]) for _call_args, call_kwargs in calls
    ]
    assert all("never" in _git_config_values(call_kwargs["env"]) for _args, call_kwargs in calls)
    assert len(auth_headers[0]) == 1
    assert next(iter(auth_headers[0])).startswith("Authorization: Basic ")
    assert len(auth_headers[1]) == 1
    assert next(iter(auth_headers[1])).split(maxsplit=2)[:2] == [
        "Authorization:",
        "Bearer",
    ]
