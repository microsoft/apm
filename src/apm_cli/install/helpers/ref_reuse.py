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
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.deps.github_downloader import GitHubPackageDownloader
    from apm_cli.deps.transport_selection import ProtocolPreference, TransportSelector
    from apm_cli.models.dependency.reference import DependencyReference

RefResolverCacheKey = tuple[
    str | None,
    str | None,
    str,
    tuple[str, str | None, int | None, bool],
]
_UNRESOLVED_AUTH_CONTEXT = object()


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
    *,
    remote_url: str | None = None,
    unauth_first: bool = False,
    resolved_context: Any = _UNRESOLVED_AUTH_CONTEXT,
    build_git_env: bool = True,
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
        if unauth_first:
            return (
                None,
                "basic",
                (auth_resolver.build_public_github_anonymous_git_env() if build_git_env else None),
            )
        auth_ctx = (
            auth_resolver.resolve_for_dep(dep_ref)
            if resolved_context is _UNRESOLVED_AUTH_CONTEXT
            else resolved_context
        )
        if auth_ctx is None:
            return None, "basic", None
        remote_env_builder = getattr(auth_resolver, "git_env_for_remote", None)
        if remote_url is not None and callable(remote_env_builder):
            from apm_cli.core.host_providers import git_transport_policy

            host_kind = getattr(getattr(auth_ctx, "host_info", None), "kind", None)
            if not isinstance(host_kind, str):
                classify = getattr(auth_resolver, "classify_host", None)
                host_kind = (
                    classify(
                        dep_ref.host or "github.com",
                        port=dep_ref.port,
                        host_type=dep_ref.host_type,
                    ).kind
                    if callable(classify)
                    else "github"
                )
            policy = git_transport_policy(host_kind, remote_url)
            token = auth_ctx.token if policy.use_resolved_credentials else None
            git_env = remote_env_builder(auth_ctx, remote_url) if build_git_env else None
        else:
            harden = getattr(auth_resolver, "hardened_git_env_for_context", None)
            git_env = (
                (harden(auth_ctx) if callable(harden) else getattr(auth_ctx, "git_env", None))
                if build_git_env
                else None
            )
            token = auth_ctx.token
        if not token:
            return None, "basic", git_env
        return (
            token,
            auth_ctx.auth_scheme,
            git_env,
        )
    except Exception:
        return None, "basic", None


def _git_semver_package_name(dep_ref: DependencyReference) -> str:
    """Return the package name used for git tag ``{name}`` matching."""
    if dep_ref.is_virtual_subdirectory() and dep_ref.virtual_path:
        return dep_ref.virtual_path.rstrip("/").rsplit("/", 1)[-1]
    return dep_ref.repo_url.rsplit("/", 1)[-1]


def is_git_semver_resolution_eligible(dep_ref: DependencyReference) -> bool:
    """Return whether a dependency must resolve its semver constraint from Git tags.

    Registry routing is decided before this helper is called by positional CLI
    ingress. This helper owns the remaining source and reference checks shared
    by that ingress and the resolve phase.
    """
    return (
        not dep_ref.is_local
        and getattr(dep_ref, "source", None) != "registry"
        and not getattr(dep_ref, "artifactory_prefix", None)
        and dep_ref.ref_kind == "semver"
    )


def maybe_resolve_git_semver(
    *,
    dep_ref: DependencyReference,
    existing_lockfile: Any,
    update_refs: bool,
    auth_resolver: Any = None,
    ref_resolver_cache: dict[RefResolverCacheKey, Any] | None = None,
    ref_resolver_cache_lock: Any = None,
    transport_selector: TransportSelector | None = None,
    protocol_pref: ProtocolPreference | None = None,
) -> Any:
    """Resolve a git-source semver range or replay its locked resolution."""
    if not is_git_semver_resolution_eligible(dep_ref):
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

    if transport_selector is None:
        from apm_cli.deps.transport_selection import (
            NoOpInsteadOfResolver,
            TransportSelector,
        )

        transport_selector = TransportSelector(NoOpInsteadOfResolver())
    if protocol_pref is None:
        from apm_cli.deps.transport_selection import ProtocolPreference

        protocol_pref = ProtocolPreference.NONE
    explicit_scheme = (getattr(dep_ref, "explicit_scheme", None) or "").lower()
    candidate_uses_ssh = explicit_scheme == "ssh" or (
        not explicit_scheme and getattr(protocol_pref, "value", None) == "ssh"
    )
    if candidate_uses_ssh:
        if dep_ref.is_azure_devops():
            from apm_cli.utils.github_host import build_ado_ssh_url

            dep_ref.validate_provider_coordinates()
            rewrite_candidate = build_ado_ssh_url(
                dep_ref.ado_organization,
                dep_ref.ado_project,
                dep_ref.ado_repo,
            )
        else:
            from apm_cli.utils.github_host import build_ssh_url

            rewrite_candidate = build_ssh_url(
                dep_ref.host or "github.com",
                dep_ref.repo_url,
                port=dep_ref.port,
                user=dep_ref.ssh_user or "git",
            )
    else:
        rewrite_candidate = dep_ref.to_github_url()
    if not dep_ref.is_azure_devops() and not rewrite_candidate.endswith(".git"):
        rewrite_candidate = f"{rewrite_candidate}.git"
    anonymous_selector = (
        getattr(auth_resolver, "uses_public_github_anonymous_first", None)
        if auth_resolver is not None
        else None
    )
    anonymous_first = bool(
        not dep_ref.is_insecure
        and callable(anonymous_selector)
        and anonymous_selector(
            dep_ref.host or "github.com",
            port=dep_ref.port,
            host_type=dep_ref.host_type,
        )
        is True
    )
    resolved_context: Any = None
    if auth_resolver is not None and not anonymous_first:
        try:
            resolved_context = auth_resolver.resolve_for_dep(dep_ref)
        except Exception:
            resolved_context = None
    token, _, _ = resolve_dep_auth(
        dep_ref,
        auth_resolver,
        remote_url=rewrite_candidate,
        unauth_first=anonymous_first,
        resolved_context=resolved_context,
        build_git_env=False,
    )
    transport_plan = transport_selector.select(
        dep_ref=dep_ref,
        cli_pref=protocol_pref,
        allow_fallback=False,
        has_token=bool(token),
        candidate_url=rewrite_candidate,
    )
    selected_attempt = transport_plan.attempts[0]
    requested_url = selected_attempt.requested_url
    if requested_url is not None:
        from urllib.parse import urlsplit

        requested_scheme = urlsplit(requested_url).scheme.lower()
        requested_uses_ssh = requested_scheme == "ssh" or ("@" in requested_url.split(":", 1)[0])
        transport_scheme = "ssh" if requested_uses_ssh else "https"
    else:
        transport_scheme = selected_attempt.scheme
    policy_url = selected_attempt.effective_url or rewrite_candidate
    resolver_token, auth_scheme, _resolver_git_env = resolve_dep_auth(
        dep_ref,
        auth_resolver,
        remote_url=policy_url,
        unauth_first=anonymous_first,
        resolved_context=resolved_context,
        build_git_env=False,
    )
    if not selected_attempt.use_token:
        resolver_token = None

    def resolver_git_env_factory() -> dict[str, str] | None:
        """Build the remote environment only when the shared resolver is new."""
        return resolve_dep_auth(
            dep_ref,
            auth_resolver,
            remote_url=policy_url,
            unauth_first=anonymous_first,
            resolved_context=resolved_context,
        )[2]

    ref_resolver = get_shared_ref_resolver(
        dep_ref.host,
        resolver_token,
        ref_resolver_cache,
        ref_resolver_cache_lock,
        auth_scheme=auth_scheme,
        git_env_factory=resolver_git_env_factory,
        auth_resolver=auth_resolver,
        auth_target=dep_ref.host,
        transport_scheme=transport_scheme,
        ssh_user=dep_ref.ssh_user or "git",
        port=dep_ref.port,
        unauth_first=anonymous_first,
    )
    remote_url = (
        requested_url
        if selected_attempt.requested_url is not None
        else (
            dep_ref.to_github_url()
            if transport_scheme == "https" and dep_ref.is_azure_devops()
            else None
        )
    )
    return GitSemverResolver(ref_resolver).resolve(
        owner_repo=owner_repo,
        package_name=package_name,
        constraint=constraint,
        remote_url=remote_url,
    )


def get_shared_ref_resolver(
    host: str | None,
    token: str | None,
    cache: dict[RefResolverCacheKey, Any] | None,
    lock: Any = None,
    *,
    auth_scheme: str = "basic",
    git_env: dict[str, str] | None = None,
    git_env_factory: Callable[[], dict[str, str] | None] | None = None,
    auth_resolver: Any = None,
    auth_target: Any = None,
    transport_scheme: str = "https",
    ssh_user: str = "git",
    port: int | None = None,
    unauth_first: bool = False,
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

    if git_env is not None and git_env_factory is not None:
        raise ValueError("git_env and git_env_factory are mutually exclusive")

    def build_resolver() -> Any:
        resolver_kwargs = {
            "host": host,
            "token": token,
            "auth_scheme": auth_scheme,
        }
        resolved_git_env = git_env_factory() if git_env_factory is not None else git_env
        if resolved_git_env is not None:
            resolver_kwargs["git_env"] = resolved_git_env
        if auth_resolver is not None:
            resolver_kwargs.update(
                auth_resolver=auth_resolver,
                auth_target=auth_target,
            )
        if unauth_first:
            resolver_kwargs["unauth_first"] = True
        if transport_scheme == "ssh":
            resolver_kwargs.update(
                transport_scheme=transport_scheme,
                ssh_user=ssh_user,
            )
        if port is not None:
            resolver_kwargs["port"] = port
        return RefResolver(**resolver_kwargs)

    if cache is None:
        return build_resolver()

    transport_identity = (
        transport_scheme,
        ssh_user if transport_scheme == "ssh" else None,
        port,
        unauth_first,
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
                resolver = build_resolver()
                cache[key] = resolver
            return resolver

    resolver = cache.get(key)
    if resolver is None:
        resolver = build_resolver()
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
