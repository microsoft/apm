"""Models for APM CLI data structures."""

from .apm_package import APMPackage, PackageInfo, clear_apm_yml_cache
from .dependency import (
    DependencyReference,
    GitReferenceType,
    MCPDependency,
    ResolvedReference,
    parse_git_reference,
)
from .results import InstallResult, PrimitiveCounts
from .validation import (
    InvalidVirtualPackageExtensionError,
    PackageContentType,
    PackageType,
    ValidationError,
    ValidationResult,
    detect_package_type,
    validate_apm_package,
)

__all__ = [  # noqa: RUF022
    # Core
    "APMPackage",
    "PackageInfo",
    "clear_apm_yml_cache",
    # Dependency
    "DependencyReference",
    "GitReferenceType",
    "MCPDependency",
    "ResolvedReference",
    "parse_git_reference",
    # Validation
    "InvalidVirtualPackageExtensionError",
    "PackageContentType",
    "PackageType",
    "ValidationError",
    "ValidationResult",
    "detect_package_type",
    "validate_apm_package",
    # Results
    "InstallResult",
    "PrimitiveCounts",
]
