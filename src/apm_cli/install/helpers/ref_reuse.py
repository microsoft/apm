"""Run-scoped Git reference resolution helpers.

Extracted from :mod:`apm_cli.install.phases.resolve` to keep that phase
module within its LOC budget (see
``tests/unit/install/test_architecture_invariants.py``).

Multiple semver deps from the same upstream repo should share one
``RefResolver`` so its per-instance ``git ls-remote`` tag listing is fetched
once per repo instead of once per dep.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.deps.github_downloader import GitHubPackageDownloader
    from apm_cli.models.dependency.reference import DependencyReference

RefResolverCacheKey = tuple[str | None, str | None, str, tuple[str, str | None, int | None]]


def _token_fingerprint(token: str | None) -> str | None:
    """Return a non-reversible fingerprint of ``token`` for use as a cache key.

    The cache lives on ``InstallContext``; keying by the raw PAT would leak
    the credential into any ``repr(ctx)`` / debug dump / dict-key trace. A
    truncated SHA-256 keeps distinct tokens in distinct buckets without
    storing the secret. ``None`` (unauthenticated) maps to ``None``.
    """
    if token is None:
        return None
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def resolve_dep_auth(
    dep_ref: Any,
    auth_resolver: Any,
) -> tuple[str | None, str, dict[str, str] | None]:
    """Resolve per-dependency authentication for use by ``git ls-remote``.

    Uses the same token and scheme the downstream clone will use. Best-effort:
    when no real token is resolved (or on any failure) the unauthenticated
    basic path remains and the downstream clone surfaces the real auth error
    with its own diagnostic. A ``bearer`` scheme is only forwarded alongside a
    non-empty token, so a token-less context never triggers a bearer request.
    """
    if auth_resolver is None:
        return None, "basic", None
    try:
        auth_ctx = auth_resolver.resolve_for_dep(dep_ref)
        if auth_ctx is None or not auth_ctx.token:
            return None, "basic", getattr(auth_ctx, "git_env", None)
        return auth_ctx.token, auth_ctx.auth_scheme, getattr(auth_ctx, "git_env", None)
    except Exception:
        return None, "basic", None


def _git_semver_package_name(dep_ref: DependencyReference) -> str:
    """Return the package name used for git tag ``{name}`` matching."""
    if dep_ref.is_virtual_subdirectory() and dep_ref.virtual_path:
        return dep_ref.virtual_path.rstrip("/").rsplit("/", 1)[-1]
    return dep_ref.repo_url.rsplit("/", 1)[-1]


def maybe_resolve_git_semver(
    *,
    dep_ref: DependencyReference,
    existing_lockfile: Any,
    update_refs: bool,
    auth_resolver: Any = None,
    ref_resolver_cache: dict[RefResolverCacheKey, Any] | None = None,
    ref_resolver_cache_lock: Any = None,
    transport_selector: Any = None,
    protocol_pref: Any = None,
) -> Any:
    """Resolve a git-source semver range or replay its locked resolution."""
    if dep_ref.is_local:
        return None
    if getattr(dep_ref, "source", None) == "registry":
        return None
    if getattr(dep_ref, "artifactory_prefix", None):
        return None
    if dep_ref.ref_kind != "semver":
        return None

    constraint = dep_ref.reference
    owner_repo = dep_ref.repo_url
    package_name = _git_semver_package_name(dep_ref)
    if not update_refs and existing_lockfile is not None:
        locked = existing_lockfile.get_dependency(dep_ref.get_unique_key())
        if (
            locked is not None
            and locked.constraint == constraint
            and locked.resolved_tag
            and locked.resolved_commit
            and locked.version
        ):
            from apm_cli.deps.git_semver_resolver import GitSemverResolution

            return GitSemverResolution(
                constraint=locked.constraint,
                resolved_version=locked.version,
                resolved_tag=locked.resolved_tag,
                resolved_sha=locked.resolved_commit,
                matched_pattern="",
                resolved_at=locked.resolved_at or "",
            )

    from apm_cli.deps.git_semver_resolver import GitSemverResolver

    token, auth_scheme, git_env = resolve_dep_auth(dep_ref, auth_resolver)
    if transport_selector is None:
        from apm_cli.deps.transport_selection import (
            NoOpInsteadOfResolver,
            TransportSelector,
        )

        transport_selector = TransportSelector(NoOpInsteadOfResolver())
    if protocol_pref is None:
        from apm_cli.deps.transport_selection import ProtocolPreference

        protocol_pref = ProtocolPreference.NONE
    transport_plan = transport_selector.select(
        dep_ref=dep_ref,
        cli_pref=protocol_pref,
        allow_fallback=False,
        has_token=bool(token),
    )
    selected_scheme = transport_plan.attempts[0].scheme
    transport_scheme = "ssh" if selected_scheme == "ssh" else "https"
    ref_resolver = get_shared_ref_resolver(
        dep_ref.host,
        token,
        ref_resolver_cache,
        ref_resolver_cache_lock,
        auth_scheme=auth_scheme,
        git_env=git_env,
        auth_resolver=auth_resolver,
        auth_target=dep_ref.host,
        transport_scheme=transport_scheme,
        ssh_user=dep_ref.ssh_user or "git",
        port=dep_ref.port,
    )
    return GitSemverResolver(ref_resolver).resolve(
        owner_repo=owner_repo,
        package_name=package_name,
        constraint=constraint,
    )


def get_shared_ref_resolver(
    host: str | None,
    token: str | None,
    cache: dict[RefResolverCacheKey, Any] | None,
    lock: Any = None,
    *,
    auth_scheme: str = "basic",
    git_env: dict[str, str] | None = None,
    auth_resolver: Any = None,
    auth_target: Any = None,
    transport_scheme: str = "https",
    ssh_user: str = "git",
    port: int | None = None,
) -> Any:
    """Return a transport-specific shared ``RefResolver`` for one auth context.

    When ``cache`` is provided, resolvers are memoized so the second and
    later deps from a repo reuse the instance (and its ref cache). The cache
    key includes normalized host, credential fingerprint, auth scheme, and the
    selected transport identity. The fingerprint is non-reversible and never
    stores the raw credential in the context object this cache lives on.
    ``host`` is normalized to ``None`` meaning "use RefResolver default
    (github.com)", so a dep written with an explicit ``host='github.com'`` and
    one with no host collapse to the same cache bucket when transport also
    matches. When ``lock`` is also provided, the get-or-create runs under it --
    required because the BFS download callback runs on a worker pool, where
    unguarded concurrent first-touches would each build a resolver and defeat
    the dedup. ``cache=None`` builds a fresh resolver per call. Token rotation
    mid-run is intentionally unsupported.
    """
    from apm_cli.marketplace.ref_resolver import RefResolver

    # Normalize the default github.com host so deps that omit host and deps
    # that spell out 'github.com' explicitly share the same cache bucket.
    _DEFAULT_HOST = "github.com"
    canonical_host = host if host and host != _DEFAULT_HOST else None

    resolver_kwargs = {
        "host": host,
        "token": token,
        "auth_scheme": auth_scheme,
    }
    if git_env is not None:
        resolver_kwargs["git_env"] = git_env
    if auth_resolver is not None:
        resolver_kwargs.update(
            auth_resolver=auth_resolver,
            auth_target=auth_target,
        )
    if transport_scheme == "ssh":
        resolver_kwargs.update(
            transport_scheme=transport_scheme,
            ssh_user=ssh_user,
        )
    if port is not None:
        resolver_kwargs["port"] = port

    if cache is None:
        return RefResolver(**resolver_kwargs)

    transport_identity = (
        transport_scheme,
        ssh_user if transport_scheme == "ssh" else None,
        port,
    )
    key = (
        canonical_host,
        _token_fingerprint(token),
        auth_scheme,
        transport_identity,
    )
    if lock is not None:
        with lock:
            resolver = cache.get(key)
            if resolver is None:
                resolver = RefResolver(**resolver_kwargs)
                cache[key] = resolver
            return resolver

    resolver = cache.get(key)
    if resolver is None:
        resolver = RefResolver(**resolver_kwargs)
        cache[key] = resolver
    return resolver


def annotate_update_plan_refs(
    deps_to_install: list[DependencyReference],
    downloader: GitHubPackageDownloader,
    *,
    update_refs: bool,
) -> list[DependencyReference]:
    """Resolve Git refs needed by the update plan through the downloader owner."""
    if not update_refs:
        return deps_to_install
    for dep_ref in deps_to_install:
        if (
            getattr(dep_ref, "resolved_reference", None) is not None
            or dep_ref.is_local
            or getattr(dep_ref, "source", None) == "registry"
            or getattr(dep_ref, "artifactory_prefix", None)
        ):
            continue
        resolved = downloader.resolve_git_reference(dep_ref)
        dep_ref.resolved_reference = resolved
    return deps_to_install
