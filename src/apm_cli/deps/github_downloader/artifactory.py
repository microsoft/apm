# pylint: disable=duplicate-code
"""GitHub package downloader for APM dependencies."""

import contextlib
import os
import sys
from pathlib import Path

import requests

from ...models.apm_package import (
    DependencyReference,
    PackageInfo,
)
from ...utils.github_host import (
    is_github_hostname,
)
from ..download_strategies.artifactory_strategy import _ArtifactoryTarget

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


class _ArtifactoryMixin:
    def _get_artifactory_headers(self) -> dict[str, str]:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.get_artifactory_headers()

    def _download_artifactory_archive(
        self,
        target: "_ArtifactoryTarget",
        target_path: Path,
    ) -> None:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.download_artifactory_archive(target, target_path)

    def _download_file_from_artifactory(
        self,
        target: "_ArtifactoryTarget",
        file_path: str,
    ) -> bytes:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.download_file_from_artifactory(target, file_path)

    @staticmethod
    def _is_artifactory_only() -> bool:
        """Backward-compat stub -- delegates to ArtifactoryRouter."""
        from ..artifactory_orchestrator import ArtifactoryRouter

        return ArtifactoryRouter.is_registry_only()

    def _should_use_artifactory_proxy(self, dep_ref: "DependencyReference") -> bool:
        """Backward-compat stub -- delegates to ArtifactoryRouter."""
        from ..artifactory_orchestrator import ArtifactoryRouter

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
        from ..artifactory_orchestrator import ArtifactoryRouter

        return ArtifactoryRouter.parse_proxy_config()

    def _resilient_get(
        self, url: str, headers: dict[str, str], timeout: int = 30, max_retries: int = 3
    ) -> requests.Response:
        """Backward-compat stub -- delegates to download strategies."""
        return self._strategies.resilient_get(
            url, headers, timeout=timeout, max_retries=max_retries
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
