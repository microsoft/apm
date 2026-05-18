"""Azure DevOps URL builders, auth helpers, and auth-failure signal detection.

Private module — import via ``apm_cli.utils.github_host`` for the public API.
All symbols here are re-exported from that module so callers do not need to
reference this path directly.
"""

import urllib.parse


def build_ado_https_clone_url(
    org: str, project: str, repo: str, token: str | None = None, host: str = "dev.azure.com"
) -> str:
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
    quoted_project = urllib.parse.quote(project, safe="")
    if token:
        # ADO uses PAT as password with empty username
        return f"https://{token}@{host}/{org}/{quoted_project}/_git/{repo}"
    return f"https://{host}/{org}/{quoted_project}/_git/{repo}"


def build_authorization_header_git_env(scheme: str, credential: str) -> dict:
    """Build env vars to inject an HTTP Authorization header into git operations.

    Uses git's GIT_CONFIG_COUNT/KEY_N/VALUE_N mechanism to set
    ``http.extraheader`` via the environment, NOT via a ``-c`` command-line
    flag.  Command-line flags appear in the OS process table and may be
    captured by host-level monitoring; environment variables are private
    to the spawned process.

    The returned dict is intended to be merged into a base env (e.g.
    ``os.environ.copy()``) before being passed to ``Repo.clone_from(env=...)``
    or ``subprocess.run(..., env=...)``.

    Args:
        scheme: HTTP auth scheme, e.g. ``"Bearer"`` or ``"Basic"``.
        credential: The credential value (token or base64-encoded user:pass).

    Returns:
        dict: ``{GIT_CONFIG_COUNT, GIT_CONFIG_KEY_0, GIT_CONFIG_VALUE_0}``.

    Note:
        Callers MUST NOT log the returned dict.  ``GIT_CONFIG_VALUE_0``
        contains the credential.
    """
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: {scheme} {credential}",
    }


def build_ado_bearer_git_env(bearer_token: str) -> dict:
    """Build env vars to authenticate to Azure DevOps with an Entra ID bearer.

    Azure DevOps accepts AAD bearer tokens anywhere a PAT is accepted.  AAD
    JWTs are typically 1.5-2.5KB which exceeds safe URL-embedding limits
    and would leak into git's own logs and the OS process table.  Header
    injection avoids both issues.

    Args:
        bearer_token: An AAD JWT scoped to the ADO resource GUID
            ``499b84ac-1321-427f-aa17-267ca6975798``.

    Returns:
        dict: env-var overlay for the spawned git subprocess.
    """
    return build_authorization_header_git_env("Bearer", bearer_token)


# Single source of truth for the ADO auth-failure signal set.
#
# Historically these signal strings were open-coded across 3+ call sites
# (pipeline._preflight_auth_check, github_downloader.list_remote_refs,
# github_downloader._execute_transport_plan, auth._try_ado_bearer_fallback)
# and drifted: the auth.py and github_downloader.py variants were missing
# "403" and "could not read username", causing #1212 (preflight failed to
# trigger bearer fallback on stale-PAT 403 / interactive-prompt-blocked
# scenarios). Consolidating here prevents that recurring drift.
#
# All five signals are union-required for ADO PAT->bearer eligibility:
#   "401"                       canonical HTTP auth failure
#   "403"                       PAT scope/permission rejection (ADO returns 403)
#   "authentication failed"     git's stderr text on credential rejection
#   "unauthorized"              libcurl synonym, capitalization varies by version
#   "could not read username"   GIT_TERMINAL_PROMPT=0 + invalid creds
_ADO_AUTH_FAILURE_SIGNALS = (
    "401",
    "403",
    "authentication failed",
    "unauthorized",
    "could not read username",
)


def is_ado_auth_failure_signal(text: str | None) -> bool:
    """Return True if ``text`` matches an ADO auth-failure signal.

    Accepts raw stderr from ``subprocess.run`` or ``str(GitCommandError)``.
    Matches case-insensitively; libcurl error capitalization has changed
    across versions (curl 7.x vs 8.x), so callers must not rely on case.

    Callers MUST gate bearer-fallback eligibility on additional context
    (host is ADO, scheme is "basic", a token was actually presented) --
    this predicate only answers the "looks like an auth failure" question.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(signal in lowered for signal in _ADO_AUTH_FAILURE_SIGNALS)


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
    quoted_project = urllib.parse.quote(project, safe="")
    if host == "ssh.dev.azure.com":
        # Cloud format
        return f"git@ssh.dev.azure.com:v3/{org}/{quoted_project}/{repo}"
    else:
        # Server format (user@host is optional, but commonly 'git@host')
        return f"ssh://git@{host}/{org}/{quoted_project}/_git/{repo}"


from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _AdoApiOpts:
    """Options for Azure DevOps API URL building."""

    ref: str = "main"
    host: str = "dev.azure.com"


def build_ado_api_url(
    org: str,
    project: str,
    repo: str,
    path: str,
    ref: str = "main",
    *,
    host: str = "dev.azure.com",
) -> str:
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
    encoded_path = urllib.parse.quote(path, safe="")
    quoted_project = urllib.parse.quote(project, safe="")
    return (
        f"https://{host}/{org}/{quoted_project}/_apis/git/repositories/{repo}/items"
        f"?path={encoded_path}&versionDescriptor.version={ref}&api-version=7.0"
    )
