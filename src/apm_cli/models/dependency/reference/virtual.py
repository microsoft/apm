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


@property
def virtual_type(self) -> "VirtualPackageType | None":
    """Return the type of virtual package, or None if not virtual.

    Classification is by extension only -- never by path segment.
    ``.prompt.md``/``.instructions.md``/``.chatmode.md``/``.agent.md``
    is FILE; everything else is SUBDIRECTORY (resolved at fetch time
    by probing for ``apm.yml``, ``SKILL.md``, ``plugin.json``, etc).
    Paths like ``collections/foo`` (no extension) are SUBDIRECTORY.
    """
    if not self.is_virtual or not self.virtual_path:
        return None
    if any(self.virtual_path.endswith(ext) for ext in self.VIRTUAL_FILE_EXTENSIONS):
        return VirtualPackageType.FILE
    return VirtualPackageType.SUBDIRECTORY


def is_virtual_file(self) -> bool:
    """Check if this is a virtual file package (individual file)."""
    return self.virtual_type == VirtualPackageType.FILE


def is_virtual_subdirectory(self) -> bool:
    """Check if this is a virtual subdirectory package (e.g., Claude Skill).

    A subdirectory package is a virtual package whose ``virtual_path``
    does not end in a recognized FILE extension. The actual on-disk
    shape is resolved at fetch time -- ``apm.yml``, ``SKILL.md``,
    ``plugin.json``, etc.

    Examples:
        - ComposioHQ/awesome-claude-skills/brand-guidelines -> True
        - owner/repo/prompts/file.prompt.md -> False (is_virtual_file)
        - owner/repo/collections/name -> True (resolved at fetch time)
    """
    return self.virtual_type == VirtualPackageType.SUBDIRECTORY


def get_virtual_package_name(self) -> str:
    """Generate a package name for this virtual package.

    For virtual packages, we create a sanitized name from the path:
    - owner/repo/prompts/code-review.prompt.md -> repo-code-review
    - owner/repo/collections/project-planning -> repo-project-planning
    """
    if not self.is_virtual or not self.virtual_path:
        return self.repo_url.split("/")[-1]  # Return repo name as fallback

    # Extract repo name and file/collection name
    repo_parts = self.repo_url.split("/")
    repo_name = repo_parts[-1] if repo_parts else "package"

    # Get the basename without extension
    path_parts = self.virtual_path.split("/")
    last = path_parts[-1]
    # Strip any recognised virtual file extension. The directory name
    # (or file basename) is the user-visible package name.
    for ext in self.VIRTUAL_FILE_EXTENSIONS:
        if last.endswith(ext):
            last = last[: -len(ext)]
            break
    return f"{repo_name}-{last}"
