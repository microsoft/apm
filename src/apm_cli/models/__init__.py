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

__all__ = [
    # Core
    "APMPackage",
    # Dependency
    "DependencyReference",
    "GitReferenceType",
    # Results
    "InstallResult",
    # Validation
    "InvalidVirtualPackageExtensionError",
    "MCPDependency",
    "PackageContentType",
    "PackageInfo",
    "PackageType",
    "PrimitiveCounts",
    "ResolvedReference",
    "ValidationError",
    "ValidationResult",
    "clear_apm_yml_cache",
    "detect_package_type",
    "parse_git_reference",
    "validate_apm_package",
]
