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


@classmethod
def _resolve_virtual_shorthand_repo(cls, repo_url, validated_host, virtual_path=None):
    """Narrow a virtual-package shorthand to just the base repo path.

    When a virtual package is given without a URL scheme
    (e.g. ``github.com/owner/repo/path/file.prompt.md``), this strips
    the virtual suffix so the downstream shorthand resolver only sees
    the ``owner/repo`` (or ``org/project/repo`` for ADO) portion.

    Returns:
        ``(host, repo_url)`` where *host* may be ``None``.
    """
    parts = repo_url.split("/")

    if "_git" in parts:
        git_idx = parts.index("_git")
        parts = parts[:git_idx] + parts[git_idx + 1 :]

    host = None
    if len(parts) >= 3 and is_supported_git_host(parts[0]):
        host = parts[0]
        if is_azure_devops_hostname(parts[0]):
            if is_visualstudio_legacy_hostname(parts[0]):
                # myorg.visualstudio.com/proj/repo/path: org in subdomain,
                # need at least host + proj + repo + 1 virtual segment.
                if len(parts) < 4:
                    raise ValueError(
                        "Invalid Azure DevOps virtual package format: must be "
                        "myorg.visualstudio.com/project/repo/path"
                    )
                repo_url = "/".join(parts[1:3])
            else:
                # dev.azure.com/org/proj/repo/path: org in path
                if len(parts) < 5:
                    raise ValueError(
                        "Invalid Azure DevOps virtual package format: must be dev.azure.com/org/project/repo/path"
                    )
                repo_url = "/".join(parts[1:4])
        elif is_artifactory_path(parts[1:]):
            art_result = parse_artifactory_path(parts[1:])
            if art_result:
                repo_url = f"{art_result[1]}/{art_result[2]}"
        elif is_gitlab_hostname(parts[0]) and virtual_path:
            vparts = [p for p in virtual_path.split("/") if p]
            tail = len(vparts)
            if tail > 0 and len(parts) > 1 + tail:
                repo_url = "/".join(parts[1 : len(parts) - tail])
            else:
                repo_url = "/".join(parts[1:])
        else:
            repo_url = "/".join(parts[1:3])
    elif len(parts) >= 2:
        if not host:
            host = default_host()
        if validated_host and is_azure_devops_hostname(validated_host):
            if len(parts) < 4:
                raise ValueError(
                    "Invalid Azure DevOps virtual package format: expected at least org/project/repo/path"
                )
            repo_url = "/".join(parts[:3])
        else:
            repo_url = "/".join(parts[:2])

    return host, repo_url


@classmethod
def _resolve_shorthand_to_parsed_url(cls, repo_url, host):
    """Resolve a non-URL shorthand path into a ``urllib``-parsed URL.

    Handles ``user/repo``, ``github.com/user/repo``,
    ``dev.azure.com/org/project/repo``, and Artifactory VCS paths.
    Validates path components before returning.

    Returns:
        ``(parsed_url, host)``
    """
    parts = repo_url.split("/")

    if "_git" in parts:
        git_idx = parts.index("_git")
        parts = parts[:git_idx] + parts[git_idx + 1 :]

    if len(parts) >= 3 and is_supported_git_host(parts[0]):
        host = parts[0]
        if is_visualstudio_legacy_hostname(host) and len(parts) >= 3:
            # *.visualstudio.com/proj/repo: org is in the subdomain, path is proj/repo only
            user_repo = "/".join(parts[1:3])
        elif is_azure_devops_hostname(host) and len(parts) >= 4:
            # dev.azure.com/org/proj/repo: org is the first path segment
            user_repo = "/".join(parts[1:4])
        elif not is_github_hostname(host) and not is_azure_devops_hostname(host):
            if is_artifactory_path(parts[1:]):
                art_result = parse_artifactory_path(parts[1:])
                if art_result:
                    user_repo = f"{art_result[1]}/{art_result[2]}"
                else:
                    user_repo = "/".join(parts[1:])
            else:
                user_repo = "/".join(parts[1:])
        else:
            user_repo = "/".join(parts[1:])
    elif len(parts) >= 2 and "." not in parts[0]:
        if not host:
            host = default_host()
        if is_azure_devops_hostname(host) and len(parts) >= 3:
            user_repo = "/".join(parts[:3])
        elif host and not is_github_hostname(host) and not is_azure_devops_hostname(host):
            user_repo = "/".join(parts)
        else:
            user_repo = "/".join(parts[:2])
    else:
        raise ValueError(
            "Use 'user/repo' or 'github.com/user/repo' or 'dev.azure.com/org/project/repo' format"
        )

    if not user_repo or "/" not in user_repo:
        raise ValueError(
            f"Invalid repository format: {repo_url}. Expected 'user/repo' or 'org/project/repo'"
        )

    uparts = user_repo.split("/")
    is_ado_host = host and is_azure_devops_hostname(host)

    if is_ado_host:
        # *.visualstudio.com encodes org in subdomain -> proj/repo is sufficient (2 parts).
        # dev.azure.com encodes org in path -> org/proj/repo required (3 parts).
        min_ado_parts = 2 if is_visualstudio_legacy_hostname(host) else 3
        if len(uparts) < min_ado_parts:
            raise ValueError(
                f"Invalid Azure DevOps repository format: {repo_url}. Expected 'org/project/repo'"
            )
    elif len(uparts) < 2:
        raise ValueError(f"Invalid repository format: {repo_url}. Expected 'user/repo'")

    allowed_pattern = r"^[a-zA-Z0-9._\- ]+$" if is_ado_host else r"^[a-zA-Z0-9._-]+$"
    validate_path_segments("/".join(uparts), context="repository path")
    for part in uparts:
        if not re.match(allowed_pattern, part.rstrip(".git")):
            raise ValueError(f"Invalid repository path component: {part}")

    quoted_repo = "/".join(urllib.parse.quote(p, safe="") for p in uparts)
    github_url = urllib.parse.urljoin(f"https://{host}/", quoted_repo)
    parsed_url = urllib.parse.urlparse(github_url)

    return parsed_url, host
