"""DependencyReference model  -- core dependency representation and parsing."""

import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from ....cache.url_normalize import SCP_LIKE_RE
from ....utils.github_host import (
    default_host,
    is_artifactory_path,
    is_azure_devops_hostname,
    is_github_hostname,
    is_gitlab_hostname,
    is_supported_git_host,
    is_visualstudio_legacy_hostname,
    maybe_raise_bare_fqdn_github_gitlab_conflict,
    parse_artifactory_path,
    unsupported_host_error,
)
from ....utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)
from ...validation import InvalidVirtualPackageExtensionError
from ..types import VirtualPackageType

# Default ports per URI scheme -- used to normalise away redundant
# explicit ports (e.g. https://host:443/...) so that lockfile keys
# and error messages stay consistent regardless of how the user
# spelled the URL.
_DEFAULT_SCHEME_PORTS: dict[str, int] = {"https": 443, "http": 80, "ssh": 22}


from .core import DependencyReference


def get_unique_key(self) -> str:
    """Get a unique key for this dependency for deduplication.

    For regular packages: repo_url
    For virtual packages: repo_url + virtual_path to ensure uniqueness
    For local packages: the local_path

    Returns:
        str: Unique key for this dependency
    """
    if self.is_local and self.local_path:
        return self.local_path
    if self.is_virtual and self.virtual_path:
        return f"{self.repo_url}/{self.virtual_path}"
    return self.repo_url


def to_canonical(self) -> str:
    """Return the canonical scheme-free identity string for this dependency.

    Follows the Docker-style default-registry convention:
    - Default host (github.com) is stripped  ->  owner/repo
    - Non-default hosts are preserved         ->  gitlab.com/owner/repo
    - Virtual paths are appended              ->  owner/repo/path/to/thing
    - Refs are appended with #                ->  owner/repo#v1.0
    - Local paths are returned as-is          ->  ./packages/my-pkg

    No .git suffix, no git@, and no transport scheme -- just the canonical
    identifier. Use ``to_apm_yml_entry()`` when the serialized apm.yml value
    must preserve an explicit ``http://`` transport.

    Returns:
        str: Canonical dependency string
    """
    if self.is_local and self.local_path:
        return self.local_path

    host = self.host or default_host()

    is_default = host.lower() == default_host().lower()
    # Custom port is part of the transport and must travel with the host label.
    host_label = f"{host}:{self.port}" if self.port else host

    # Start with optional host prefix
    if is_default and not self.port and not self.artifactory_prefix:
        result = self.repo_url
    elif self.artifactory_prefix:
        result = f"{host_label}/{self.artifactory_prefix}/{self.repo_url}"
    else:
        result = f"{host_label}/{self.repo_url}"

    # Append virtual path for virtual packages
    if self.is_virtual and self.virtual_path:
        result = f"{result}/{self.virtual_path}"

    # Append reference (branch, tag, commit)
    if self.reference:
        result = f"{result}#{self.reference}"

    return result


def get_identity(self) -> str:
    """Return the identity of this dependency (canonical form without ref/alias).

    Two deps with the same identity are the same package, regardless of
    which ref or alias they specify. Used for duplicate detection and uninstall matching.

    Returns:
        str: Identity string (e.g., "owner/repo" or "gitlab.com/owner/repo/path")
    """
    if self.is_local and self.local_path:
        return self.local_path

    host = self.host or default_host()
    is_default = host.lower() == default_host().lower()
    host_label = f"{host}:{self.port}" if self.port else host

    if is_default and not self.port and not self.artifactory_prefix:
        result = self.repo_url
    elif self.artifactory_prefix:
        result = f"{host_label}/{self.artifactory_prefix}/{self.repo_url}"
    else:
        result = f"{host_label}/{self.repo_url}"

    if self.is_virtual and self.virtual_path:
        result = f"{result}/{self.virtual_path}"

    return result


@staticmethod
def canonicalize(raw: str) -> str:
    """Parse any raw input form and return its canonical identifier form.

    Convenience method that combines parse() + to_canonical().

    Args:
        raw: Any supported input form (shorthand, FQDN, HTTPS, SSH, etc.)

    Returns:
        str: Canonical scheme-free identifier form
    """
    return DependencyReference.parse(raw).to_canonical()


def get_canonical_dependency_string(self) -> str:
    """Get the host-blind canonical string for filesystem and orphan-detection matching.

    This returns repo_url (+ virtual_path) without host prefix -- it matches
    the filesystem layout in apm_modules/ which is also host-blind.

    For identity-based matching that includes non-default hosts, use get_identity().
    For the transport-aware apm.yml entry, use to_apm_yml_entry().

    Returns:
        str: Host-blind canonical string (e.g., "owner/repo")
    """
    return self.get_unique_key()


def get_display_name(self) -> str:
    """Get display name for this dependency (alias or repo name)."""
    if self.alias:
        return self.alias
    if self.is_local and self.local_path:
        return self.local_path
    if self.is_virtual:
        return self.get_virtual_package_name()
    return self.repo_url  # Full repo URL for disambiguation


def __str__(self) -> str:
    """String representation of the dependency reference."""
    if self.is_local and self.local_path:
        return self.local_path
    if self.host:
        host_label = f"{self.host}:{self.port}" if self.port else self.host
        if self.artifactory_prefix:
            result = f"{host_label}/{self.artifactory_prefix}/{self.repo_url}"
        else:
            result = f"{host_label}/{self.repo_url}"
    else:
        result = self.repo_url
    if self.virtual_path:
        result += f"/{self.virtual_path}"
    if self.reference:
        result += f"#{self.reference}"
    if self.alias:
        result += f"@{self.alias}"
    return result
