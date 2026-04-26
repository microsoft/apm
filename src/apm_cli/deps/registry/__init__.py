"""Dedicated registry API resolver.

Additive resolver mode that fetches APM packages over a REST registry contract
(see docs/proposals/registry-api.md). Sits alongside the existing Git resolver;
opt-in via the top-level ``registries:`` block in ``apm.yml`` or the
``APM_ENABLE_REGISTRY=1`` environment variable.

This package is intentionally separate from ``src/apm_cli/registry/`` (the MCP
registry client) — the two address different concepts and must not be confused.
"""

from .auth import RegistryAuthContext, resolve_registry_token
from .client import RegistryClient, RegistryError, VersionEntry
from .extractor import extract_tarball, verify_sha256
from .resolver import RegistryPackageResolver
from .semver import is_semver_range, match_version

__all__ = [
    "RegistryAuthContext",
    "RegistryClient",
    "RegistryError",
    "RegistryPackageResolver",
    "VersionEntry",
    "extract_tarball",
    "is_semver_range",
    "match_version",
    "resolve_registry_token",
    "verify_sha256",
]
