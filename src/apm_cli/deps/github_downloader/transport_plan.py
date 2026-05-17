"""GitHub package downloader for APM dependencies."""

import contextlib
import os
import re
import stat  # noqa: F401
import subprocess
import sys
import tempfile
import threading
import time  # noqa: F401
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Union

import git  # noqa: F401  # re-exported for tests that patch github_downloader.git
import requests
from git import RemoteProgress, Repo
from git.exc import GitCommandError

from ...core.auth import AuthContext, AuthResolver
from ...models.apm_package import (
    APMPackage,
    DependencyReference,
    GitReferenceType,
    PackageInfo,
    PackageType,
    RemoteRef,
    ResolvedReference,
    validate_apm_package,
)
from ...utils.console import _rich_warning  # noqa: F401  # re-exported for tests
from ...utils.github_host import (
    default_host,
    is_azure_devops_hostname,  # noqa: F401
    is_github_hostname,
    sanitize_token_url_in_message,
)
from ...utils.yaml_io import yaml_to_str
from ..bare_cache import (
    bare_clone_with_fallback,
    clone_with_fallback,
    fetch_sha_into_bare,
    materialize_from_bare,
)
from ..download_strategies import DownloadDelegate
from ..git_remote_ops import (
    parse_ls_remote_output,
    semver_sort_key,
    sort_remote_refs,
)
from ..transport_selection import (
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
    from ...utils.file_ops import robust_rmtree

    robust_rmtree(path, ignore_errors=True)


class _TransportPlanMixin:
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
            repo_cls=sys.modules[__package__].Repo,
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
            from ..clone_engine import CloneEngine

            engine = CloneEngine(host=self)
            self._clone_engine = engine
        return engine

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
        from ..github_downloader_validation import validate_virtual_package_exists as _v

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
        from ..github_downloader_validation import _directory_exists_at_ref as _impl

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
        from ..github_downloader_validation import _ref_exists_via_ls_remote as _impl

        ok, _winning = _impl(self, dep_ref, ref, log)
        return ok

    def _ssh_attempt_allowed(self) -> bool:
        """Backward-compat shim -- delegates to the validation module."""
        from ..github_downloader_validation import _ssh_attempt_allowed as _impl

        return _impl(self)
