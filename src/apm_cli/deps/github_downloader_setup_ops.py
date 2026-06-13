"""Setup / auth / raw-file ops for :class:`GitHubPackageDownloader`.

Moved bodies (kept thin wrappers on the class): constructor wiring, git-env
setup, error sanitisation, per-dependency auth resolution, and raw-file
download routing. Patched globals are routed through a function-level
``from apm_cli.deps import github_downloader as _gh`` alias so monkeypatches
on the original module still apply; no module-scope import of the original
module (avoids an import cycle).
"""

import os
import re

from ..models.apm_package import DependencyReference
from ..utils.github_host import sanitize_token_url_in_message


def setup_git_environment(downloader) -> dict:
    """Set up Git environment with authentication using centralized token manager.

    Builds the auth-bearing env via :class:`GitAuthEnvBuilder`, then records
    token-state attributes on the downloader (read by many other methods).
    """
    from apm_cli.deps import github_downloader as _gh

    from .git_auth_env import GitAuthEnvBuilder

    builder = GitAuthEnvBuilder(downloader.token_manager)
    env = builder.setup_environment()

    # IMPORTANT: Do not resolve credentials via helpers at construction time.
    # AuthResolver.resolve(...) can trigger OS credential helper UI. If we do
    # this eagerly (host-only key) and later resolve per-dependency (host+org),
    # users can see duplicate auth prompts. Keep constructor token state env-only
    # and resolve lazily per dependency during clone/validate flows.
    downloader.github_token = downloader.token_manager.get_token_for_purpose("modules", env)
    downloader.has_github_token = downloader.github_token is not None
    downloader._github_token_from_credential_fill = False

    # GitLab (env-only at init; lazy auth resolution happens per dep)
    downloader.gitlab_token = downloader.token_manager.get_token_for_purpose("gitlab_modules", env)
    downloader.has_gitlab_token = downloader.gitlab_token is not None

    # Azure DevOps (env-only at init; lazy auth resolution happens per dep)
    downloader.ado_token = downloader.token_manager.get_token_for_purpose("ado_modules", env)
    downloader.has_ado_token = downloader.ado_token is not None

    # JFrog Artifactory (not host-based, uses dedicated env var)
    downloader.artifactory_token = downloader.token_manager.get_token_for_purpose(
        "artifactory_modules", env
    )
    downloader.has_artifactory_token = downloader.artifactory_token is not None

    _gh._debug(
        f"Token setup: has_github_token={downloader.has_github_token}, "
        f"has_gitlab_token={downloader.has_gitlab_token}, "
        f"has_ado_token={downloader.has_ado_token}, "
        f"has_artifactory_token={downloader.has_artifactory_token}"
        f"{', source=credential_helper' if downloader._github_token_from_credential_fill else ''}"
    )

    return env


def sanitize_git_error(downloader, error_message: str) -> str:
    """Sanitize Git error messages to remove potentially sensitive auth information."""
    from apm_cli.deps import github_downloader as _gh

    # Remove any tokens that might appear in URLs for github hosts (https://token@host).
    sanitized = sanitize_token_url_in_message(error_message, host=_gh.default_host())

    # Sanitize Azure DevOps URLs - both cloud (dev.azure.com) and any on-prem server.
    # Generic pattern catches https://token@anyhost for all hosts.
    sanitized = re.sub(r"https://[^@\s]+@([^\s/]+)", r"https://***@\1", sanitized)

    # Remove any tokens that might appear as standalone values.
    sanitized = re.sub(
        r"(ghp_|gho_|ghu_|ghs_|ghr_|glpat[_-])[a-zA-Z0-9_\-]+",
        "***",
        sanitized,
    )

    # Remove environment variable values that might contain tokens.
    sanitized = re.sub(
        r"(GITHUB_TOKEN|GITHUB_APM_PAT|ADO_APM_PAT|GH_TOKEN|GITHUB_COPILOT_PAT|GITLAB_APM_PAT|GITLAB_TOKEN)=[^\s]+",
        r"\1=***",
        sanitized,
    )

    return sanitized


def resolve_dep_token(downloader, dep_ref: DependencyReference | None = None) -> str | None:
    """Resolve the per-dependency auth token via AuthResolver.

    GitHub, GitLab, and ADO hosts use the token resolved by AuthResolver.
    Other generic hosts return None so git credential helpers can provide
    credentials instead.
    """
    if dep_ref is None:
        return downloader.github_token

    if downloader._is_generic_dependency_host(dep_ref):
        return None

    dep_ctx = downloader.auth_resolver.resolve_for_dep(dep_ref)
    return dep_ctx.token


def resolve_dep_auth_ctx(downloader, dep_ref: DependencyReference | None = None):
    """Resolve the full AuthContext for a dependency.

    Returns the AuthContext from AuthResolver, or None for generic hosts or
    when no dep_ref is provided.
    """
    if dep_ref is None:
        return None

    dep_host = dep_ref.host
    if downloader._is_generic_dependency_host(dep_ref):
        return None

    ctx = downloader.auth_resolver.resolve_for_dep(dep_ref)
    # Verbose source surfacing (#852): one-time per-host log line so users can
    # see which credential source was actually used. Routed through
    # AuthResolver.notify_auth_source() (#856 follow-up F2).
    if os.environ.get("APM_VERBOSE") == "1":
        downloader.auth_resolver.notify_auth_source(dep_host or "", ctx)
    return ctx


def download_raw_file(
    downloader,
    dep_ref: DependencyReference,
    file_path: str,
    ref: str = "main",
    verbose_callback=None,
) -> bytes:
    """Download a single file from a repository (GitHub, GitLab, ADO, Artifactory)."""
    from apm_cli.deps import github_downloader as _gh

    _ = dep_ref.host or _gh.default_host()

    # Check if this is Artifactory (Mode 1: explicit FQDN)
    if dep_ref.is_artifactory():
        repo_parts = dep_ref.repo_url.split("/")
        return downloader._download_file_from_artifactory(
            dep_ref.host,
            dep_ref.artifactory_prefix,
            repo_parts[0],
            repo_parts[1] if len(repo_parts) > 1 else repo_parts[0],
            file_path,
            ref,
        )

    # Check if this should go through Artifactory proxy (Mode 2)
    art_proxy = downloader._parse_artifactory_base_url()
    if art_proxy and downloader._should_use_artifactory_proxy(dep_ref):
        repo_parts = dep_ref.repo_url.split("/")
        return downloader._download_file_from_artifactory(
            art_proxy[0],
            art_proxy[1],
            repo_parts[0],
            repo_parts[1] if len(repo_parts) > 1 else repo_parts[0],
            file_path,
            ref,
            scheme=art_proxy[2],
        )

    # Check if this is Azure DevOps
    if dep_ref.is_azure_devops():
        return downloader._download_ado_file(dep_ref, file_path, ref)

    # GitHub API
    return downloader._download_github_file(
        dep_ref, file_path, ref, verbose_callback=verbose_callback
    )


def init_downloader(
    downloader, auth_resolver, transport_selector, protocol_pref, allow_fallback
) -> None:
    """Wire up a freshly-constructed :class:`GitHubPackageDownloader`.

    Resolves auth/transport defaults, builds the delegate + orchestrator
    collaborators, and declares the install-pipeline-attached fields
    (shared/persistent caches, tiered resolver, install logger) so they are
    part of the documented surface rather than monkey-patched fields.
    """
    import threading

    from apm_cli.deps import github_downloader as _gh

    downloader.auth_resolver = auth_resolver or _gh.AuthResolver()
    downloader.token_manager = downloader.auth_resolver._token_manager  # Backward compat
    downloader.git_env = downloader._setup_git_environment()
    downloader._transport_selector = transport_selector or _gh.TransportSelector()
    if protocol_pref is not None:
        downloader._protocol_pref = protocol_pref
    else:
        # Config-aware helper (env > apm config > None) so ``apm config set ssh
        # true`` is honoured even when constructed without explicit args.
        from ..config import get_apm_protocol_pref as _get_pref
        from .transport_selection import ProtocolPreference

        downloader._protocol_pref = ProtocolPreference.from_str(_get_pref())
    if allow_fallback is not None:
        downloader._allow_fallback = allow_fallback
    else:
        # Config-aware helper (env > apm config > False).
        from ..config import get_apm_allow_protocol_fallback as _get_fallback

        downloader._allow_fallback = _get_fallback()
    # Dedup set for the issue #786 cross-protocol port warning: one install run
    # calls _clone_with_fallback multiple times per dep. We want the warning
    # exactly once per (host, repo, port) identity across all those calls.
    downloader._fallback_port_warned: set = set()
    downloader._fallback_port_warned_lock = threading.Lock()

    # Delegate backend-specific download logic to the download delegate.
    downloader._strategies = _gh.DownloadDelegate(host=downloader)

    # Artifactory orchestration is encapsulated in a dedicated facade backed by
    # the DownloadDelegate's HTTP archive downloader.
    from .artifactory_orchestrator import ArtifactoryOrchestrator
    from .clone_engine import CloneEngine
    from .git_reference_resolver import GitReferenceResolver

    downloader._artifactory = ArtifactoryOrchestrator(archive_downloader=downloader._strategies)
    downloader._refs = GitReferenceResolver(host=downloader)
    downloader._clone_engine = CloneEngine(host=downloader)

    # WS2a (#1116): per-run shared clone cache for subdirectory dep dedup. Set
    # by the install pipeline before resolution; None means no dedup.
    downloader.shared_clone_cache = None

    # WS3 (#1116): persistent cross-run git cache. When set, the download flow
    # checks the on-disk cache before any network clone. None disables it.
    downloader.persistent_git_cache = None

    # #1369: tiered ref resolver. Attached by resolve.py / outdated.py after
    # construction. When set, resolve_git_reference delegates to it.
    downloader._tiered_resolver = None

    # Perf #1433: optional InstallLogger attached by the install pipeline. When
    # set, the subdir download path emits structured verbose-only [perf] lines.
    downloader.install_logger = None
