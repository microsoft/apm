"""Synthetic package that stands for the project's own content."""

from __future__ import annotations

from pathlib import Path

from ..models.apm_package import APMPackage, PackageInfo, PackageType


def build_project_local_package_info(source_root: Path) -> PackageInfo:
    """Build the PackageInfo that install and audit replay use for the project itself.

    Preserve install's ``APM_PACKAGE`` interpretation instead of detecting a dependency
    package type that could promote a sibling ``skills/`` folder or root ``SKILL.md``.
    """
    local_pkg = APMPackage(
        name="_local",
        version="0.0.0",
        package_path=source_root,
        source="local",
    )
    return PackageInfo(
        package=local_pkg,
        install_path=source_root,
        package_type=PackageType.APM_PACKAGE,
    )
