"""Utilities for handling GitHub, GitHub Enterprise, Azure DevOps, and Artifactory hostnames and URLs."""

import os
import re
import urllib.parse
from typing import Optional


def default_host() -> str:
    """Return the default Git host (can be overridden via GITHUB_HOST env var)."""
    return os.environ.get("GITHUB_HOST", "github.com")


def is_azure_devops_hostname(hostname: Optional[str]) -> bool:
    """Return True if hostname is Azure DevOps (cloud or server).
    
    Accepts:
    - dev.azure.com (Azure DevOps Services)
    - *.visualstudio.com (legacy Azure DevOps URLs)
    - Custom Azure DevOps Server hostnames are supported via GITHUB_HOST env var
    """
    if not hostname:
        return False
    h = hostname.lower()
    if h == "dev.azure.com":
        return True
    if h.endswith(".visualstudio.com"):
        return True
    return False


def is_github_hostname(hostname: Optional[str]) -> bool:
    """Return True if hostname should be treated as GitHub (cloud or enterprise).

    Accepts 'github.com' and hosts that end with '.ghe.com'.
    
    Note: This is primarily for internal hostname classification.
    APM accepts any Git host via FQDN syntax without validation.
    """
    if not hostname:
        return False
    h = hostname.lower()
    if h == "github.com":
        return True
    if h.endswith(".ghe.com"):
        return True
    return False


def is_supported_git_host(hostname: Optional[str]) -> bool:
    """Return True if hostname is a supported Git hosting platform.
    
    Supports:
    - GitHub.com
    - GitHub Enterprise (*.ghe.com)
    - Azure DevOps Services (dev.azure.com)
    - Azure DevOps legacy (*.visualstudio.com)
    - Any FQDN set via GITHUB_HOST environment variable
    - Any valid FQDN (generic git host support for GitLab, Bitbucket, etc.)
    """
    if not hostname:
        return False
    
    # Check GitHub hosts
    if is_github_hostname(hostname):
        return True
    
    # Check Azure DevOps hosts
    if is_azure_devops_hostname(hostname):
        return True
    
    # Accept the configured default host (supports custom Azure DevOps Server, etc.)
    configured_host = os.environ.get("GITHUB_HOST", "").lower()
    if configured_host and hostname.lower() == configured_host:
        return True
    
    # Accept any valid FQDN as a generic git host (GitLab, Bitbucket, self-hosted, etc.)
    if is_valid_fqdn(hostname):
        return True
    
    return False


def unsupported_host_error(hostname: str, context: Optional[str] = None) -> str:
    """Generate an actionable error message for unsupported Git hosts.
    
    Args:
        hostname: The hostname that was rejected
        context: Optional context message (e.g., "Protocol-relative URLs are not supported")
    
    Returns:
        str: A user-friendly error message with fix instructions
    """
    current_host = os.environ.get("GITHUB_HOST", "")
    
    msg = ""
    if context:
        msg += f"{context}\n\n"
    
    msg += f"Invalid Git host: '{hostname}'.\n"
    msg += "\n"
    msg += "APM supports any valid FQDN as a Git host, including:\n"
    msg += "  * github.com\n"
    msg += "  * *.ghe.com (GitHub Enterprise Cloud)\n"
    msg += "  * dev.azure.com, *.visualstudio.com (Azure DevOps)\n"
    msg += "  * gitlab.com, bitbucket.org, or any self-hosted Git server\n"
    msg += "\n"
    
    if current_host:
        msg += f"Your GITHUB_HOST is set to: '{current_host}'\n"
        msg += f"But you're trying to use: '{hostname}'\n"
        msg += "\n"
    
    msg += f"To use '{hostname}', set the GITHUB_HOST environment variable:\n"
    msg += "\n"
    msg += f"  # Linux/macOS:\n"
    msg += f"  export GITHUB_HOST={hostname}\n"
    msg += "\n"
    msg += f"  # Windows (PowerShell):\n"
    msg += f'  $env:GITHUB_HOST = "{hostname}"\n'
    msg += "\n"
    msg += f"  # Windows (Command Prompt):\n"
    msg += f"  set GITHUB_HOST={hostname}\n"
    
    return msg


from urllib.parse import quote as url_quote


def build_raw_content_url(owner: str, repo: str, ref: str, file_path: str) -> str:
    """Build a raw.githubusercontent.com URL for fetching file content.

    This CDN endpoint is not subject to the GitHub REST API rate limit and
    does not require authentication for public repositories.

    Only valid for github.com — GitHub Enterprise Server and GHE Cloud Data
    Residency hosts do not have a ``raw.githubusercontent.com`` equivalent.

    Args:
        owner: Repository owner (user or organisation)
        repo: Repository name
        ref: Git reference (branch, tag, or commit SHA)
        file_path: Path to file within the repository

    Returns:
        str: ``https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{file_path}``
    """
    encoded_ref = url_quote(ref, safe='')
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{encoded_ref}/{file_path}"


def build_ssh_url(host: str, repo_ref: str, port: Optional[int] = None) -> str:
    """Build an SSH clone URL for the given host and repo_ref (owner/repo).

    When ``port`` is set, emit the explicit ``ssh://`` form because SCP
    shorthand (``git@host:path``) cannot carry a port — the ``:`` is the path
    separator. Without a port, keep the compact SCP shorthand (no behavioural
    change for the common case).
    """
    if port:
        return f"ssh://git@{host}:{port}/{repo_ref}.git"
    return f"git@{host}:{repo_ref}.git"


def build_https_clone_url(
    host: str,
    repo_ref: str,
    token: Optional[str] = None,
    port: Optional[int] = None,
) -> str:
    """Build an HTTPS clone URL. If token provided, use x-access-token format (no escaping done).

    ``port`` is embedded in the netloc (``host:port``) when set so custom
    HTTPS ports (e.g. self-hosted Git servers on 8443) are preserved.

    Note: callers must avoid logging raw token-bearing URLs.
    """
    netloc = f"{host}:{port}" if port else host
    if token:
        # Use x-access-token format which is compatible with GitHub Enterprise and GH Actions
        return f"https://x-access-token:{token}@{netloc}/{repo_ref}.git"
    return f"https://{netloc}/{repo_ref}"


# Azure DevOps URL builders

def build_ado_https_clone_url(org: str, project: str, repo: str, token: Optional[str] = None, host: str = "dev.azure.com") -> str:
    """Build Azure DevOps HTTPS clone URL.
    
    Azure DevOps accepts PAT as password with any username, or as bearer token.
    The standard format is: https://dev.azure.com/{org}/{project}/_git/{repo}
    
    Args:
        org: Azure DevOps organization name
        project: Azure DevOps project name
        repo: Repository name
        token: Optional Personal Access Token for authentication
        host: Azure DevOps host (default: dev.azure.com)
    
    Returns:
        str: HTTPS clone URL for Azure DevOps
    """
    quoted_project = urllib.parse.quote(project, safe='')
    if token:
        # ADO uses PAT as password with empty username
        return f"https://{token}@{host}/{org}/{quoted_project}/_git/{repo}"
    return f"https://{host}/{org}/{quoted_project}/_git/{repo}"


def build_ado_ssh_url(org: str, project: str, repo: str, host: str = "ssh.dev.azure.com") -> str:
    """Build Azure DevOps SSH clone URL for cloud or server.
    
    For Azure DevOps Services (cloud):
        git@ssh.dev.azure.com:v3/{org}/{project}/{repo}
    
    For Azure DevOps Server (on-premises):
        ssh://git@{host}/{org}/{project}/_git/{repo}
    
    Args:
        org: Azure DevOps organization name
        project: Azure DevOps project name  
        repo: Repository name
        host: SSH host (default: ssh.dev.azure.com for cloud; set to your server for on-prem)
    
    Returns:
        str: SSH clone URL for Azure DevOps
    """
    quoted_project = urllib.parse.quote(project, safe='')
    if host == "ssh.dev.azure.com":
        # Cloud format
        return f"git@ssh.dev.azure.com:v3/{org}/{quoted_project}/{repo}"
    else:
        # Server format (user@host is optional, but commonly 'git@host')
        return f"ssh://git@{host}/{org}/{quoted_project}/_git/{repo}"


def build_ado_api_url(org: str, project: str, repo: str, path: str, ref: str = "main", host: str = "dev.azure.com") -> str:
    """Build Azure DevOps REST API URL for file contents.
    
    API format: https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}/items
    
    Args:
        org: Azure DevOps organization name
        project: Azure DevOps project name
        repo: Repository name
        path: Path to file within the repository
        ref: Git reference (branch, tag, or commit). Defaults to "main"
        host: Azure DevOps host (default: dev.azure.com)
    
    Returns:
        str: API URL for retrieving file contents
    """
    encoded_path = urllib.parse.quote(path, safe='')
    quoted_project = urllib.parse.quote(project, safe='')
    return (
        f"https://{host}/{org}/{quoted_project}/_apis/git/repositories/{repo}/items"
        f"?path={encoded_path}&versionDescriptor.version={ref}&api-version=7.0"
    )


def is_artifactory_path(path_segments: list) -> bool:
    """Return True if path segments indicate a JFrog Artifactory VCS repository.

    Artifactory VCS paths follow the pattern: artifactory/{repo-key}/{owner}/{repo}
    Detection: first segment is 'artifactory' and there are at least 4 segments.
    """
    return (len(path_segments) >= 4
            and path_segments[0].lower() == 'artifactory')


def parse_artifactory_path(path_segments: list) -> tuple:
    """Parse Artifactory path into (prefix, owner, repo, virtual_path).

    Input:  ['artifactory', 'github', 'microsoft', 'apm-sample-package']
    Output: ('artifactory/github', 'microsoft', 'apm-sample-package', None)

    Input:  ['artifactory', 'github', 'owner', 'repo', 'skills', 'review']
    Output: ('artifactory/github', 'owner', 'repo', 'skills/review')

    Returns None if not a valid Artifactory path.
    """
    if not is_artifactory_path(path_segments):
        return None
    repo_key = path_segments[1]
    remaining = path_segments[2:]
    prefix = f"artifactory/{repo_key}"
    owner = remaining[0]
    repo = remaining[1]
    virtual_path = '/'.join(remaining[2:]) if len(remaining) > 2 else None
    return (prefix, owner, repo, virtual_path)


def build_artifactory_archive_url(host: str, prefix: str, owner: str, repo: str, ref: str = "main", scheme: str = "https") -> tuple:
    """Build Artifactory VCS archive download URLs.

    Returns a tuple of URLs to try in order.  Because Artifactory proxies
    the upstream server's native URL scheme, we attempt GitHub-style,
    GitLab-style, and codeload.github.com-style archive paths so the caller
    does not need to know what sits behind the Artifactory remote repository.

    Organizations using private GitHub repositories must configure their
    Artifactory upstream as ``codeload.github.com`` (instead of ``github.com``)
    because Artifactory cannot follow GitHub's cross-host redirect (which
    carries short-lived tokens) to codeload.  When the upstream is
    ``codeload.github.com``, the required archive path is
    ``/{owner}/{repo}/zip/refs/heads/{ref}`` (no ``.zip`` extension).

    Args:
        host: Artifactory hostname (e.g., 'artifactory.example.com')
        prefix: Artifactory path prefix (e.g., 'artifactory/github')
        owner: Repository owner
        repo: Repository name
        ref: Git reference (branch or tag name)
        scheme: URL scheme (default 'https'; 'http' for local dev proxies)

    Returns:
        Tuple of URLs to try in order
    """
    base = f"{scheme}://{host}/{prefix}/{owner}/{repo}"
    return (
        # GitHub-style: /archive/refs/heads/{ref}.zip
        f"{base}/archive/refs/heads/{ref}.zip",
        # GitLab-style: /-/archive/{ref}/{repo}-{ref}.zip
        f"{base}/-/archive/{ref}/{repo}-{ref}.zip",
        # GitHub-style tags fallback
        f"{base}/archive/refs/tags/{ref}.zip",
        # codeload.github.com-style: /zip/refs/heads/{ref}
        # Required when Artifactory upstream is configured as codeload.github.com
        # (workaround for private repos where github.com redirects to codeload with tokens
        # that Artifactory cannot follow across hosts)
        f"{base}/zip/refs/heads/{ref}",
        f"{base}/zip/refs/tags/{ref}",
    )


def is_valid_fqdn(hostname: str) -> bool:
    """Validate if a string is a valid Fully Qualified Domain Name (FQDN).

    Args:
        hostname: The hostname string to validate

    Returns:
        bool: True if the hostname is a valid FQDN, False otherwise

    Valid FQDN must:
    - Contain labels separated by dots
    - Labels must contain only alphanumeric chars and hyphens
    - Labels must not start or end with hyphens
    - Have at least one dot
    """
    if not hostname:
        return False
    
    hostname = hostname.split('/')[0]  # Remove any path components
    
    # Single regex to validate all FQDN rules:
    # - Starts with alphanumeric
    # - Labels only contain alphanumeric and hyphens
    # - Labels don't start/end with hyphens
    # - At least two labels (one dot)
    pattern = r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?)+$"
    return bool(re.match(pattern, hostname))


def sanitize_token_url_in_message(message: str, host: Optional[str] = None) -> str:
    """Sanitize occurrences of token-bearing https URLs for the given host in message.

    If host is None, default_host() is used. Replaces https://<anything>@host with https://***@host
    """
    if not host:
        host = default_host()

    # Escape host for regex
    host_re = re.escape(host)
    pattern = rf"https://[^@\s]+@{host_re}"
    return re.sub(pattern, f"https://***@{host}", message)
