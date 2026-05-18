"""DependencyReference model  -- core dependency representation and parsing."""

import re
import urllib.parse

from ....utils.github_host import (
    default_host,
    is_artifactory_path,
    is_azure_devops_hostname,
    is_github_hostname,
    is_gitlab_hostname,
    is_supported_git_host,
    is_visualstudio_legacy_hostname,
    parse_artifactory_path,
)
from ....utils.path_security import (
    validate_path_segments,
)

# Default ports per URI scheme -- used to normalise away redundant
# explicit ports (e.g. https://host:443/...) so that lockfile keys
# and error messages stay consistent regardless of how the user
# spelled the URL.
_DEFAULT_SCHEME_PORTS: dict[str, int] = {"https": 443, "http": 80, "ssh": 22}


@classmethod
@classmethod
def _resolve_ado_virtual_shorthand(cls, parts: list[str], validated_host: str | None) -> str:
    """Resolve ADO virtual shorthand to base repo path."""
    if is_visualstudio_legacy_hostname(parts[0]):
        if len(parts) < 4:
            raise ValueError(
                "Invalid Azure DevOps virtual package format: must be "
                "myorg.visualstudio.com/project/repo/path"
            )
        return "/".join(parts[1:3])
    # dev.azure.com/org/proj/repo/path: org in path
    if len(parts) < 5:
        raise ValueError(
            "Invalid Azure DevOps virtual package format: must be dev.azure.com/org/project/repo/path"
        )
    return "/".join(parts[1:4])


@classmethod
@classmethod
def _resolve_gitlab_virtual_shorthand(cls, parts: list[str], virtual_path: str | None) -> str:
    """Resolve GitLab virtual shorthand to base repo path."""
    if not virtual_path:
        return "/".join(parts[1:])
    vparts = [p for p in virtual_path.split("/") if p]
    tail = len(vparts)
    if tail > 0 and len(parts) > 1 + tail:
        return "/".join(parts[1 : len(parts) - tail])
    return "/".join(parts[1:])


@classmethod
@classmethod
def _resolve_explicit_host_virtual(
    cls, parts: list[str], validated_host: str | None, virtual_path: str | None
) -> tuple[str, str]:
    """Resolve virtual shorthand when explicit host is present.

    Returns:
        (host, repo_url)
    """
    host = parts[0]
    if is_azure_devops_hostname(parts[0]):
        repo_url = cls._resolve_ado_virtual_shorthand(parts, validated_host)
    elif is_artifactory_path(parts[1:]):
        art_result = parse_artifactory_path(parts[1:])
        if art_result:
            repo_url = f"{art_result[1]}/{art_result[2]}"
        else:
            repo_url = "/".join(parts[1:3])
    elif is_gitlab_hostname(parts[0]) and virtual_path:
        repo_url = cls._resolve_gitlab_virtual_shorthand(parts, virtual_path)
    else:
        repo_url = "/".join(parts[1:3])
    return host, repo_url


@classmethod
@classmethod
def _resolve_implicit_host_virtual(
    cls, parts: list[str], validated_host: str | None
) -> tuple[str, str]:
    """Resolve virtual shorthand when host is implicit.

    Returns:
        (host, repo_url)
    """
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
        host, repo_url = cls._resolve_explicit_host_virtual(parts, validated_host, virtual_path)
    elif len(parts) >= 2:
        host, repo_url = cls._resolve_implicit_host_virtual(parts, validated_host)

    return host, repo_url


@classmethod
def _extract_user_repo_from_parts(cls, parts: list[str], host: str) -> str:
    """Extract user/repo path from URL parts based on host type."""
    if is_visualstudio_legacy_hostname(host) and len(parts) >= 3:
        return "/".join(parts[1:3])
    if is_azure_devops_hostname(host) and len(parts) >= 4:
        return "/".join(parts[1:4])
    if not is_github_hostname(host) and not is_azure_devops_hostname(host):
        if is_artifactory_path(parts[1:]):
            art_result = parse_artifactory_path(parts[1:])
            if art_result:
                return f"{art_result[1]}/{art_result[2]}"
        return "/".join(parts[1:])
    return "/".join(parts[1:])


@classmethod
def _extract_user_repo_shorthand(cls, parts: list[str], host: str) -> str:
    """Extract user/repo path from shorthand parts."""
    if is_azure_devops_hostname(host) and len(parts) >= 3:
        return "/".join(parts[:3])
    if host and not is_github_hostname(host) and not is_azure_devops_hostname(host):
        return "/".join(parts)
    return "/".join(parts[:2])


@classmethod
def _validate_user_repo_format(cls, user_repo: str, repo_url: str, host: str) -> None:
    """Validate the extracted user/repo format and path components."""
    if not user_repo or "/" not in user_repo:
        raise ValueError(
            f"Invalid repository format: {repo_url}. Expected 'user/repo' or 'org/project/repo'"
        )

    uparts = user_repo.split("/")
    is_ado_host = host and is_azure_devops_hostname(host)

    if is_ado_host:
        min_ado_parts = 2 if is_visualstudio_legacy_hostname(host) else 3
        if len(uparts) < min_ado_parts:
            raise ValueError(
                f"Invalid Azure DevOps repository format: {repo_url}. Expected 'org/project/repo'"
            )
    elif len(uparts) < 2:
        raise ValueError(f"Invalid repository format: {repo_url}. Expected 'user/repo'")

    allowed_pattern = r"^[a-zA-Z0-9._\- ]+$" if is_ado_host else r"^[a-zA-Z0-9._~-]+$"
    validate_path_segments("/".join(uparts), context="repository path")
    for part in uparts:
        if not re.match(allowed_pattern, part.rstrip(".git")):
            raise ValueError(f"Invalid repository path component: {part}")


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
        user_repo = cls._extract_user_repo_from_parts(parts, host)
    elif len(parts) >= 2 and "." not in parts[0]:
        if not host:
            host = default_host()
        user_repo = cls._extract_user_repo_shorthand(parts, host)
    else:
        raise ValueError(
            "Use 'user/repo' or 'github.com/user/repo' or 'dev.azure.com/org/project/repo' format"
        )

    cls._validate_user_repo_format(user_repo, repo_url, host)

    quoted_repo = "/".join(urllib.parse.quote(p, safe="") for p in user_repo.split("/"))
    github_url = urllib.parse.urljoin(f"https://{host}/", quoted_repo)
    parsed_url = urllib.parse.urlparse(github_url)

    return parsed_url, host
