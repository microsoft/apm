"""Dedicated registry API resolver.

Additive resolver mode that fetches APM packages over a REST registry contract
(see docs/proposals/registry-api.md). Sits alongside the existing Git resolver;
opt-in via ``apm experimental enable registries`` before using the
top-level ``registries:`` block in ``apm.yml``.

This package is intentionally separate from ``src/apm_cli/registry/`` (the MCP
registry client) — the two address different concepts and must not be confused.
"""

from .auth import (
    RegistryAuthContext,
    make_auth_context,
    registry_token_env_var,
    resolve_registry_basic,
    resolve_registry_token,
)
from .client import RegistryClient, RegistryError, VersionEntry
from .config_loader import load_merged_registries, resolve_effective_registries
from .extractor import (
    extract_archive,
    extract_tarball,
    extract_zip,
    verify_sha256,
)
from .feature_gate import (
    DISPLAY_NAME,
    ENABLE_COMMAND,
    FLAG_NAME,
    PackageRegistryFeatureDisabledError,
    is_package_registry_enabled,
    require_package_registry_enabled,
)
from .resolver import RegistryPackageResolver
from .semver import is_semver_range, match_version

__all__ = [
    "DISPLAY_NAME",
    "ENABLE_COMMAND",
    "FLAG_NAME",
    "PackageRegistryFeatureDisabledError",
    "RegistryAuthContext",
    "RegistryClient",
    "RegistryError",
    "RegistryPackageResolver",
    "VersionEntry",
    "extract_archive",
    "extract_tarball",
    "extract_zip",
    "is_package_registry_enabled",
    "is_semver_range",
    "load_merged_registries",
    "make_auth_context",
    "match_version",
    "registry_token_env_var",
    "require_package_registry_enabled",
    "resolve_effective_registries",
    "resolve_registry_basic",
    "resolve_registry_token",
    "verify_sha256",
]
