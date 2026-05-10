"""GitHub package downloader for APM dependencies."""

import contextlib
import os
import re
import stat  # noqa: F401
import subprocess
import sys
import tempfile
import time  # noqa: F401
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Union

import git  # noqa: F401  # re-exported for tests that patch github_downloader.git
import requests
from git import RemoteProgress, Repo
from git.exc import GitCommandError

from ..core.auth import AuthContext, AuthResolver
from ..models.apm_package import (
    APMPackage,
    DependencyReference,
    GitReferenceType,
    PackageInfo,
    PackageType,
    RemoteRef,
    ResolvedReference,
    validate_apm_package,
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


class GitProgressReporter(RemoteProgress):
    """Report git clone progress to Rich Progress."""

    def __init__(self, progress_task_id=None, progress_obj=None, package_name=None):
        super().__init__()
        self.task_id = progress_task_id
        self.progress = progress_obj
        self.package_name = package_name  # Keep consistent name throughout download
        self.last_op = None
        self.disabled = False  # Flag to stop updates after download completes

    def update(self, op_code, cur_count, max_count=None, message=""):
        """Called by GitPython during clone operations."""
        if not self.progress or self.task_id is None or self.disabled:
            return

        # Keep the package name consistent - don't change description to git operations
        # This keeps the UI clean and scannable

        # Update progress bar naturally - let it reach 100%
        if max_count and max_count > 0:
            # Determinate progress (we have total count)
            self.progress.update(
                self.task_id,
                completed=cur_count,
                total=max_count,
                # Note: We don't update description - keep the original package name
            )
        else:
            # Indeterminate progress (just show activity)
            self.progress.update(
                self.task_id,
                total=100,  # Set fake total for indeterminate tasks
                completed=min(cur_count, 100) if cur_count else 0,
                # Note: We don't update description - keep the original package name
            )

        self.last_op = cur_count

    def _get_op_name(self, op_code):
        """Convert git operation code to human-readable name."""
        from git import RemoteProgress

        # Extract operation type from op_code
        if op_code & RemoteProgress.COUNTING:
            return "Counting objects"
        elif op_code & RemoteProgress.COMPRESSING:
            return "Compressing objects"
        elif op_code & RemoteProgress.WRITING:
            return "Writing objects"
        elif op_code & RemoteProgress.RECEIVING:
            return "Receiving objects"
        elif op_code & RemoteProgress.RESOLVING:
            return "Resolving deltas"
        elif op_code & RemoteProgress.FINDING_SOURCES:
            return "Finding sources"
        elif op_code & RemoteProgress.CHECKING_OUT:
            return "Checking out files"
        else:
            return "Cloning"


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

        Delegates to :class:`GitReferenceResolver`.
        """
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

    def _try_sparse_checkout(
        self,
        dep_ref: DependencyReference,
        temp_clone_path: Path,
        subdir_path: str,
        ref: str | None = None,
    ) -> bool:
        """Attempt sparse-checkout to download only a subdirectory (git 2.25+).

        Returns True on success. Falls back silently on failure.
        """

        try:
            temp_clone_path.mkdir(parents=True, exist_ok=True)

            # Resolve per-dependency token via AuthResolver.
            dep_token = self._resolve_dep_token(dep_ref)
            dep_auth_ctx = self._resolve_dep_auth_ctx(dep_ref)
            dep_auth_scheme = dep_auth_ctx.auth_scheme if dep_auth_ctx else "basic"

            # For ADO bearer, use the AuthContext git_env with header injection
            if dep_auth_scheme == "bearer" and dep_auth_ctx is not None:
                env = {**os.environ, **(dep_auth_ctx.git_env or {})}
            else:
                env = {**os.environ, **(self.git_env or {})}
            auth_url = self._build_repo_url(
                dep_ref.repo_url,
                use_ssh=False,
                dep_ref=dep_ref,
                token=dep_token,
                auth_scheme=dep_auth_scheme,
            )

            cmds = [
                ["git", "init"],
                ["git", "remote", "add", "origin", auth_url],
                ["git", "sparse-checkout", "init", "--cone"],
                ["git", "sparse-checkout", "set", subdir_path],
            ]
            fetch_cmd = ["git", "fetch", "origin"]
            fetch_cmd.append(ref or "HEAD")
            fetch_cmd.append("--depth=1")
            cmds.append(fetch_cmd)
            cmds.append(["git", "checkout", "FETCH_HEAD"])

            for cmd in cmds:
                result = subprocess.run(
                    cmd,
                    cwd=str(temp_clone_path),
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=120,
                )
                if result.returncode != 0:
                    _debug(
                        f"Sparse-checkout step failed ({' '.join(cmd)}): {result.stderr.strip()}"
                    )
                    return False

            return True
        except Exception as e:
            _debug(f"Sparse-checkout failed: {e}")
            return False

    def download_subdirectory_package(
        self,
        dep_ref: DependencyReference,
        target_path: Path,
        progress_task_id=None,
        progress_obj=None,
    ) -> PackageInfo:
        """Download a subdirectory from a repo as an APM package.

        Used for Claude Skills or APM packages nested in monorepos.
        Clones the repo, extracts the subdirectory, and cleans up.

        Args:
            dep_ref: Dependency reference with virtual_path set to subdirectory
            target_path: Local path where package should be created
            progress_task_id: Rich Progress task ID for progress updates
            progress_obj: Rich Progress object for progress updates

        Returns:
            PackageInfo: Information about the downloaded package

        Raises:
            ValueError: If the dependency is not a valid subdirectory package
            RuntimeError: If download or validation fails
        """
        if not dep_ref.is_virtual or not dep_ref.virtual_path:
            raise ValueError("Dependency must be a virtual subdirectory package")

        if not dep_ref.is_virtual_subdirectory():
            raise ValueError(f"Path '{dep_ref.virtual_path}' is not a valid subdirectory package")

        # Use user-specified ref, or None to use repo's default branch
        ref = dep_ref.reference  # None if not specified
        subdir_path = dep_ref.virtual_path

        # Update progress - starting
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=10, total=100)

        # WS2a (#1116): attempt shared clone dedup when a per-run cache
        # is available.  Two subdir deps from the same (host, owner, repo, ref)
        # share one clone; different refs always get independent clones.
        shared_cache = self.shared_clone_cache
        use_shared = shared_cache is not None
        # Determine cache key components from the dep_ref.
        cache_host = dep_ref.host or default_host()
        cache_owner = dep_ref.repo_url.split("/")[0] if "/" in dep_ref.repo_url else ""
        cache_repo = dep_ref.repo_url.split("/")[1] if "/" in dep_ref.repo_url else dep_ref.repo_url

        # WS3 (#1116): try persistent cross-run cache first.
        # Build a canonical URL for cache key derivation.
        _persistent_cache = self.persistent_git_cache
        _persistent_checkout: Path | None = None
        if _persistent_cache is not None:
            _canonical_url = f"https://{cache_host}/{cache_owner}/{cache_repo}"
            try:
                _persistent_checkout = _persistent_cache.get_checkout(
                    _canonical_url, ref, env=self._git_env_dict()
                )
            except Exception:
                # Cache miss or failure -- fall through to normal clone path.
                _persistent_checkout = None

        # Use mkdtemp + explicit cleanup so we control when rmtree runs.
        # tempfile.TemporaryDirectory().__exit__ calls shutil.rmtree without our
        # retry logic, which raises WinError 32 when git processes still hold
        # handles at the end of the with-block.
        from ..config import get_apm_temp_dir

        temp_dir = None
        shared_bare_path: Path | None = None
        # WS2 path resolves the SHA from the BARE so we don't pay
        # rev-parse twice (or open the working-tree Repo unnecessarily).
        # See design.md sec 5.5: _ws2_resolved_commit threads the SHA past
        # the generic Repo(temp_clone_path).head.commit.hexsha block below.
        _ws2_resolved_commit: str | None = None
        try:
            if _persistent_checkout is not None:
                # WS3: persistent cache hit -- use the cached checkout directly.
                temp_clone_path = _persistent_checkout
            elif use_shared:
                # WS2 (#1126): shared cache holds BARE clones keyed by
                # (host, owner, repo, ref). Each consumer materializes its
                # own working tree from the bare; this is subdir-agnostic
                # so two parallel consumers requesting different
                # subdirectories of the same repo+ref can share one bare
                # without racing on sparse-checkout. See design.md sec 5.5.
                is_commit_sha = ref and re.match(r"^[a-f0-9]{7,40}$", ref) is not None

                def _shared_bare_clone_fn(bare_target: Path) -> None:
                    self._bare_clone_with_fallback(
                        dep_ref.repo_url,
                        bare_target,
                        dep_ref=dep_ref,
                        ref=ref,
                        is_commit_sha=bool(is_commit_sha),
                    )

                try:
                    shared_bare_path = shared_cache.get_or_clone(
                        cache_host, cache_owner, cache_repo, ref, _shared_bare_clone_fn
                    )
                except Exception as e:
                    raise RuntimeError(f"Failed to clone repository: {e}") from e

                # Per-consumer materialization. mkdtemp gives a unique
                # path so concurrent consumers do not collide. The bare
                # is read-only after this point; only the consumer dir
                # is written to.
                temp_dir = tempfile.mkdtemp(dir=get_apm_temp_dir())
                temp_clone_path = Path(temp_dir) / "consumer"
                try:
                    _ws2_resolved_commit = self._materialize_from_bare(
                        shared_bare_path,
                        temp_clone_path,
                        ref=ref,
                        env=self._git_env_dict(),
                        # Only short-circuit SHA resolution when the user
                        # pinned a full 40-char SHA. Abbreviated SHAs
                        # (7-39 chars) must be resolved to the full
                        # SHA against the bare so resolved_commit
                        # matches `head.commit.hexsha` (always 40-char)
                        # in lockfile comparisons. The bare's HEAD has
                        # already been update-ref'd to the full SHA in
                        # _bare_action, so rev-parse HEAD returns 40 chars.
                        # Copilot review finding (#1135).
                        known_sha=ref if (is_commit_sha and len(ref) == 40) else None,
                    )
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to prepare dependency from cached clone: {e}"
                    ) from e
            else:
                # Legacy per-dep clone path (no shared cache).
                temp_dir = tempfile.mkdtemp(dir=get_apm_temp_dir())
                # Sparse checkout always targets "repo/".  If it fails we clone into
                # "repo_clone/" so we never have to rmtree a directory that may still
                # have live git handles from the failed subprocess.
                sparse_clone_path = Path(temp_dir) / "repo"
                temp_clone_path = sparse_clone_path

                # Update progress - cloning
                if progress_obj and progress_task_id is not None:
                    progress_obj.update(progress_task_id, completed=20, total=100)

                # Phase 4 (#171): Try sparse-checkout first (git 2.25+), fall back to full clone
                sparse_ok = self._try_sparse_checkout(dep_ref, sparse_clone_path, subdir_path, ref)

                if not sparse_ok:
                    # Full clone into a fresh subdirectory so we don't have to touch
                    # the (possibly locked) sparse-checkout directory at all.
                    temp_clone_path = Path(temp_dir) / "repo_clone"

                    package_display_name = subdir_path.split("/")[-1]
                    progress_reporter = (
                        GitProgressReporter(progress_task_id, progress_obj, package_display_name)
                        if progress_task_id and progress_obj
                        else None
                    )

                    # Detect if ref is a commit SHA (can't be used with --branch in shallow clones)
                    is_commit_sha = ref and re.match(r"^[a-f0-9]{7,40}$", ref) is not None

                    clone_kwargs = {
                        "dep_ref": dep_ref,
                    }
                    if is_commit_sha:
                        # For commit SHAs, clone without checkout then checkout the specific commit.
                        # Shallow clone doesn't support fetching by arbitrary SHA.
                        clone_kwargs["no_checkout"] = True
                    else:
                        clone_kwargs["depth"] = 1
                        if ref:
                            clone_kwargs["branch"] = ref

                    try:
                        self._clone_with_fallback(
                            dep_ref.repo_url,
                            temp_clone_path,
                            progress_reporter=progress_reporter,
                            **clone_kwargs,
                        )
                    except Exception as e:
                        raise RuntimeError(f"Failed to clone repository: {e}") from e

                    if is_commit_sha:
                        repo_obj = None
                        try:
                            repo_obj = Repo(temp_clone_path)
                            repo_obj.git.checkout(ref)
                        except Exception as e:
                            raise RuntimeError(f"Failed to checkout commit {ref}: {e}") from e
                        finally:
                            _close_repo(repo_obj)

                    # Disable progress reporter after clone
                    if progress_reporter:
                        progress_reporter.disabled = True

            # Update progress - extracting subdirectory
            if progress_obj and progress_task_id is not None:
                progress_obj.update(progress_task_id, completed=70, total=100)

            # Check if subdirectory exists
            source_subdir = temp_clone_path / subdir_path
            # Security: ensure subdirectory resolves within the cloned repo
            from ..utils.path_security import ensure_path_within

            ensure_path_within(source_subdir, temp_clone_path)
            if not source_subdir.exists():
                raise RuntimeError(f"Subdirectory '{subdir_path}' not found in repository")

            if not source_subdir.is_dir():
                raise RuntimeError(f"Path '{subdir_path}' is not a directory")

            # Create target directory
            target_path.mkdir(parents=True, exist_ok=True)

            # If target exists and has content, remove it
            if target_path.exists() and any(target_path.iterdir()):
                _rmtree(target_path)
                target_path.mkdir(parents=True, exist_ok=True)

            # Copy subdirectory contents to target (retry on transient
            # file-lock errors caused by antivirus scanning on Windows).
            from ..utils.file_ops import robust_copy2, robust_copytree

            for item in source_subdir.iterdir():
                src = source_subdir / item.name
                dst = target_path / item.name
                if src.is_dir():
                    robust_copytree(src, dst)
                else:
                    robust_copy2(src, dst)

            # Capture commit SHA; close the Repo object immediately so its file
            # handles are released before _rmtree() runs in the finally block.
            # WS2 path skips this because _materialize_from_bare already
            # resolved the SHA from the bare (avoids opening Repo on the
            # consumer dir, which leaks a Windows file handle that would
            # block the rmtree below; see design.md sec 5.5).
            if _ws2_resolved_commit is not None:
                resolved_commit = _ws2_resolved_commit
            else:
                repo = None
                try:
                    repo = Repo(temp_clone_path)
                    resolved_commit = repo.head.commit.hexsha
                except Exception:
                    resolved_commit = "unknown"
                finally:
                    _close_repo(repo)

            # Update progress - validating
            if progress_obj and progress_task_id is not None:
                progress_obj.update(progress_task_id, completed=90, total=100)

        except PermissionError as exc:
            exc_path = getattr(exc, "filename", None)
            # If temp_dir wasn't created (mkdtemp failed) or the error is within
            # the temp tree, this is likely a restricted temp directory issue.
            if temp_dir is None or (exc_path and str(exc_path).startswith(str(temp_dir))):
                raise RuntimeError(
                    "Access denied in temporary directory"
                    + (f" '{temp_dir}'" if temp_dir else "")
                    + ". Corporate security may restrict this path. "
                    "Fix: apm config set temp-dir <WRITABLE_PATH>"
                ) from None
            raise
        except OSError as exc:
            if getattr(exc, "errno", None) == 13 or getattr(exc, "winerror", None) == 5:
                exc_path = getattr(exc, "filename", None)
                if temp_dir is None or (exc_path and str(exc_path).startswith(str(temp_dir))):
                    raise RuntimeError(
                        "Access denied in temporary directory"
                        + (f" '{temp_dir}'" if temp_dir else "")
                        + ". Corporate security may restrict this path. "
                        "Fix: apm config set temp-dir <WRITABLE_PATH>"
                    ) from None
            raise
        finally:
            if temp_dir:
                _rmtree(temp_dir)

        # Validate the extracted package (after temp dir is cleaned up)
        validation_result = validate_apm_package(target_path)
        if not validation_result.is_valid:
            error_msgs = "; ".join(validation_result.errors)
            raise RuntimeError(
                f"Subdirectory is not a valid APM package or Claude Skill: {error_msgs}"
            )

        # Get the resolved reference for metadata
        resolved_ref = ResolvedReference(
            original_ref=ref or "default",
            ref_name=ref or "default",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=resolved_commit,
        )

        # For plugins without an explicit version, stamp with the short commit SHA.
        package = validation_result.package
        from .package_validator import stamp_plugin_version

        stamp_plugin_version(
            package,
            validation_result.package_type,
            resolved_commit,
            target_path,
        )

        # Update progress - complete
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=100, total=100)

        return PackageInfo(
            package=package,
            install_path=target_path,
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,
            package_type=validation_result.package_type,
        )

    def _download_subdirectory_from_artifactory(
        self,
        dep_ref: "DependencyReference",
        target_path: Path,
        proxy_info: tuple,
        progress_task_id=None,
        progress_obj=None,
    ) -> PackageInfo:
        """Backward-compat stub -- delegates to ArtifactoryOrchestrator."""
        return self._artifactory.download_subdirectory(
            dep_ref,
            target_path,
            proxy_info,
            progress_task_id=progress_task_id,
            progress_obj=progress_obj,
        )

    def _download_package_from_artifactory(
        self,
        dep_ref: "DependencyReference",
        target_path: Path,
        proxy_info: tuple | None = None,
        progress_task_id=None,
        progress_obj=None,
    ) -> PackageInfo:
        """Backward-compat stub -- delegates to ArtifactoryOrchestrator."""
        return self._artifactory.download_package(
            dep_ref,
            target_path,
            proxy_info=proxy_info,
            progress_task_id=progress_task_id,
            progress_obj=progress_obj,
        )

    def download_package(
        self,
        repo_ref: Union[str, "DependencyReference"],
        target_path: Path,
        progress_task_id=None,
        progress_obj=None,
        verbose_callback=None,
    ) -> PackageInfo:
        """Download a GitHub repository and validate it as an APM package.

        For virtual packages (individual files or collections), creates a minimal
        package structure instead of cloning the full repository.

        Args:
            repo_ref: Repository reference — either a DependencyReference object
                or a string (e.g., "user/repo#branch"). Passing the object
                directly avoids a lossy parse round-trip for generic git hosts.
            target_path: Local path where package should be downloaded
            progress_task_id: Rich Progress task ID for progress updates
            progress_obj: Rich Progress object for progress updates
            verbose_callback: Optional callable for verbose logging (receives str messages)

        Returns:
            PackageInfo: Information about the downloaded package

        Raises:
            ValueError: If the repository reference is invalid
            RuntimeError: If download or validation fails
        """
        # Accept both string and DependencyReference to avoid lossy round-trips
        if isinstance(repo_ref, DependencyReference):
            dep_ref = repo_ref
        else:
            try:
                dep_ref = DependencyReference.parse(repo_ref)
            except ValueError as e:
                raise ValueError(f"Invalid repository reference '{repo_ref}': {e}") from e

        # Handle virtual packages differently
        if dep_ref.is_virtual:
            art_proxy = self._parse_artifactory_base_url()
            if self._is_artifactory_only() and not dep_ref.is_artifactory() and not art_proxy:
                raise RuntimeError(
                    f"PROXY_REGISTRY_ONLY is set but no Artifactory proxy is configured for '{repo_ref}'. "
                    "Set PROXY_REGISTRY_URL or use explicit Artifactory FQDN syntax."
                )
            if dep_ref.is_virtual_file():
                return self.download_virtual_file_package(
                    dep_ref, target_path, progress_task_id, progress_obj
                )
            # SUBDIRECTORY (the only other virtual type after #1094 dropped
            # the `.collection.yml` form): includes Artifactory modes.
            if dep_ref.is_artifactory():
                proxy_info = (dep_ref.host, dep_ref.artifactory_prefix, "https")
                return self._download_subdirectory_from_artifactory(
                    dep_ref, target_path, proxy_info, progress_task_id, progress_obj
                )
            if self._is_artifactory_only() and art_proxy:
                return self._download_subdirectory_from_artifactory(
                    dep_ref, target_path, art_proxy, progress_task_id, progress_obj
                )
            return self.download_subdirectory_package(
                dep_ref, target_path, progress_task_id, progress_obj
            )

        # Artifactory download path (Mode 1: explicit FQDN, Mode 2: transparent proxy)
        use_artifactory = dep_ref.is_artifactory()
        art_proxy = None
        if not use_artifactory:
            art_proxy = self._parse_artifactory_base_url()
            if art_proxy and self._should_use_artifactory_proxy(dep_ref):
                use_artifactory = True

        if use_artifactory:
            return self._download_package_from_artifactory(
                dep_ref, target_path, art_proxy, progress_task_id, progress_obj
            )

        # When PROXY_REGISTRY_ONLY is set but no Artifactory proxy matched, block direct git
        if self._is_artifactory_only():
            raise RuntimeError(
                f"PROXY_REGISTRY_ONLY is set but no Artifactory proxy is configured for '{dep_ref}'. "
                "Set PROXY_REGISTRY_URL or use explicit Artifactory FQDN syntax."
            )

        # Regular package download (existing logic)
        resolved_ref = self.resolve_git_reference(dep_ref)

        # Create target directory if it doesn't exist
        target_path.mkdir(parents=True, exist_ok=True)

        # If directory already exists and has content, remove it
        if target_path.exists() and any(target_path.iterdir()):
            _rmtree(target_path)
            target_path.mkdir(parents=True, exist_ok=True)

        # WS3 (#1116): persistent cross-run cache fast path for whole-repo
        # deps.  When a cached checkout exists for the resolved SHA, copy
        # files directly into target_path and skip the network clone.
        _persistent_cache = self.persistent_git_cache
        if _persistent_cache is not None:
            try:
                cache_host = dep_ref.host or default_host()
                cache_owner = dep_ref.repo_url.split("/")[0] if "/" in dep_ref.repo_url else ""
                cache_repo = (
                    dep_ref.repo_url.split("/")[1] if "/" in dep_ref.repo_url else dep_ref.repo_url
                )
                _canonical_url = f"https://{cache_host}/{cache_owner}/{cache_repo}"
                _cached = _persistent_cache.get_checkout(
                    _canonical_url,
                    resolved_ref.resolved_commit or resolved_ref.ref_name,
                    locked_sha=resolved_ref.resolved_commit,
                    env=self._git_env_dict(),
                )
                from ..utils.file_ops import robust_copy2, robust_copytree

                for item in _cached.iterdir():
                    if item.name == ".git":
                        continue
                    src = _cached / item.name
                    dst = target_path / item.name
                    if src.is_dir():
                        robust_copytree(src, dst)
                    else:
                        robust_copy2(src, dst)

                # Validate, then return without cloning.
                validation_result = validate_apm_package(target_path)
                if validation_result.is_valid and validation_result.package:
                    package = validation_result.package
                    package.source = dep_ref.to_github_url()
                    package.resolved_commit = resolved_ref.resolved_commit
                    if (
                        validation_result.package_type == PackageType.MARKETPLACE_PLUGIN
                        and package.version == "0.0.0"
                        and resolved_ref.resolved_commit
                    ):
                        short_sha = resolved_ref.resolved_commit[:7]
                        package.version = short_sha
                        apm_yml_path = target_path / "apm.yml"
                        if apm_yml_path.exists():
                            from ..utils.yaml_io import dump_yaml, load_yaml

                            _data = load_yaml(apm_yml_path) or {}
                            _data["version"] = short_sha
                            dump_yaml(_data, apm_yml_path)
                    return PackageInfo(
                        package=package,
                        install_path=target_path,
                        resolved_reference=resolved_ref,
                        installed_at=datetime.now().isoformat(),
                        dependency_ref=dep_ref,
                        package_type=validation_result.package_type,
                    )
                # Validation failed against cached copy: fall through to a
                # fresh clone (cache may be stale or repo structure changed).
                if target_path.exists() and any(target_path.iterdir()):
                    _rmtree(target_path)
                    target_path.mkdir(parents=True, exist_ok=True)
            except Exception:
                # Any cache failure -> fall back to network clone.
                if target_path.exists() and any(target_path.iterdir()):
                    _rmtree(target_path)
                    target_path.mkdir(parents=True, exist_ok=True)

        # Store progress reporter so we can disable it after clone
        progress_reporter = None
        package_display_name = (
            dep_ref.repo_url.split("/")[-1] if "/" in dep_ref.repo_url else dep_ref.repo_url
        )

        try:
            # Clone the repository using fallback authentication methods
            # Use shallow clone for performance if we have a specific commit
            if resolved_ref.ref_type == GitReferenceType.COMMIT:
                # For commits, we need to clone and checkout the specific commit
                progress_reporter = (
                    GitProgressReporter(progress_task_id, progress_obj, package_display_name)
                    if progress_task_id and progress_obj
                    else None
                )
                repo = self._clone_with_fallback(
                    dep_ref.repo_url,
                    target_path,
                    progress_reporter=progress_reporter,
                    dep_ref=dep_ref,
                    verbose_callback=verbose_callback,
                )
                repo.git.checkout(resolved_ref.resolved_commit)
            else:
                # For branches and tags, we can use shallow clone
                progress_reporter = (
                    GitProgressReporter(progress_task_id, progress_obj, package_display_name)
                    if progress_task_id and progress_obj
                    else None
                )
                repo = self._clone_with_fallback(
                    dep_ref.repo_url,
                    target_path,
                    progress_reporter=progress_reporter,
                    dep_ref=dep_ref,
                    verbose_callback=verbose_callback,
                    depth=1,
                    branch=resolved_ref.ref_name,
                )

            # Disable progress reporter to prevent late git updates
            if progress_reporter:
                progress_reporter.disabled = True

            # Remove .git directory to save space and prevent treating as a Git repository
            git_dir = target_path / ".git"
            if git_dir.exists():
                _rmtree(git_dir)

        except GitCommandError as e:
            # Check if this might be a private repository access issue
            if "Authentication failed" in str(e) or "remote: Repository not found" in str(e):
                error_msg = f"Failed to clone repository {dep_ref.repo_url}. "
                host = dep_ref.host or default_host()
                org = dep_ref.repo_url.split("/")[0] if dep_ref.repo_url else None
                error_msg += self.auth_resolver.build_error_context(
                    host,
                    "clone",
                    org=org,
                    port=dep_ref.port,
                    dep_url=dep_ref.repo_url,
                )
                raise RuntimeError(error_msg) from e
            else:
                sanitized_error = self._sanitize_git_error(str(e))
                raise RuntimeError(
                    f"Failed to clone repository {dep_ref.repo_url}: {sanitized_error}"
                ) from e
        except RuntimeError:
            # Re-raise RuntimeError from _clone_with_fallback
            raise

        # Validate the downloaded package
        validation_result = validate_apm_package(target_path)
        if not validation_result.is_valid:
            # Clean up on validation failure
            if target_path.exists():
                _rmtree(target_path)

            error_msg = f"Invalid APM package {dep_ref.repo_url}:\n"
            for error in validation_result.errors:
                error_msg += f"  - {error}\n"
            raise RuntimeError(error_msg.strip())

        # Load the APM package metadata
        if not validation_result.package:
            raise RuntimeError(
                f"Package validation succeeded but no package metadata found for {dep_ref.repo_url}"
            )

        package = validation_result.package
        package.source = dep_ref.to_github_url()
        package.resolved_commit = resolved_ref.resolved_commit

        # For plugins without an explicit version, use the short commit SHA so the
        # lock file and conflict detection have a meaningful, stable version string.
        from .package_validator import stamp_plugin_version

        stamp_plugin_version(
            package,
            validation_result.package_type,
            resolved_ref.resolved_commit,
            target_path,
        )

        # Create and return PackageInfo
        return PackageInfo(
            package=package,
            install_path=target_path,
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,  # Store for canonical dependency string
            package_type=validation_result.package_type,  # Track if APM, Claude Skill, or Hybrid
        )

    def _get_clone_progress_callback(self):
        """Get a progress callback for Git clone operations.

        Returns:
            Callable that can be used as progress callback for GitPython
        """

        def progress_callback(op_code, cur_count, max_count=None, message=""):
            """Progress callback for Git operations."""
            if max_count:
                percentage = int((cur_count / max_count) * 100)
                print(
                    f"\r Cloning: {percentage}% ({cur_count}/{max_count}) {message}",
                    end="",
                    flush=True,
                )
            else:
                print(f"\r Cloning: {message} ({cur_count})", end="", flush=True)

        return progress_callback
