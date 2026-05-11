"""Artifactory download orchestration.

This module isolates everything about *Artifactory routing* -- how to
decide whether a dependency should flow through Artifactory, how to
parse the registry-proxy config, and how to materialize a full package
or a subdirectory from an Artifactory VCS archive.

Design pattern: **Adapter** + **Facade**.

- The :class:`ArtifactoryRouter` adapts the heterogeneous ``dep_ref``
  shapes ("explicit Artifactory FQDN", "transparent proxy of GitHub",
  "registry-only mode") into the single decision the caller needs:
  *should this dependency be fetched from Artifactory, and if so,
  with which (host, prefix, scheme)?*

- The :class:`ArtifactoryOrchestrator` is the facade in front of the
  download flow itself: ``download_package`` and
  ``download_subdirectory`` are the two operations the rest of the
  codebase actually invokes. They depend only on a small downloader
  protocol (``_HasArchiveDownloader``) so they remain testable in
  isolation.

Why this lives in its own module: the bulk-cum-fragility ratio. The
flow (parse base URL -> decide routing -> tempdir extraction ->
APM validation -> stamp metadata) is purely internal to Artifactory
and crosscuts none of the other vendor backends. Keeping it inside
``GitHubPackageDownloader`` made the latter monolithic without any
reuse benefit.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..models.apm_package import (
    GitReferenceType,
    PackageInfo,
    ResolvedReference,
    validate_apm_package,
)
from ..utils.github_host import default_host, is_github_hostname

if TYPE_CHECKING:
    from ..models.apm_package import DependencyReference


# ---------------------------------------------------------------------------
# Internal collaborators
# ---------------------------------------------------------------------------


class _HasArchiveDownloader(Protocol):
    """Minimal contract the orchestrator needs from its caller.

    Implementations:
      - :class:`apm_cli.deps.download_strategies.DownloadDelegate`
        (production: real HTTP archive download).
      - Test doubles (unit tests).
    """

    def download_artifactory_archive(  # pragma: no cover - protocol only
        self,
        host: str,
        prefix: str,
        owner: str,
        repo: str,
        ref: str,
        target_path: Path,
        *,
        scheme: str = "https",
    ) -> None: ...


# ---------------------------------------------------------------------------
# Routing decisions
# ---------------------------------------------------------------------------


class ArtifactoryRouter:
    """Decide whether (and how) a dependency should flow through Artifactory.

    Stateless aside from environment variable inspection, which is
    delegated to :class:`apm_cli.deps.registry_proxy.RegistryConfig`.
    """

    @staticmethod
    def is_registry_only() -> bool:
        """Return True when registry-only mode is active.

        Honors the canonical ``PROXY_REGISTRY_ONLY`` env var, with
        backward compatibility for the deprecated ``ARTIFACTORY_ONLY``
        alias.
        """
        from .registry_proxy import is_enforce_only

        return is_enforce_only()

    @classmethod
    def should_use_proxy(cls, dep_ref: DependencyReference) -> bool:
        """Return True when *dep_ref* should be routed through the
        transparent Artifactory proxy.

        Note: returns False when the dependency is already an explicit
        Artifactory dep (``dep_ref.is_artifactory()``); that path takes
        a different branch in :meth:`ArtifactoryOrchestrator.download_package`.
        """
        if dep_ref.is_artifactory():
            return False
        if cls.is_registry_only():
            return True
        if dep_ref.is_azure_devops():
            return False
        host = dep_ref.host or default_host()
        return is_github_hostname(host)

    @staticmethod
    def parse_proxy_config() -> tuple[str, str, str] | None:
        """Return ``(host, prefix, scheme)`` from registry-proxy env, or None.

        Delegates env-var precedence and deprecation warnings to
        :class:`~apm_cli.deps.registry_proxy.RegistryConfig`.
        """
        from .registry_proxy import RegistryConfig

        cfg = RegistryConfig.from_env()
        if cfg is None:
            return None
        return (cfg.host, cfg.prefix, cfg.scheme)


# ---------------------------------------------------------------------------
# Download orchestration
# ---------------------------------------------------------------------------


class ArtifactoryOrchestrator:
    """Materialize Artifactory packages and subdirectories.

    Holds a reference to a :class:`_HasArchiveDownloader` -- in
    production this is the same ``DownloadDelegate`` instance used by
    the surrounding ``GitHubPackageDownloader``, so HTTP session reuse
    and auth headers stay shared.
    """

    def __init__(self, archive_downloader: _HasArchiveDownloader) -> None:
        self._archive_downloader = archive_downloader

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _resolve_host_prefix(
        dep_ref: DependencyReference,
        proxy_info: tuple[str, str, str] | None,
    ) -> tuple[str, str, str]:
        """Return ``(host, prefix, scheme)`` for the download.

        Explicit Artifactory deps win; otherwise fall back to the
        registry-proxy config.
        """
        if dep_ref.is_artifactory():
            host, prefix = dep_ref.host, dep_ref.artifactory_prefix
            if not host or not prefix:
                raise ValueError(
                    f"Artifactory dependency '{dep_ref.repo_url}' is missing "
                    "host or artifactory prefix"
                )
            return (host, prefix, "https")
        if proxy_info:
            return proxy_info
        raise RuntimeError("Artifactory download requires either FQDN or ARTIFACTORY_BASE_URL")

    @staticmethod
    def _split_owner_repo(dep_ref: DependencyReference) -> tuple[str, str]:
        repo_parts = dep_ref.repo_url.split("/")
        if len(repo_parts) < 2 or not repo_parts[0] or not repo_parts[1]:
            raise ValueError(
                f"Invalid Artifactory repo reference '{dep_ref.repo_url}': "
                "expected 'owner/repo' format"
            )
        return repo_parts[0], repo_parts[1]

    @staticmethod
    def _progress(progress_obj, progress_task_id, *, completed: int, total: int = 100) -> None:
        if progress_obj and progress_task_id is not None:
            progress_obj.update(progress_task_id, completed=completed, total=total)

    # -- public surface -------------------------------------------------

    def download_package(
        self,
        dep_ref: DependencyReference,
        target_path: Path,
        proxy_info: tuple[str, str, str] | None = None,
        progress_task_id=None,
        progress_obj=None,
    ) -> PackageInfo:
        """Download a full APM package via Artifactory VCS archive."""
        from ..utils.file_ops import robust_rmtree
        from .github_downloader import _debug, _rmtree

        ref = dep_ref.reference or "main"
        owner, repo = self._split_owner_repo(dep_ref)
        host, prefix, scheme = self._resolve_host_prefix(dep_ref, proxy_info)

        _debug(f"Downloading from Artifactory: {host}/{prefix}/{owner}/{repo}#{ref}")
        if target_path.exists() and any(target_path.iterdir()):
            robust_rmtree(target_path)
        target_path.mkdir(parents=True, exist_ok=True)
        self._progress(progress_obj, progress_task_id, completed=10)
        try:
            self._archive_downloader.download_artifactory_archive(
                host, prefix, owner, repo, ref, target_path, scheme=scheme
            )
        except RuntimeError:
            if target_path.exists():
                _rmtree(target_path)
            raise
        self._progress(progress_obj, progress_task_id, completed=70)

        validation_result = validate_apm_package(target_path)
        if not validation_result.is_valid:
            if target_path.exists():
                _rmtree(target_path)
            error_msg = f"Invalid APM package {dep_ref.repo_url}:\n"
            for error in validation_result.errors:
                error_msg += f"  - {error}\n"
            raise RuntimeError(error_msg.strip())
        if not validation_result.package:
            raise RuntimeError(
                f"Package validation succeeded but no package metadata found for {dep_ref.repo_url}"
            )
        package = validation_result.package
        package.source = dep_ref.to_github_url()
        package.resolved_commit = None
        resolved_ref = ResolvedReference(
            original_ref=f"{dep_ref.repo_url}#{ref}",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=None,
            ref_name=ref,
        )
        self._progress(progress_obj, progress_task_id, completed=100)
        return PackageInfo(
            package=package,
            install_path=target_path,
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,
            package_type=validation_result.package_type,
        )

    def download_subdirectory(
        self,
        dep_ref: DependencyReference,
        target_path: Path,
        proxy_info: tuple[str, str, str],
        progress_task_id=None,
        progress_obj=None,
    ) -> PackageInfo:
        """Download an archive from Artifactory and extract a subdirectory."""
        from ..config import get_apm_temp_dir
        from ..utils.file_ops import robust_copy2, robust_copytree, robust_rmtree

        ref = dep_ref.reference or "main"
        subdir_path = dep_ref.virtual_path
        repo_parts = dep_ref.repo_url.split("/")
        owner = repo_parts[0]
        repo = repo_parts[1] if len(repo_parts) > 1 else repo_parts[0]
        host, prefix, scheme = proxy_info

        self._progress(progress_obj, progress_task_id, completed=10)

        with tempfile.TemporaryDirectory(dir=get_apm_temp_dir()) as temp_dir:
            temp_path = Path(temp_dir) / "full_pkg"
            self._archive_downloader.download_artifactory_archive(
                host, prefix, owner, repo, ref, temp_path, scheme=scheme
            )
            self._progress(progress_obj, progress_task_id, completed=60)
            source_subdir = temp_path / subdir_path
            if not source_subdir.exists() or not source_subdir.is_dir():
                raise RuntimeError(
                    f"Subdirectory '{subdir_path}' not found in archive from "
                    f"Artifactory ({host}/{prefix}/{owner}/{repo}#{ref})"
                )
            target_path.mkdir(parents=True, exist_ok=True)
            if target_path.exists() and any(target_path.iterdir()):
                robust_rmtree(target_path)
                target_path.mkdir(parents=True, exist_ok=True)
            for item in source_subdir.iterdir():
                src = source_subdir / item.name
                dst = target_path / item.name
                if src.is_dir():
                    robust_copytree(src, dst)
                else:
                    robust_copy2(src, dst)

        self._progress(progress_obj, progress_task_id, completed=80)
        validation_result = validate_apm_package(target_path)
        if not validation_result.is_valid:
            raise RuntimeError(
                f"Subdirectory is not a valid APM package: {'; '.join(validation_result.errors)}"
            )
        resolved_ref = ResolvedReference(
            original_ref=ref,
            ref_name=ref,
            ref_type=GitReferenceType.BRANCH,
            resolved_commit=None,
        )
        self._progress(progress_obj, progress_task_id, completed=100)
        return PackageInfo(
            package=validation_result.package,
            install_path=target_path,
            resolved_reference=resolved_ref,
            installed_at=datetime.now().isoformat(),
            dependency_ref=dep_ref,
            package_type=validation_result.package_type,
        )
