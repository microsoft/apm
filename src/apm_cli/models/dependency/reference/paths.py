"""DependencyReference model  -- core dependency representation and parsing."""

from pathlib import Path

from ....utils.github_host import (
    is_azure_devops_hostname,
)
from ....utils.path_security import (
    ensure_path_within,
    validate_path_segments,
)

# Default ports per URI scheme -- used to normalise away redundant
# explicit ports (e.g. https://host:443/...) so that lockfile keys
# and error messages stay consistent regardless of how the user
# spelled the URL.
_DEFAULT_SCHEME_PORTS: dict[str, int] = {"https": 443, "http": 80, "ssh": 22}


def is_artifactory(self) -> bool:
    """Check if this reference points to a JFrog Artifactory VCS repository."""
    return self.artifactory_prefix is not None


def is_azure_devops(self) -> bool:
    """Check if this reference points to Azure DevOps."""

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


def _get_local_install_path(self, apm_modules_dir: Path) -> Path:
    """Get install path for local packages."""
    if not self.local_path:
        raise ValueError("Local package missing local_path")
    pkg_dir_name = Path(self.local_path).name
    validate_path_segments(
        pkg_dir_name,
        context="local package path",
        reject_empty=True,
    )
    result = apm_modules_dir / "_local" / pkg_dir_name
    ensure_path_within(result, apm_modules_dir)
    return result


def _get_virtual_subdirectory_install_path(
    self, apm_modules_dir: Path, repo_parts: list[str]
) -> Path:
    """Get install path for virtual subdirectory packages."""
    if self.is_azure_devops() and len(repo_parts) >= 3:
        result = apm_modules_dir / repo_parts[0] / repo_parts[1] / repo_parts[2] / self.virtual_path
    elif len(repo_parts) >= 2:
        result = apm_modules_dir.joinpath(*repo_parts, self.virtual_path)
    else:
        result = apm_modules_dir.joinpath(*repo_parts)
    return result


def _get_virtual_file_install_path(self, apm_modules_dir: Path, repo_parts: list[str]) -> Path:
    """Get install path for virtual file/collection packages."""
    package_name = self.get_virtual_package_name()
    if self.is_azure_devops() and len(repo_parts) >= 3:
        result = apm_modules_dir / repo_parts[0] / repo_parts[1] / package_name
    elif len(repo_parts) >= 2:
        result = apm_modules_dir / repo_parts[0] / package_name
    else:
        result = apm_modules_dir.joinpath(*repo_parts)
    return result


def _get_regular_install_path(self, apm_modules_dir: Path, repo_parts: list[str]) -> Path:
    """Get install path for regular (non-virtual) packages."""
    if self.is_azure_devops() and len(repo_parts) >= 3:
        result = apm_modules_dir / repo_parts[0] / repo_parts[1] / repo_parts[2]
    elif len(repo_parts) >= 2:
        result = apm_modules_dir.joinpath(*repo_parts)
    else:
        result = apm_modules_dir.joinpath(*repo_parts)
    return result


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
    if self.is_local:
        return self._get_local_install_path(apm_modules_dir)

    repo_parts = self.repo_url.split("/")
    validate_path_segments(self.repo_url, context="repo_url")
    if self.virtual_path:
        validate_path_segments(self.virtual_path, context="virtual_path")

    if self.is_virtual:
        if self.is_virtual_subdirectory():
            result = self._get_virtual_subdirectory_install_path(apm_modules_dir, repo_parts)
        else:
            result = self._get_virtual_file_install_path(apm_modules_dir, repo_parts)
    else:
        result = self._get_regular_install_path(apm_modules_dir, repo_parts)

    ensure_path_within(result, apm_modules_dir)
    return result
