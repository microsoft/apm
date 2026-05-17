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


def is_artifactory(self) -> bool:
    """Check if this reference points to a JFrog Artifactory VCS repository."""
    return self.artifactory_prefix is not None


def is_azure_devops(self) -> bool:
    """Check if this reference points to Azure DevOps."""
    from ....utils.github_host import is_azure_devops_hostname

    return self.host is not None and is_azure_devops_hostname(self.host)


@staticmethod
def is_local_path(dep_str: str) -> bool:
    """Check if a dependency string looks like a local filesystem path.

    Local paths start with './', '../', '/', '~/', '~\\', or a Windows drive
    letter (e.g. 'C:\\' or 'C:/').
    Protocol-relative URLs ('//...') are explicitly excluded.
    """
    s = dep_str.strip()
    # Reject protocol-relative URLs ('//...')
    if s.startswith("//"):
        return False
    if s.startswith(("./", "../", "/", "~/", "~\\", ".\\", "..\\")):
        return True
    # Windows absolute paths: drive letter + colon + separator (C:\ or C:/).
    # Only ASCII letters A-Z/a-z are valid drive letters.
    return bool(
        len(s) >= 3
        and ("A" <= s[0] <= "Z" or "a" <= s[0] <= "z")
        and s[1] == ":"
        and s[2] in ("\\", "/")
    )


def get_install_path(self, apm_modules_dir: Path) -> Path:
    """Get the canonical filesystem path where this package should be installed.

    This is the single source of truth for where a package lives in apm_modules/.

    For regular packages:
        - GitHub: apm_modules/owner/repo/
        - ADO: apm_modules/org/project/repo/

    For virtual file/collection packages:
        - GitHub: apm_modules/owner/<virtual-package-name>/
        - ADO: apm_modules/org/project/<virtual-package-name>/

    For subdirectory packages (Claude Skills, nested APM packages):
        - GitHub: apm_modules/owner/repo/subdir/path/
        - ADO: apm_modules/org/project/repo/subdir/path/

    For local packages:
        - apm_modules/_local/<directory-name>/

    Args:
        apm_modules_dir: Path to the apm_modules directory

    Raises:
        PathTraversalError: If the computed path escapes apm_modules_dir
    Returns:
        Path: Absolute path to the package installation directory
    """
    if self.is_local and self.local_path:
        pkg_dir_name = Path(self.local_path).name
        validate_path_segments(
            pkg_dir_name,
            context="local package path",
            reject_empty=True,
        )
        result = apm_modules_dir / "_local" / pkg_dir_name
        ensure_path_within(result, apm_modules_dir)
        return result

    repo_parts = self.repo_url.split("/")

    # Security: reject traversal in repo_url segments (catches lockfile injection)
    validate_path_segments(self.repo_url, context="repo_url")

    # Security: reject traversal in virtual_path (catches lockfile injection)
    if self.virtual_path:
        validate_path_segments(self.virtual_path, context="virtual_path")
    result: Path | None = None

    if self.is_virtual:
        # Subdirectory packages (like Claude Skills) should use natural path structure
        if self.is_virtual_subdirectory():
            # Use repo path + subdirectory path
            if self.is_azure_devops() and len(repo_parts) >= 3:
                # ADO: org/project/repo/subdir
                result = (
                    apm_modules_dir
                    / repo_parts[0]
                    / repo_parts[1]
                    / repo_parts[2]
                    / self.virtual_path
                )
            elif len(repo_parts) >= 2:
                # owner/repo/subdir or group/subgroup/repo/subdir
                result = apm_modules_dir.joinpath(*repo_parts, self.virtual_path)
        else:
            # Virtual file/collection: use sanitized package name (flattened)
            package_name = self.get_virtual_package_name()
            if self.is_azure_devops() and len(repo_parts) >= 3:
                # ADO: org/project/virtual-pkg-name
                result = apm_modules_dir / repo_parts[0] / repo_parts[1] / package_name
            elif len(repo_parts) >= 2:
                # owner/virtual-pkg-name (use first segment as namespace)
                result = apm_modules_dir / repo_parts[0] / package_name
    # Regular package: use full repo path
    elif self.is_azure_devops() and len(repo_parts) >= 3:
        # ADO: org/project/repo
        result = apm_modules_dir / repo_parts[0] / repo_parts[1] / repo_parts[2]
    elif len(repo_parts) >= 2:
        # owner/repo or group/subgroup/repo (generic hosts)
        result = apm_modules_dir.joinpath(*repo_parts)

    if result is None:
        # Fallback: join all parts
        result = apm_modules_dir.joinpath(*repo_parts)

    # Security: ensure the computed path stays within apm_modules/
    ensure_path_within(result, apm_modules_dir)
    return result
