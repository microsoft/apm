"""OCI package retrieval for APM dependencies."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    PackageType,
    ResolvedReference,
    validate_apm_package,
)


@dataclass(frozen=True)
class OCIPullResult:
    """Result of pulling an OCI-backed APM package."""

    package_path: Path
    resolved_reference: str
    resolved_digest: Optional[str] = None


class OCIRegistryClient:
    """Fetches OCI-backed APM package artifacts using the ORAS CLI."""

    def pull_package(
        self,
        resolved_reference: str,
        target_path: Path,
        requirement=None,
    ) -> PackageInfo:
        """Pull an OCI artifact into *target_path* and validate it as an APM package.

        Expected OCI payload:
        - one ``*.tar.gz`` archive
        - archive contains raw APM package sources with ``apm.yml`` at the root
          or inside a single top-level directory
        """
        if target_path.exists() and any(target_path.iterdir()):
            shutil.rmtree(target_path, ignore_errors=True)
        target_path.mkdir(parents=True, exist_ok=True)

        pull_dir = Path(tempfile.mkdtemp(prefix="apm-oci-pull-"))
        try:
            self._pull_artifact(resolved_reference, pull_dir)
            archive_path = self._locate_package_archive(pull_dir)
            package_root = self._extract_package_archive(archive_path, target_path)
        finally:
            shutil.rmtree(pull_dir, ignore_errors=True)

        validation = validate_apm_package(package_root)
        if not validation.is_valid:
            issues = (
                "; ".join(getattr(err, "message", str(err)) for err in validation.errors)
                if validation.errors else "unknown validation error"
            )
            raise RuntimeError(f"OCI artifact {resolved_reference} is not a valid APM package: {issues}")

        package = APMPackage.from_apm_yml(package_root / "apm.yml")
        package.package_path = package_root
        package.source = resolved_reference

        return PackageInfo(
            package=package,
            install_path=package_root,
            resolved_reference=ResolvedReference(
                original_ref=resolved_reference,
                ref_type=GitReferenceType.BRANCH,
                resolved_commit=None,
                ref_name=getattr(requirement, "version", None),
            ),
            installed_at=datetime.now().isoformat(),
            dependency_ref=requirement,
            package_type=validation.package_type or PackageType.APM_PACKAGE,
        )

    def _pull_artifact(self, resolved_reference: str, output_dir: Path) -> None:
        """Pull the OCI artifact to *output_dir* using ORAS."""
        try:
            proc = subprocess.run(
                ["oras", "pull", resolved_reference, "--output", str(output_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "OCI support requires the 'oras' CLI to be installed and available on PATH."
            ) from exc

        if proc.returncode != 0:
            stderr = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"Failed to pull OCI artifact {resolved_reference}: {stderr}")

    @staticmethod
    def _locate_package_archive(output_dir: Path) -> Path:
        """Find the pulled raw-package archive in ORAS output."""
        archives = sorted(output_dir.rglob("*.tar.gz"))
        if len(archives) == 1:
            return archives[0]
        if not archives:
            raise RuntimeError(
                "OCI artifact did not contain a raw APM package archive (*.tar.gz)."
            )
        raise RuntimeError(
            "OCI artifact contained multiple package archives; expected exactly one *.tar.gz."
        )

    @staticmethod
    def _extract_package_archive(archive_path: Path, target_path: Path) -> Path:
        """Extract a raw-package archive and return the package root."""
        with tarfile.open(archive_path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise RuntimeError(
                        f"Refusing to extract unsafe archive entry: {member.name}"
                    )
                if member.issym() or member.islnk():
                    raise RuntimeError(
                        f"Refusing to extract symlink/hardlink from OCI archive: {member.name}"
                    )
            if sys.version_info >= (3, 12):
                tar.extractall(target_path, filter="data")
            else:
                tar.extractall(target_path)  # noqa: S202

        if (target_path / "apm.yml").exists():
            return target_path
        for child in sorted(target_path.iterdir()):
            if child.is_dir() and (child / "apm.yml").exists():
                return child
        raise RuntimeError(
            "Extracted OCI archive did not contain an APM package root with apm.yml."
        )
