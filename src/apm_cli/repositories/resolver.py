"""Repository-driven resolution for logical APM package requirements."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..deps.github_downloader import GitHubPackageDownloader
from ..deps.oci_registry import OCIRegistryClient
from ..models.dependency import DependencyReference, PackageRequirement
from ..repositories.config import RepositoryDefinition, load_repositories


@dataclass(frozen=True)
class ResolvedArtifact:
    """A concrete artifact selected for a logical package requirement."""

    repository_name: str
    source_type: str
    locator: str
    host: Optional[str] = None


class ArtifactResolver:
    """Resolve logical package requirements through configured repositories."""

    def __init__(
        self,
        git_downloader: GitHubPackageDownloader,
        oci_client: OCIRegistryClient,
    ):
        self.git_downloader = git_downloader
        self.oci_client = oci_client

    def fetch_requirement(self, requirement: PackageRequirement, target_path: Path):
        """Resolve and fetch a package requirement into *target_path*."""
        last_error: Optional[Exception] = None
        for repository in load_repositories():
            if requirement.repository and repository.name != requirement.repository:
                continue
            try:
                artifact = self._resolve_artifact(requirement, repository)
                return self._fetch_artifact(requirement, artifact, target_path)
            except Exception as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise RuntimeError(
                f"Failed to resolve {requirement} from configured repositories: {last_error}"
            )
        if requirement.repository:
            raise RuntimeError(
                f"Requested repository '{requirement.repository}' is not configured for {requirement}"
            )
        raise RuntimeError(f"No configured repository could resolve {requirement}")

    def _resolve_artifact(
        self, requirement: PackageRequirement, repository: RepositoryDefinition
    ) -> ResolvedArtifact:
        """Build a concrete locator for a requirement against one repository."""
        if repository.type == "git":
            locator = f"{repository.base.rstrip('/')}/{requirement.name}.git"
            if requirement.version:
                locator += f"#{requirement.version}"
            host = locator.split("://", 1)[1].split("/", 1)[0] if "://" in locator else None
            return ResolvedArtifact(
                repository_name=repository.name,
                source_type="git",
                locator=locator,
                host=host,
            )

        if repository.type == "oci":
            if not requirement.version:
                raise RuntimeError(
                    f"OCI repository '{repository.name}' requires a version for {requirement.name}"
                )
            locator = f"{repository.base.rstrip('/')}/{requirement.name}:{requirement.version}"
            return ResolvedArtifact(
                repository_name=repository.name,
                source_type="oci",
                locator=locator,
            )

        raise RuntimeError(f"Unsupported repository type: {repository.type}")

    def _fetch_artifact(
        self,
        requirement: PackageRequirement,
        artifact: ResolvedArtifact,
        target_path: Path,
    ):
        """Fetch a concrete artifact and annotate the requirement with the result."""
        if artifact.source_type == "git":
            dep_ref = DependencyReference.parse(artifact.locator)
            package_info = self.git_downloader.download_package(dep_ref, target_path)
            requirement.resolved_source_type = "git"
            requirement.resolved_repository = artifact.repository_name
            requirement.resolved_ref = artifact.locator
            requirement.resolved_host = dep_ref.host
            return package_info

        if artifact.source_type == "oci":
            package_info = self.oci_client.pull_package(artifact.locator, target_path, requirement)
            requirement.resolved_source_type = "oci"
            requirement.resolved_repository = artifact.repository_name
            requirement.resolved_ref = artifact.locator
            requirement.resolved_digest = getattr(package_info, "resolved_digest", None)
            return package_info

        raise RuntimeError(f"Unsupported artifact type: {artifact.source_type}")
