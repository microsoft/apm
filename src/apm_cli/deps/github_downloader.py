"""GitHub package downloader for APM dependencies."""

import contextlib
import os
import re
import stat  # noqa: F401
import sys
import tempfile  # noqa: F401  # re-exported for tests that patch github_downloader.tempfile
import threading
import time  # noqa: F401
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Union

import git  # noqa: F401  # re-exported for tests that patch github_downloader.git
import requests
from git import Repo

from ..core.auth import AuthContext, AuthResolver
from ..models.apm_package import (
    APMPackage,
    DependencyReference,
    GitReferenceType,
    PackageInfo,
    RemoteRef,
    ResolvedReference,
)
from ..utils.console import _rich_warning  # noqa: F401  # re-exported for tests
from ..utils.github_host import (
    default_host,
    is_azure_devops_hostname,  # noqa: F401
    is_github_hostname,
    sanitize_token_url_in_message,
)
from ..utils.yaml_io import yaml_to_str
from .bare_cache import (
    bare_clone_with_fallback,
    clone_with_fallback,
    materialize_from_bare,
)
from .download_strategies import DownloadDelegate
from .git_remote_ops import (
    parse_ls_remote_output,
    semver_sort_key,
    sort_remote_refs,
)
from .github_downloader_packages import (
    _download_package_from_artifactory,
    _download_subdirectory_from_artifactory,
    _get_clone_progress_callback,
    _try_sparse_checkout,
    download_package,
    download_subdirectory_package,
)
from .transport_selection import (
    ProtocolPreference,
    TransportSelector,
    is_fallback_allowed,
    protocol_pref_from_env,
)

# Public docs anchor for the cross-protocol fallback caveat surfaced by the
# #786 warning. Lives under the dependencies guide, next to the canonical
# `--allow-protocol-fallback` section (Starlight site defined in
# docs/astro.config.mjs).
_PROTOCOL_FALLBACK_DOCS_URL = (
    "https://microsoft.github.io/apm/guides/dependencies/#restoring-the-legacy-permissive-chain"
)


def _debug(message: str) -> None:
    """Print debug message if APM_DEBUG environment variable is set."""
    if os.environ.get("APM_DEBUG"):
        print(f"[DEBUG] {message}", file=sys.stderr)


def _close_repo(repo) -> None:
    """Release GitPython handles so directories can be deleted on Windows."""
    if repo is None:
        return
    with contextlib.suppress(Exception):
        repo.git.clear_cache()
    with contextlib.suppress(Exception):
        repo.close()


def _rmtree(path) -> None:
    """Remove a directory tree, handling read-only files and brief Windows locks.

    Delegates to :func:`robust_rmtree` which retries with exponential backoff
    on transient lock errors (e.g. antivirus scanning on Windows).
    """
    from ..utils.file_ops import robust_rmtree

    robust_rmtree(path, ignore_errors=True)


class GitHubPackageDownloader:
    """Downloads and validates APM packages from GitHub repositories."""

    def __init__(
        self,
        auth_resolver=None,
        transport_selector: TransportSelector | None = None,
        protocol_pref: ProtocolPreference | None = None,
        allow_fallback: bool | None = None,
    ):
        """Initialize the GitHub package downloader.

        Args:
            auth_resolver: Auth resolver instance. Defaults to a new AuthResolver.
            transport_selector: TransportSelector for protocol decisions.
                Defaults to a new selector with GitConfigInsteadOfResolver.
            protocol_pref: User-stated transport preference for shorthand
                deps. When None, reads APM_GIT_PROTOCOL env.
            allow_fallback: When True, permits cross-protocol fallback
                (legacy behavior). When None, reads
                APM_ALLOW_PROTOCOL_FALLBACK env.
        """
        self.auth_resolver = auth_resolver or AuthResolver()
        self.token_manager = self.auth_resolver._token_manager  # Backward compat
        self.git_env = self._setup_git_environment()
        self._transport_selector = transport_selector or TransportSelector()
        self._protocol_pref = (
            protocol_pref if protocol_pref is not None else protocol_pref_from_env()
        )
        self._allow_fallback = (
            allow_fallback if allow_fallback is not None else is_fallback_allowed()
        )
        # Dedup set for the issue #786 cross-protocol port warning: one install
        # run calls _clone_with_fallback multiple times per dep (ref-resolution
        # clone, then the actual dep clone). We want the warning exactly once
        # per (host, repo, port) identity across all those calls.
        self._fallback_port_warned: set = set()
        self._fallback_port_warned_lock = threading.Lock()

        # Delegate backend-specific download logic to the download delegate.
        self._strategies = DownloadDelegate(host=self)

        # Artifactory orchestration is encapsulated in a dedicated facade
        # (download_package / download_subdirectory) backed by the
        # DownloadDelegate's HTTP archive downloader.
        from .artifactory_orchestrator import ArtifactoryOrchestrator
        from .clone_engine import CloneEngine
        from .git_reference_resolver import GitReferenceResolver

        self._artifactory = ArtifactoryOrchestrator(archive_downloader=self._strategies)
        self._refs = GitReferenceResolver(host=self)
        self._clone_engine = CloneEngine(host=self)

        # WS2a (#1116): per-run shared clone cache for subdirectory dep
        # deduplication.  Set by the install pipeline before resolution
        # starts; None means no dedup (each subdir dep clones independently).
        self.shared_clone_cache = None

        # WS3 (#1116): persistent cross-run git cache.  When set, the
        # download flow checks the on-disk cache before any network clone.
        # Set by the install pipeline; None disables persistent caching.
        self.persistent_git_cache = None

        # #1369: tiered ref resolver. Attached by resolve.py / outdated.py
        # after construction via ``build_tiered_ref_resolver``. When set,
        # :meth:`resolve_git_reference` delegates to it before falling
        # through to ``self._refs.resolve``. Declared here so the
        # attribute is part of the documented downloader surface rather
        # than a monkey-patched field.
        self._tiered_resolver = None

    def _git_env_dict(self) -> dict[str, str]:
        """Return a sanitized git env dict for cache-layer subprocess calls.

        Delegates to :class:`GitAuthEnvBuilder.subprocess_env_dict`.
        """
        from .git_auth_env import GitAuthEnvBuilder

        return GitAuthEnvBuilder.subprocess_env_dict(self.git_env)

    def _setup_git_environment(self) -> dict[str, Any]:
        """Set up Git environment with authentication using centralized token manager.

        Builds the auth-bearing env via :class:`GitAuthEnvBuilder`, then
        records token-state attributes on the downloader (these are read
        by many other methods on the class).
        """
        from .git_auth_env import GitAuthEnvBuilder

        builder = GitAuthEnvBuilder(self.token_manager)
        env = builder.setup_environment()

        # IMPORTANT: Do not resolve credentials via helpers at construction time.
        # AuthResolver.resolve(...) can trigger OS credential helper UI. If we do
        # this eagerly (host-only key) and later resolve per-dependency (host+org),
        # users can see duplicate auth prompts. Keep constructor token state env-only
        # and resolve lazily per dependency during clone/validate flows.
        self.github_token = self.token_manager.get_token_for_purpose("modules", env)
        self.has_github_token = self.github_token is not None
        self._github_token_from_credential_fill = False

        # GitLab (env-only at init; lazy auth resolution happens per dep)
        self.gitlab_token = self.token_manager.get_token_for_purpose("gitlab_modules", env)
        self.has_gitlab_token = self.gitlab_token is not None

        # Azure DevOps (env-only at init; lazy auth resolution happens per dep)
        self.ado_token = self.token_manager.get_token_for_purpose("ado_modules", env)
        self.has_ado_token = self.ado_token is not None

        # JFrog Artifactory (not host-based, uses dedicated env var)
        self.artifactory_token = self.token_manager.get_token_for_purpose(
            "artifactory_modules", env
        )
        self.has_artifactory_token = self.artifactory_token is not None

        _debug(
            f"Token setup: has_github_token={self.has_github_token}, "
            f"has_gitlab_token={self.has_gitlab_token}, "
            f"has_ado_token={self.has_ado_token}, "
            f"has_artifactory_token={self.has_artifactory_token}"
            f"{', source=credential_helper' if self._github_token_from_credential_fill else ''}"
        )

        return env

    # --- Registry proxy support ---

    @property
    def registry_config(self):
        """Lazily-constructed :class:`~apm_cli.deps.registry_proxy.RegistryConfig`.

        Returns ``None`` when no registry proxy is configured.
        """
        if not hasattr(self, "_registry_config_cache"):
            from .registry_proxy import RegistryConfig

            self._registry_config_cache = RegistryConfig.from_env()
        return self._registry_config_cache

    # --- Artifactory VCS archive download support ---

    def _get_artifactory_headers(self) -> dict[str, str]:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.get_artifactory_headers()

    def _download_artifactory_archive(
        self,
        host: str,
        prefix: str,
        owner: str,
        repo: str,
        ref: str,
        target_path: Path,
        scheme: str = "https",
    ) -> None:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.download_artifactory_archive(
            host,
            prefix,
            owner,
            repo,
            ref,
            target_path,
            scheme=scheme,
        )

    def _download_file_from_artifactory(
        self,
        host: str,
        prefix: str,
        owner: str,
        repo: str,
        file_path: str,
        ref: str,
        scheme: str = "https",
    ) -> bytes:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.download_file_from_artifactory(
            host,
            prefix,
            owner,
            repo,
            file_path,
            ref,
            scheme=scheme,
        )

    @staticmethod
    def _is_artifactory_only() -> bool:
        """Backward-compat stub -- delegates to ArtifactoryRouter."""
        from .artifactory_orchestrator import ArtifactoryRouter

        return ArtifactoryRouter.is_registry_only()

    def _should_use_artifactory_proxy(self, dep_ref: "DependencyReference") -> bool:
        """Backward-compat stub -- delegates to ArtifactoryRouter."""
        from .artifactory_orchestrator import ArtifactoryRouter

        return ArtifactoryRouter.should_use_proxy(dep_ref)

    def _is_generic_dependency_host(self, dep_ref: DependencyReference | None) -> bool:
        """Return True for hosts where git credential helpers own auth."""
        if dep_ref is None or dep_ref.is_azure_devops():
            return False
        dep_host = dep_ref.host
        if not dep_host or is_github_hostname(dep_host):
            return False
        return self.auth_resolver.classify_host(dep_host, port=dep_ref.port).kind != "gitlab"

    def _parse_artifactory_base_url(self) -> tuple | None:
        """Backward-compat stub -- delegates to ArtifactoryRouter."""
        from .artifactory_orchestrator import ArtifactoryRouter

        return ArtifactoryRouter.parse_proxy_config()

    def _resolve_dep_token(self, dep_ref: DependencyReference | None = None) -> str | None:
        """Resolve the per-dependency auth token via AuthResolver.

        GitHub, GitLab, and ADO hosts use the token resolved by AuthResolver.
        Other generic hosts return None so git credential helpers can provide
        credentials instead.

        Args:
            dep_ref: Optional dependency reference for host/org lookup.

        Returns:
            Token string or None.
        """
        if dep_ref is None:
            return self.github_token

        if self._is_generic_dependency_host(dep_ref):
            return None

        dep_ctx = self.auth_resolver.resolve_for_dep(dep_ref)
        return dep_ctx.token

    def _resolve_dep_auth_ctx(
        self, dep_ref: DependencyReference | None = None
    ) -> AuthContext | None:
        """Resolve the full AuthContext for a dependency.

        Returns the AuthContext from AuthResolver, or None for generic hosts
        or when no dep_ref is provided.
        """
        if dep_ref is None:
            return None

        dep_host = dep_ref.host
        if self._is_generic_dependency_host(dep_ref):
            return None

        ctx = self.auth_resolver.resolve_for_dep(dep_ref)
        # Verbose source surfacing (#852): one-time per-host log line so users
        # can see which credential source was actually used. Routed through
        # AuthResolver.notify_auth_source() (#856 follow-up F2) so the line
        # obeys the same verbose-channel logic as every other diagnostic.
        if os.environ.get("APM_VERBOSE") == "1":
            self.auth_resolver.notify_auth_source(dep_host or "", ctx)
        return ctx

    def _build_noninteractive_git_env(
        self,
        *,
        preserve_config_isolation: bool = False,
        suppress_credential_helpers: bool = False,
    ) -> dict[str, str]:
        """Return a non-interactive git env for unauthenticated git operations.

        Delegates to :class:`GitAuthEnvBuilder.noninteractive_env`.
        """
        from .git_auth_env import GitAuthEnvBuilder

        return GitAuthEnvBuilder.noninteractive_env(
            self.git_env,
            preserve_config_isolation=preserve_config_isolation,
            suppress_credential_helpers=suppress_credential_helpers,
        )

    def _resilient_get(
        self, url: str, headers: dict[str, str], timeout: int = 30, max_retries: int = 3
    ) -> requests.Response:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.resilient_get(
            url, headers, timeout=timeout, max_retries=max_retries
        )

    def _sanitize_git_error(self, error_message: str) -> str:
        """Sanitize Git error messages to remove potentially sensitive authentication information.

        Args:
            error_message: Raw error message from Git operations

        Returns:
            str: Sanitized error message with sensitive data removed
        """
        import re

        # Remove any tokens that might appear in URLs for github hosts (format: https://token@host)
        # Sanitize for default host and common enterprise hosts via helper
        sanitized = sanitize_token_url_in_message(error_message, host=default_host())

        # Sanitize Azure DevOps URLs - both cloud (dev.azure.com) and any on-prem server
        # Use a generic pattern to catch https://token@anyhost format for all hosts
        # This catches: dev.azure.com, ado.company.com, tfs.internal.corp, etc.
        sanitized = re.sub(r"https://[^@\s]+@([^\s/]+)", r"https://***@\1", sanitized)

        # Remove any tokens that might appear as standalone values
        sanitized = re.sub(
            r"(ghp_|gho_|ghu_|ghs_|ghr_|glpat[_-])[a-zA-Z0-9_\-]+",
            "***",
            sanitized,
        )

        # Remove environment variable values that might contain tokens
        sanitized = re.sub(
            r"(GITHUB_TOKEN|GITHUB_APM_PAT|ADO_APM_PAT|GH_TOKEN|GITHUB_COPILOT_PAT|GITLAB_APM_PAT|GITLAB_TOKEN)=[^\s]+",
            r"\1=***",
            sanitized,
        )

        return sanitized

    def _build_repo_url(
        self,
        repo_ref: str,
        use_ssh: bool = False,
        dep_ref: DependencyReference = None,
        token: str | None = None,
        auth_scheme: str = "basic",
    ) -> str:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.build_repo_url(
            repo_ref,
            use_ssh=use_ssh,
            dep_ref=dep_ref,
            token=token,
            auth_scheme=auth_scheme,
        )

    def _clone_with_fallback(
        self,
        repo_url_base: str,
        target_path: Path,
        progress_reporter=None,
        dep_ref: DependencyReference = None,
        verbose_callback=None,
        **clone_kwargs,
    ) -> Repo:
        """Thin delegate to :func:`bare_cache.clone_with_fallback` (kept on the class so test patches still work)."""
        return clone_with_fallback(
            self._execute_transport_plan,
            repo_url_base,
            target_path,
            progress_reporter=progress_reporter,
            dep_ref=dep_ref,
            verbose_callback=verbose_callback,
            repo_cls=Repo,
            **clone_kwargs,
        )

    def _execute_transport_plan(
        self,
        repo_url_base: str,
        target_path: Path,
        *,
        dep_ref: DependencyReference | None = None,
        clone_action: Callable[[str, dict[str, str], Path], None],
        verbose_callback=None,
    ) -> None:
        """Execute a clone action against a TransportPlan with full fallback.

        Delegates to :class:`CloneEngine`. Stub kept on the downloader so
        existing test patches that target this method on the class still
        work.
        """
        return self._get_clone_engine().execute(
            repo_url_base,
            target_path,
            dep_ref=dep_ref,
            clone_action=clone_action,
            verbose_callback=verbose_callback,
        )

    def _get_clone_engine(self):
        """Return the CloneEngine, lazily constructing it if needed.

        Lazy construction matters for tests that build a downloader via
        ``GitHubPackageDownloader.__new__(...)`` and skip ``__init__``;
        they only set the attributes the engine actually reads.
        """
        engine = getattr(self, "_clone_engine", None)
        if engine is None:
            from .clone_engine import CloneEngine

            engine = CloneEngine(host=self)
            self._clone_engine = engine
        return engine

    # ------------------------------------------------------------------
    # Bare-clone helpers (#1126: subdir-agnostic shared cache)
    # ------------------------------------------------------------------

    def _bare_clone_with_fallback(
        self,
        repo_url_base: str,
        bare_target: Path,
        *,
        dep_ref: DependencyReference,
        ref: str | None,
        is_commit_sha: bool,
    ) -> None:
        """Thin delegate to :func:`bare_cache.bare_clone_with_fallback` (kept on the class so test patches still work)."""
        bare_clone_with_fallback(
            self._execute_transport_plan,
            repo_url_base,
            bare_target,
            dep_ref=dep_ref,
            ref=ref,
            is_commit_sha=is_commit_sha,
        )

    def _materialize_from_bare(
        self,
        bare_path: Path,
        consumer_dir: Path,
        *,
        ref: str | None,
        env: dict[str, str],
        known_sha: str | None = None,
    ) -> str:
        """Thin delegate to :func:`bare_cache.materialize_from_bare` (kept on the class so test patches still work)."""
        return materialize_from_bare(bare_path, consumer_dir, ref=ref, env=env, known_sha=known_sha)

    @staticmethod
    def _parse_ls_remote_output(output: str) -> list[RemoteRef]:
        """Backward-compat stub -- delegates to git_remote_ops."""
        return parse_ls_remote_output(output)

    @staticmethod
    def _semver_sort_key(name: str):
        """Backward-compat stub -- delegates to git_remote_ops."""
        return semver_sort_key(name)

    @classmethod
    def _sort_remote_refs(cls, refs: list[RemoteRef]) -> list[RemoteRef]:
        """Backward-compat stub -- delegates to git_remote_ops."""
        return sort_remote_refs(refs)

    def list_remote_refs(self, dep_ref: DependencyReference) -> list[RemoteRef]:
        """Enumerate remote tags and branches without cloning.

        Delegates to :class:`GitReferenceResolver`. Stub kept on the
        downloader for backward compatibility with callers/tests that
        access this method directly.
        """
        return self._refs.list_remote_refs(dep_ref)

    def resolve_git_reference(
        self, repo_ref: Union[str, "DependencyReference"]
    ) -> ResolvedReference:
        """Resolve a Git reference (branch/tag/commit) to a specific commit SHA.

        Delegates to :class:`TieredRefResolver` when one is attached
        (per-run, by the install resolve phase or outdated command) for
        the #1369 fast-path; falls through to the legacy
        :class:`GitReferenceResolver` otherwise.
        """
        tiered = getattr(self, "_tiered_resolver", None)
        if tiered is not None:
            return tiered.resolve(repo_ref)
        return self._refs.resolve(repo_ref)

    def _resolve_commit_sha_for_ref(self, dep_ref: DependencyReference, ref: str) -> str | None:
        """Resolve a Git ref to its 40-char commit SHA via the cheap commits API.

        Delegates to :class:`GitReferenceResolver`. Stub kept on the
        downloader for backward compatibility with internal callers.
        """
        return self._refs.resolve_commit_sha_for_ref(dep_ref, ref)

    def download_raw_file(
        self, dep_ref: DependencyReference, file_path: str, ref: str = "main", verbose_callback=None
    ) -> bytes:
        """Download a single file from repository (GitHub or Azure DevOps).

        Args:
            dep_ref: Parsed dependency reference
            file_path: Path to file within the repository (e.g., "prompts/code-review.prompt.md")
            ref: Git reference (branch, tag, or commit SHA). Defaults to "main"
            verbose_callback: Optional callable for verbose logging (receives str messages)

        Returns:
            bytes: File content

        Raises:
            RuntimeError: If download fails or file not found
        """
        _ = dep_ref.host or default_host()

        # Check if this is Artifactory (Mode 1: explicit FQDN)
        if dep_ref.is_artifactory():
            repo_parts = dep_ref.repo_url.split("/")
            return self._download_file_from_artifactory(
                dep_ref.host,
                dep_ref.artifactory_prefix,
                repo_parts[0],
                repo_parts[1] if len(repo_parts) > 1 else repo_parts[0],
                file_path,
                ref,
            )

        # Check if this should go through Artifactory proxy (Mode 2)
        art_proxy = self._parse_artifactory_base_url()
        if art_proxy and self._should_use_artifactory_proxy(dep_ref):
            repo_parts = dep_ref.repo_url.split("/")
            return self._download_file_from_artifactory(
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
            return self._download_ado_file(dep_ref, file_path, ref)

        # GitHub API
        return self._download_github_file(
            dep_ref, file_path, ref, verbose_callback=verbose_callback
        )

    def _download_ado_file(
        self, dep_ref: DependencyReference, file_path: str, ref: str = "main"
    ) -> bytes:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.download_ado_file(dep_ref, file_path, ref=ref)

    def _try_raw_download(self, owner: str, repo: str, ref: str, file_path: str) -> bytes | None:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.try_raw_download(owner, repo, ref, file_path)

    def _download_gitlab_file(
        self,
        dep_ref: DependencyReference,
        file_path: str,
        ref: str = "main",
        verbose_callback=None,
    ) -> bytes:
        """Backward-compat stub -- delegates to backend-specific strategies."""
        return self._strategies.download_gitlab_file(
            dep_ref, file_path, ref=ref, verbose_callback=verbose_callback
        )

    def _download_github_file(
        self,
        dep_ref: DependencyReference,
        file_path: str,
        ref: str = "main",
        verbose_callback=None,
    ) -> bytes:
        """Backward-compat stub -- delegates to backend-specific strategies."""
        host = dep_ref.host or default_host()
        if self.auth_resolver.classify_host(host).kind == "gitlab":
            return self._download_gitlab_file(
                dep_ref, file_path, ref, verbose_callback=verbose_callback
            )
        return self._strategies.download_github_file(
            dep_ref,
            file_path,
            ref=ref,
            verbose_callback=verbose_callback,
        )

    def validate_virtual_package_exists(
        self,
        dep_ref: DependencyReference,
        verbose_callback: Callable[[str], None] | None = None,
        warn_callback: Callable[[str], None] | None = None,
    ) -> bool:
        """Validate that a virtual package exists at ``dep_ref``.

        Thin delegation to :func:`github_downloader_validation.validate_virtual_package_exists`
        -- see that module for the full validation strategy (marker-file
        probes, Contents API directory probe, ``git ls-remote`` fallback).
        """
        from .github_downloader_validation import validate_virtual_package_exists as _v

        return _v(
            self,
            dep_ref,
            verbose_callback=verbose_callback,
            warn_callback=warn_callback,
        )

    def _directory_exists_at_ref(
        self,
        dep_ref: DependencyReference,
        path: str,
        ref: str,
        log: Callable[[str], None],
    ) -> bool:
        """Backward-compat shim -- delegates to the validation module."""
        from .github_downloader_validation import _directory_exists_at_ref as _impl

        return _impl(self, dep_ref, path, ref, log)

    def _ref_exists_via_ls_remote(
        self,
        dep_ref: DependencyReference,
        ref: str,
        log: Callable[[str], None],
    ) -> bool:
        """Backward-compat shim -- delegates to the validation module.

        Returns ``bool`` (success only); the underlying impl now also
        returns the winning AttemptSpec, but legacy callers only need
        the success flag.
        """
        from .github_downloader_validation import _ref_exists_via_ls_remote as _impl

        ok, _winning = _impl(self, dep_ref, ref, log)
        return ok

    def _ssh_attempt_allowed(self) -> bool:
        """Backward-compat shim -- delegates to the validation module."""
        from .github_downloader_validation import _ssh_attempt_allowed as _impl

        return _impl(self)

    def download_virtual_file_package(
        self,
        dep_ref: DependencyReference,
        target_path: Path,
        progress_task_id=None,
        progress_obj=None,
    ) -> PackageInfo:
        """Download a single file as a virtual APM package.

        Creates a minimal APM package structure with the file placed in the appropriate
        .apm/ subdirectory based on its extension.

        Args:
            dep_ref: Dependency reference with virtual_path set
            target_path: Local path where virtual package should be created
            progress_task_id: Rich Progress task ID for progress updates
            progress_obj: Rich Progress object for progress updates

        Returns:
            PackageInfo: Information about the created virtual package

        Raises:
            ValueError: If the dependency is not a valid virtual file package
            RuntimeError: If download fails
        """
        if not dep_ref.is_virtual or not dep_ref.virtual_path:
            raise ValueError("Dependency must be a virtual file package")

        if not dep_ref.is_virtual_file():
            raise ValueError(
                f"Path '{dep_ref.virtual_path}' is not a valid individual file. "
                f"Must end with one of: {', '.join(DependencyReference.VIRTUAL_FILE_EXTENSIONS)}"
            )

        # Determine the ref to use
        ref = dep_ref.reference or "main"

        # Resolve the commit SHA cheaply BEFORE the file download. This is one
        # short HTTP call (Accept: application/vnd.github.sha returns just the
        # 40-char SHA in the body) and the result is propagated into PackageInfo
        # so the lockfile and per-dep header can render the SHA suffix instead
        # of just the ref name. On non-GitHub hosts or any failure this returns
        # None and we fall back to ref-name only -- the install never fails on
        # SHA resolution.
        resolved_commit = self._resolve_commit_sha_for_ref(dep_ref, ref)

        # Update progress - downloading
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=50, total=100)

        # Download the file content
        try:
            file_content = self.download_raw_file(dep_ref, dep_ref.virtual_path, ref)
        except RuntimeError as e:
            raise RuntimeError(f"Failed to download virtual package: {e}") from e

        # Update progress - processing
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=90, total=100)

        # Create target directory structure
        target_path.mkdir(parents=True, exist_ok=True)

        # Determine the subdirectory based on file extension
        subdirs = {
            ".prompt.md": "prompts",
            ".instructions.md": "instructions",
            ".chatmode.md": "chatmodes",
            ".agent.md": "agents",
        }

        subdir = None
        filename = dep_ref.virtual_path.split("/")[-1]
        for ext, dir_name in subdirs.items():
            if dep_ref.virtual_path.endswith(ext):
                subdir = dir_name
                break

        if not subdir:
            raise ValueError(f"Unknown file extension for {dep_ref.virtual_path}")

        # Create .apm structure
        apm_dir = target_path / ".apm" / subdir
        apm_dir.mkdir(parents=True, exist_ok=True)

        # Write the file
        file_path = apm_dir / filename
        file_path.write_bytes(file_content)

        # Generate minimal apm.yml
        package_name = dep_ref.get_virtual_package_name()

        # Try to extract description from file frontmatter
        description = f"Virtual package containing {filename}"
        try:
            content_str = file_content.decode("utf-8")
            # Simple frontmatter parsing (YAML between --- markers)
            if content_str.startswith("---\n"):
                end_idx = content_str.find("\n---\n", 4)
                if end_idx > 0:
                    frontmatter = content_str[4:end_idx]
                    # Look for description field
                    for line in frontmatter.split("\n"):
                        if line.startswith("description:"):
                            description = line.split(":", 1)[1].strip().strip("\"'")
                            break
        except Exception:
            # If frontmatter parsing fails, use default description
            pass

        apm_yml_data = {
            "name": package_name,
            "version": "1.0.0",
            "description": description,
            "author": dep_ref.repo_url.split("/")[0],
        }
        apm_yml_content = yaml_to_str(apm_yml_data)

        apm_yml_path = target_path / "apm.yml"
        apm_yml_path.write_text(apm_yml_content, encoding="utf-8")

        # Create APMPackage object
        package = APMPackage(
            name=package_name,
            version="1.0.0",
            description=description,
            author=dep_ref.repo_url.split("/")[0],
            source=dep_ref.to_github_url(),
            package_path=target_path,
        )

        # Build the resolved reference. On non-GitHub hosts or SHA-resolve
        # failure the resolved_commit stays None and the suffix renders as
        # "#ref" only -- matching the existing subdirectory behavior in
        # _try_sparse_checkout / _download_subdirectory.
        ref_type = (
            GitReferenceType.COMMIT
            if re.match(r"^[a-f0-9]{40}$", ref.lower())
            else GitReferenceType.BRANCH
        )
        resolved_ref = ResolvedReference(
            original_ref=str(dep_ref.reference) if dep_ref.reference else ref,
            ref_name=ref,
            ref_type=ref_type,
            resolved_commit=resolved_commit,
        )

        # Return PackageInfo
        return PackageInfo(
            package=package,
            install_path=target_path,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,  # Store for canonical dependency string
            resolved_reference=resolved_ref,
        )

    _try_sparse_checkout = _try_sparse_checkout
    download_subdirectory_package = download_subdirectory_package
    _download_subdirectory_from_artifactory = _download_subdirectory_from_artifactory
    _download_package_from_artifactory = _download_package_from_artifactory
    download_package = download_package
    _get_clone_progress_callback = _get_clone_progress_callback
