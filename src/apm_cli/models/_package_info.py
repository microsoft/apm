"""PackageInfo dataclass -- extracted from apm_package to trim line count.

``APMPackage`` is only referenced in a type annotation, so it is guarded
under ``TYPE_CHECKING`` to avoid a circular import.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .dependency import DependencyReference, ResolvedReference
from .validation import PackageType

if TYPE_CHECKING:
    from .apm_package import APMPackage


@dataclass
class PackageInfo:
    """Information about a downloaded/installed package."""

    package: APMPackage
    install_path: Path
    resolved_reference: ResolvedReference | None = None
    installed_at: str | None = None  # ISO timestamp
    dependency_ref: DependencyReference | None = (
        None  # Original dependency reference for canonical string
    )
    package_type: PackageType | None = None  # APM_PACKAGE, CLAUDE_SKILL, or HYBRID

    def get_canonical_dependency_string(self) -> str:
        """Get the canonical dependency string for this package.

        Used for orphan detection - this is the unique identifier as stored in apm.yml.
        For virtual packages, includes the full path (e.g., owner/repo/collections/name).
        For regular packages, just the repo URL (e.g., owner/repo).

        Returns:
            str: Canonical dependency string, or package source/name as fallback
        """
        if self.dependency_ref:
            return self.dependency_ref.get_canonical_dependency_string()
        # Fallback to package source or name
        return self.package.source or self.package.name or "unknown"

    def get_primitives_path(self) -> Path:
        """Get path to the .apm directory for this package."""
        return self.install_path / ".apm"

    def has_primitives(self) -> bool:
        """Check if the package has any primitives."""
        apm_dir = self.get_primitives_path()
        if apm_dir.exists():
            # Check for any primitive files in .apm/ subdirectories
            for primitive_type in ["instructions", "chatmodes", "contexts", "prompts", "hooks"]:
                primitive_dir = apm_dir / primitive_type
                if primitive_dir.exists() and any(primitive_dir.iterdir()):
                    return True

        # Also check hooks/ at package root (Claude-native convention)
        hooks_dir = self.install_path / "hooks"
        if hooks_dir.exists() and any(hooks_dir.glob("*.json")):  # noqa: SIM103
            return True

        return False
