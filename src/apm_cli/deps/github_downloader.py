"""GitHub package downloader for APM dependencies."""

import contextlib
import os

# subprocess / tempfile are re-exported (tests patch them on this module) even
# though their only direct users now live in github_downloader_ops, which
# routes back through ``_gh.<name>`` so the patches still apply.
import subprocess as subprocess
import sys
import tempfile as tempfile
import time as time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Union

import git  # noqa: F401  -- re-exported; tests patch apm_cli.deps.github_downloader.git
import requests
from git import RemoteProgress, Repo

from ..core.auth import AuthContext
from ..core.auth import AuthResolver as AuthResolver
from ..models.apm_package import APMPackage as APMPackage
from ..models.apm_package import (
    DependencyReference,
    PackageInfo,
    RemoteRef,
    ResolvedReference,
)
from ..models.apm_package import (
    validate_apm_package as validate_apm_package,
)
from ..utils.console import (
    _rich_warning as _rich_warning,
)
from ..utils.github_host import (
    default_host,
    is_github_hostname,
)
from ..utils.yaml_io import yaml_to_str as yaml_to_str
from .bare_cache import (
    bare_clone_with_fallback,
    clone_with_fallback,
    fetch_sha_into_bare,
    materialize_from_bare,
)
from .download_strategies import DownloadDelegate as DownloadDelegate
from .git_remote_ops import (
    parse_ls_remote_output,
    semver_sort_key,
    sort_remote_refs,
)
from .transport_selection import (
    ProtocolPreference,
    TransportSelector,
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


def _dir_size_bytes(path: Path) -> int:
    """Return the total on-disk size of a directory tree in bytes.

    Best-effort: silently skips files that disappear or cannot be
    stat-ed mid-walk (e.g. transient .git lock files). Uses ``lstat``
    so symlinks contribute the size of the link itself, never the
    target -- this keeps the measurement bounded to the directory
    tree and matches :func:`apm_cli.cache.git_cache._dir_size`. Used
    only for verbose-mode perf diagnostics (#1433) -- never gates
    behavior.
    """
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(str(path)):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                try:
                    total += os.lstat(fpath).st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


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
        """Initialize the GitHub package downloader (wiring delegated)."""
        from .github_downloader_setup_ops import init_downloader as _impl

        _impl(self, auth_resolver, transport_selector, protocol_pref, allow_fallback)

    def _git_env_dict(self) -> dict[str, str]:
        """Return a sanitized git env dict for cache-layer subprocess calls.

        Delegates to :class:`GitAuthEnvBuilder.subprocess_env_dict`.
        """
        from .git_auth_env import GitAuthEnvBuilder

        return GitAuthEnvBuilder.subprocess_env_dict(self.git_env)

    def _setup_git_environment(self) -> dict[str, Any]:
        """Set up Git environment with authentication (delegated)."""
        from .github_downloader_setup_ops import setup_git_environment as _impl

        return _impl(self)

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
        return (
            self.auth_resolver.classify_host(
                dep_host,
                port=dep_ref.port,
                host_type=dep_ref.host_type,
            ).kind
            != "gitlab"
        )

    def _parse_artifactory_base_url(self) -> tuple | None:
        """Backward-compat stub -- delegates to ArtifactoryRouter."""
        from .artifactory_orchestrator import ArtifactoryRouter

        return ArtifactoryRouter.parse_proxy_config()

    def _resolve_dep_token(self, dep_ref: DependencyReference | None = None) -> str | None:
        """Resolve the per-dependency auth token via AuthResolver (delegated)."""
        from .github_downloader_setup_ops import resolve_dep_token as _impl

        return _impl(self, dep_ref)

    def _resolve_dep_auth_ctx(
        self, dep_ref: DependencyReference | None = None
    ) -> AuthContext | None:
        """Resolve the full AuthContext for a dependency (delegated)."""
        from .github_downloader_setup_ops import resolve_dep_auth_ctx as _impl

        return _impl(self, dep_ref)

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
        """Sanitize Git error messages to remove sensitive auth info (delegated)."""
        from .github_downloader_setup_ops import sanitize_git_error as _impl

        return _impl(self, error_message)

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
        sparse_paths: list[str] | None = None,
    ) -> str:
        """Thin delegate to :func:`bare_cache.materialize_from_bare` (kept on the class so test patches still work)."""
        return materialize_from_bare(
            bare_path,
            consumer_dir,
            ref=ref,
            env=env,
            known_sha=known_sha,
            sparse_paths=sparse_paths,
        )

    def _fetch_sha_into_bare(
        self,
        bare_path: Path,
        sha: str,
        *,
        dep_ref: "DependencyReference",
    ) -> bool:
        """Thin delegate to :func:`bare_cache.fetch_sha_into_bare` (kept on the class so test patches still work)."""
        return fetch_sha_into_bare(
            self._execute_transport_plan,
            dep_ref.repo_url,
            bare_path,
            sha,
            dep_ref=dep_ref,
        )

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

    def list_remote_tag_refs(self, dep_ref: DependencyReference) -> list[RemoteRef]:
        """Enumerate remote tags only without cloning."""
        return self._refs.list_remote_tag_refs(dep_ref)

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
        """Download a single file from a repository (delegated)."""
        from .github_downloader_setup_ops import download_raw_file as _impl

        return _impl(self, dep_ref, file_path, ref, verbose_callback)

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
        if (
            self.auth_resolver.classify_host(
                host,
                port=dep_ref.port,
                host_type=dep_ref.host_type,
            ).kind
            == "gitlab"
        ):
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
        """Download a single file as a virtual APM package (delegated)."""
        from .github_downloader_package_ops import download_virtual_file_package as _impl

        return _impl(self, dep_ref, target_path, progress_task_id, progress_obj)

    def _try_sparse_checkout(
        self,
        dep_ref: DependencyReference,
        temp_clone_path: Path,
        subdir_path: str,
        ref: str | None = None,
    ) -> bool:
        """Attempt sparse-checkout to download only a subdirectory (delegated)."""
        from .github_downloader_package_ops import try_sparse_checkout as _impl

        return _impl(self, dep_ref, temp_clone_path, subdir_path, ref)

    def download_subdirectory_package(
        self,
        dep_ref: DependencyReference,
        target_path: Path,
        progress_task_id=None,
        progress_obj=None,
    ) -> PackageInfo:
        """Download a subdirectory from a repo as an APM package (delegated)."""
        from .github_downloader_subdir_ops import download_subdirectory_package as _impl

        return _impl(self, dep_ref, target_path, progress_task_id, progress_obj)

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
        """Download a GitHub repository and validate it as an APM package (delegated)."""
        from .github_downloader_package_ops import download_package as _impl

        return _impl(self, repo_ref, target_path, progress_task_id, progress_obj, verbose_callback)

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
