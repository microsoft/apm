"""Backend-specific download delegates for APM packages.

Encapsulates HTTP resilient-get, GitHub API file download, Azure DevOps
file download, and Artifactory archive download logic.  The owning
:class:`~apm_cli.deps.github_downloader.GitHubPackageDownloader` creates
a single :class:`DownloadDelegate` instance and delegates download
operations to it (Facade/Delegate pattern).
"""

import os
import sys
from pathlib import Path

import requests

from ...models.apm_package import DependencyReference

# ---------------------------------------------------------------------------
# Module-level debug helper (mirrors the one in github_downloader so that
# this module has no import dependency on the orchestrator).
# ---------------------------------------------------------------------------


def _debug(message: str) -> None:
    """Print debug message if APM_DEBUG environment variable is set."""
    if os.environ.get("APM_DEBUG"):
        print(f"[DEBUG] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# DownloadDelegate
# ---------------------------------------------------------------------------


class DownloadDelegate:
    """Facade/Delegate that encapsulates backend-specific download logic.

    Holds the real implementations of HTTP resilient-get, URL building,
    and file download methods for GitHub, Azure DevOps, and Artifactory
    backends.

    A back-reference to the owning ``GitHubPackageDownloader`` (*host*)
    is kept as a known trade-off: it creates a circular reference
    between the delegate and its owner, but avoids duplicating shared
    state (``auth_resolver``, tokens, ``registry_config``) and
    preserves existing test ``patch.object`` points on the orchestrator.
    """

    def __init__(self, host):
        """Initialize with a reference to the owning downloader.

        Args:
            host: The :class:`GitHubPackageDownloader` instance that owns
                this delegate.
        """
        self._host = host

    # ------------------------------------------------------------------
    # HTTP resilient GET
    # ------------------------------------------------------------------

    def resilient_get(
        self, url: str, headers: dict[str, str], timeout: int = 30, max_retries: int = 3
    ) -> requests.Response:
        return _strategy_base.resilient_get(self, url, headers, timeout, max_retries)

    # ------------------------------------------------------------------
    # Repository URL building
    # ------------------------------------------------------------------

    def build_repo_url(
        self,
        repo_ref: str,
        use_ssh: bool = False,
        dep_ref: DependencyReference = None,
        token: str | None = None,
        auth_scheme: str = "basic",
    ) -> str:
        conf = _strategy_base._CloneConf(use_ssh=use_ssh, auth_scheme=auth_scheme)
        return _strategy_base.build_repo_url(self, repo_ref, dep_ref, token, conf)

    # ------------------------------------------------------------------
    # Artifactory helpers
    # ------------------------------------------------------------------

    def get_artifactory_headers(self) -> dict[str, str]:
        return _artifactory_strategy.get_artifactory_headers(self)

    def download_artifactory_archive(
        self,
        target: "_artifactory_strategy._ArtifactoryTarget",
        target_path: Path,
    ) -> None:
        return _artifactory_strategy.download_artifactory_archive(self, target, target_path)

    def download_file_from_artifactory(
        self,
        target: "_artifactory_strategy._ArtifactoryTarget",
        file_path: str,
    ) -> bytes:
        return _artifactory_strategy.download_file_from_artifactory(self, target, file_path)

    # ------------------------------------------------------------------
    # Raw / CDN download helper
    # ------------------------------------------------------------------

    def try_raw_download(self, owner: str, repo: str, ref: str, file_path: str) -> bytes | None:
        return _git_strategy.try_raw_download(self, owner, repo, ref, file_path)

    # ------------------------------------------------------------------
    # Azure DevOps file download
    # ------------------------------------------------------------------

    def download_ado_file(
        self, dep_ref: DependencyReference, file_path: str, ref: str = "main"
    ) -> bytes:
        return _git_strategy.download_ado_file(self, dep_ref, file_path, ref)

    # ------------------------------------------------------------------
    # GitLab file download
    # ------------------------------------------------------------------

    def download_gitlab_file(
        self, dep_ref: DependencyReference, file_path: str, ref: str = "main", verbose_callback=None
    ) -> bytes:
        return _git_strategy.download_gitlab_file(self, dep_ref, file_path, ref, verbose_callback)

    # ------------------------------------------------------------------
    # GitHub file download
    # ------------------------------------------------------------------

    def download_github_file(
        self, dep_ref: DependencyReference, file_path: str, ref: str = "main", verbose_callback=None
    ) -> bytes:
        return _git_strategy.download_github_file(self, dep_ref, file_path, ref, verbose_callback)

    # ------------------------------------------------------------------
    # Helpers for download_github_file
    # ------------------------------------------------------------------

    @staticmethod
    @staticmethod
    def _is_configured_ghes(host: str) -> bool:
        return _git_strategy._is_configured_ghes(host)

    @staticmethod
    @staticmethod
    def _build_contents_api_urls(
        host: str,
        owner: str,
        repo: str,
        file_path: str,
        ref: str,
    ) -> list[str]:
        return _git_strategy._build_contents_api_urls(host, owner, repo, file_path, ref)

    @staticmethod
    @staticmethod
    def _build_generic_host_auth_headers(
        host: str, auth_ctx, *, accept: str | None = None
    ) -> dict[str, str]:
        return _git_strategy._build_generic_host_auth_headers(host, auth_ctx, accept=accept)

    @staticmethod
    @staticmethod
    def _extract_contents_api_payload(response, is_github_host: bool) -> bytes:
        return _git_strategy._extract_contents_api_payload(response, is_github_host)

    @staticmethod
    @staticmethod
    def _build_unsupported_or_missing_error(ctx: "_git_strategy._MissingFileCtx") -> str:
        return _git_strategy._build_unsupported_or_missing_error(ctx)


from . import artifactory_strategy as _artifactory_strategy
from . import git_strategy as _git_strategy
from . import strategy_base as _strategy_base
