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


def to_apm_yml_entry(self):
    """Return the entry to store in apm.yml.

    For HTTP (insecure) deps, returns a dict with 'git' and 'allow_insecure' keys.
    For deps with skill_subset, returns a dict with 'git' and 'skills' keys.
    For all other deps, returns the canonical string (same as to_canonical()).

    Returns:
        str or dict: String for simple deps; dict for HTTP or skill-subset deps.
    """
    if self.is_insecure:
        host = self.host or default_host()
        entry = {"git": f"http://{host}/{self.repo_url}"}
        if self.reference:
            entry["ref"] = self.reference
        if self.alias:
            entry["alias"] = self.alias
        entry["allow_insecure"] = self.allow_insecure
        if self.skill_subset:
            entry["skills"] = sorted(self.skill_subset)
        return entry
    if self.skill_subset:
        entry = {"git": self.get_identity()}
        if self.reference:
            entry["ref"] = self.reference
        if self.alias:
            entry["alias"] = self.alias
        entry["skills"] = sorted(self.skill_subset)
        return entry
    return self.to_canonical()


def to_github_url(self) -> str:
    """Convert to full repository URL.

    For Azure DevOps, generates: https://dev.azure.com/org/project/_git/repo
    For GitHub, generates: https://github.com/owner/repo
    For local packages, returns the local path.
    """
    if self.is_local and self.local_path:
        return self.local_path

    host = self.host or default_host()
    netloc = f"{host}:{self.port}" if self.port else host

    scheme = "http" if self.is_insecure else "https"

    if self.is_azure_devops():
        # ADO format: https://dev.azure.com/org/project/_git/repo
        project = urllib.parse.quote(self.ado_project, safe="")
        repo = urllib.parse.quote(self.ado_repo, safe="")
        return f"https://{netloc}/{self.ado_organization}/{project}/_git/{repo}"
    elif self.artifactory_prefix:
        return f"{scheme}://{netloc}/{self.artifactory_prefix}/{self.repo_url}"
    else:
        # Git host format: https://github.com/owner/repo
        return f"{scheme}://{netloc}/{self.repo_url}"


def to_clone_url(self) -> str:
    """Convert to a clone-friendly URL (same as to_github_url for most purposes)."""
    return self.to_github_url()
