"""Package-content routing helpers for skill integration."""

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.models.apm_package import PackageContentType


def get_effective_type(package_info) -> "PackageContentType":
    """Return the effective content type based on package structure."""
    from apm_cli.models.apm_package import PackageContentType, PackageType

    if package_info.package_type in (
        PackageType.CLAUDE_SKILL,
        PackageType.HYBRID,
        PackageType.SKILL_BUNDLE,
        PackageType.MARKETPLACE_PLUGIN,
    ):
        return PackageContentType.SKILL
    return PackageContentType.INSTRUCTIONS


def should_install_skill(
    package_info,
    *,
    resolve_effective_type: Callable = get_effective_type,
) -> bool:
    """Return whether the package should be installed as a native skill."""
    from apm_cli.models.apm_package import PackageContentType

    return resolve_effective_type(package_info) in (
        PackageContentType.SKILL,
        PackageContentType.HYBRID,
    )


def should_compile_instructions(
    package_info,
    *,
    resolve_effective_type: Callable = get_effective_type,
) -> bool:
    """Return whether package instructions should be included in compiled output."""
    from apm_cli.models.apm_package import PackageContentType

    return resolve_effective_type(package_info) in (
        PackageContentType.INSTRUCTIONS,
        PackageContentType.HYBRID,
    )
