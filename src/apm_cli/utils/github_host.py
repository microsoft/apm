"""Utilities for handling GitHub, GitHub Enterprise, Azure DevOps, and Artifactory hostnames and URLs."""

import os
import re
import urllib.parse

from ._github_host_artifactory import (
    build_artifactory_archive_url as build_artifactory_archive_url,
)
from ._github_host_artifactory import (
    is_artifactory_path as is_artifactory_path,
)
from ._github_host_artifactory import (
    iter_artifactory_boundary_candidates as iter_artifactory_boundary_candidates,
)
from ._github_host_artifactory import (
    parse_artifactory_path as parse_artifactory_path,
)


def default_host() -> str:
    """Return the default Git host (can be overridden via GITHUB_HOST env var)."""
    return os.environ.get("GITHUB_HOST", "github.com")


def is_azure_devops_hostname(hostname: str | None) -> bool:
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
    if h == "ssh.dev.azure.com":
        return True
    return bool(h.endswith(".visualstudio.com"))


def is_visualstudio_legacy_hostname(hostname: str | None) -> bool:
    """Return True if hostname is a legacy ``*.visualstudio.com`` ADO host.

    For these hosts the Azure DevOps organisation is encoded in the subdomain
    (e.g. ``myorg.visualstudio.com``) rather than as the first path segment.
    This is in contrast to ``dev.azure.com`` where the org is the first path
    segment (``dev.azure.com/org/project/repo``).
    """
    if not hostname:
        return False
    return hostname.lower().endswith(".visualstudio.com")


def is_gitlab_hostname(hostname: str | None) -> bool:
    """Return True if *hostname* is GitLab SaaS or a GitLab host from env configuration.

    Matches, in order of what this function checks (not full ``classify_host`` order):

    - ``gitlab.com`` (case-insensitive)
    - ``GITLAB_HOST`` — single self-managed host (same pattern as ``GITHUB_HOST`` for GHES)
    - ``APM_GITLAB_HOSTS`` — comma-separated list of self-managed hosts

    **GHES precedence:** If ``GITHUB_HOST`` matches *hostname* under the same
    rules as ``AuthResolver.classify_host`` (GHES, not ``gitlab.com`` SaaS),
    this returns ``False`` so GitLab env lists cannot claim an enterprise
    GitHub host.
    """
    if not hostname:
        return False
    h = hostname.strip().lower().split("/")[0]

    # GHES precedence: GITHUB_HOST match is enterprise GitHub, not GitLab, even if
    # the same host appears in GitLab env vars (GHES takes priority over any
    # GitLab environment hint).
    ghes_host = os.environ.get("GITHUB_HOST", "").strip().lower().split("/")[0]
    if (
        ghes_host
        and ghes_host == h
        and ghes_host not in {"github.com", "gitlab.com"}
        and not ghes_host.endswith(".ghe.com")
        and is_valid_fqdn(ghes_host)
    ):
        return False

    if h == "gitlab.com":
        return True
    gitlab_single = os.environ.get("GITLAB_HOST", "").strip().lower().split("/")[0]
    if gitlab_single and gitlab_single == h:
        return is_valid_fqdn(h)
    raw_list = os.environ.get("APM_GITLAB_HOSTS", "")
    for part in raw_list.split(","):
        entry = part.strip().lower().split("/")[0]
        if entry and entry == h and is_valid_fqdn(entry):
            return True
    return False


def has_github_gitlab_host_env_conflict(hostname: str | None) -> bool:
    """Return True when *hostname* is claimed as GHES via ``GITHUB_HOST`` and also as GitLab.

    Uses the same GHES-env match rules as :func:`is_gitlab_hostname` (GHES precedence
    block): ``GITHUB_HOST`` must be a valid FQDN, not ``github.com`` / ``gitlab.com``,
    and not ``*.ghe.com``. If that host is also ``GITLAB_HOST`` or listed in
    ``APM_GITLAB_HOSTS``, bare FQDN shorthand cannot be disambiguated without user action.

    This does **not** change GitLab vs GHES classification elsewhere.
    """
    if not hostname:
        return False
    h = hostname.strip().lower().split("/")[0]
    if not is_valid_fqdn(h):
        return False

    ghes_host = os.environ.get("GITHUB_HOST", "").strip().lower().split("/")[0]
    github_claims_as_ghes = (
        ghes_host
        and ghes_host == h
        and ghes_host not in {"github.com", "gitlab.com"}
        and not ghes_host.endswith(".ghe.com")
        and is_valid_fqdn(ghes_host)
    )
    if not github_claims_as_ghes:
        return False

    gitlab_single = os.environ.get("GITLAB_HOST", "").strip().lower().split("/")[0]
    if gitlab_single and gitlab_single == h and is_valid_fqdn(h):
        return True

    raw_list = os.environ.get("APM_GITLAB_HOSTS", "")
    for part in raw_list.split(","):
        entry = part.strip().lower().split("/")[0]
        if entry and entry == h and is_valid_fqdn(entry):
            return True

    return False


def format_github_gitlab_host_conflict_error(hostname: str) -> str:
    """Human-readable error when :func:`has_github_gitlab_host_env_conflict` is True."""
    return (
        f"Host '{hostname}' is configured as both GitHub Enterprise via GITHUB_HOST "
        f"and GitLab via GITLAB_HOST or APM_GITLAB_HOSTS. "
        f"APM cannot safely infer whether this shorthand is a nested repository path "
        f"or a repository plus package path.\n\n"
        "Use object form in apm.yml:\n"
        f"  - git: https://{hostname}/owner/repo\n"
        "    path: path/inside/repo\n\n"
        "Or run APM with GITHUB_HOST unset for this command only:\n"
        f"  env -u GITHUB_HOST GITLAB_HOST={hostname} apm install <package>"
    )


def maybe_raise_bare_fqdn_github_gitlab_conflict(raw: str) -> None:
    """Raise ``ValueError`` for ambiguous bare FQDN shorthand when GHES/GitLab envs conflict.

    Explicit ``https://``, ``http://``, ``ssh://``, ``git@``, and protocol-relative URLs
    are excluded. Only applies when there are at least three path segments after the host
    (same threshold as GitLab direct shorthand probing).
    """
    s = raw.strip()
    if "#" in s:
        s = s.rsplit("#", 1)[0].strip()
    if s.startswith(("git@", "https://", "http://", "ssh://", "//")):
        return
    if "/" not in s:
        return
    parts = [p for p in s.split("/") if p]
    # host + at least three segments → ambiguous nested repo vs repo + virtual path
    if len(parts) < 4:
        return
    host_cand = parts[0]
    if "." not in host_cand:
        return
    if not is_supported_git_host(host_cand):
        return
    if has_github_gitlab_host_env_conflict(host_cand):
        raise ValueError(format_github_gitlab_host_conflict_error(host_cand))


def is_github_hostname(hostname: str | None) -> bool:
    """Return True if hostname should be treated as GitHub (cloud or enterprise).

    Accepts ``github.com``, hosts that end with ``.ghe.com``, and any custom
    GitHub Enterprise Server host configured via the ``GITHUB_HOST`` env var.

    The ``GITHUB_HOST`` check mirrors the GHES detection in
    :meth:`~apm_cli.core.auth.AuthResolver.classify_host` so that parse-time
    host classification (used by ``_detect_virtual_package`` and
    ``_resolve_shorthand_to_parsed_url``) agrees with install-time auth
    routing.  Without this, FQDN shorthand with subpaths (e.g.
    ``ghe.example.com/org/repo/packages/skill``) embeds the subpath into the
    git URL instead of splitting into ``git:`` + ``path:``.
    """
    if not hostname:
        return False
    h = hostname.lower()
    if h == "github.com":
        return True
    if h.endswith(".ghe.com"):
        return True
    # GHES: GITHUB_HOST env var points to a custom GitHub Enterprise Server.
    # Use the same normalization as AuthResolver.classify_host() (.lower()
    # only, no .split("/")[0]) so both stages agree on which env values match.
    ghes_host = os.environ.get("GITHUB_HOST", "").lower()
    return bool(
        ghes_host
        and ghes_host == h
        and ghes_host not in {"github.com", "gitlab.com"}
        and not ghes_host.endswith(".ghe.com")
        and not is_azure_devops_hostname(ghes_host)
        and is_valid_fqdn(ghes_host)
    )


def is_supported_git_host(hostname: str | None) -> bool:
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
    return bool(is_valid_fqdn(hostname))


def unsupported_host_error(hostname: str, context: str | None = None) -> str:
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
    msg += "  # Linux/macOS:\n"
    msg += f"  export GITHUB_HOST={hostname}\n"
    msg += "\n"
    msg += "  # Windows (PowerShell):\n"
    msg += f'  $env:GITHUB_HOST = "{hostname}"\n'
    msg += "\n"
    msg += "  # Windows (Command Prompt):\n"
    msg += f"  set GITHUB_HOST={hostname}\n"

    return msg


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
    encoded_ref = urllib.parse.quote(ref, safe="")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{encoded_ref}/{file_path}"


_SSH_USER_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.+-]*$")
_SSH_USER_MAX_LEN = 64


def validate_ssh_user(user: str) -> str:
    """Validate an SSH username; return it unchanged or raise ``ValueError``.

    Allowlist policy (deliberately strict):

    - First character must be alphanumeric or underscore. This blocks
      SSH option injection vectors like ``-oProxyCommand=...`` from ever
      reaching ``git clone`` argv as a userinfo segment.
    - Remaining characters are letters, digits, ``.``, ``+``, ``-``, ``_``.
      This forbids ``/`` (path escape), ``@`` (double-userinfo confusion
      in ``ssh://user@host``), ``:`` (port confusion), and any whitespace
      or control character (log/ANSI injection).
    - Maximum length 64 bytes: long enough for any legitimate username
      and short enough to bound log size and reject buffer-abuse payloads.

    The shape matches the ``user`` group in ``SCP_LIKE_RE``
    (``cache/url_normalize.py``) so SCP-shorthand inputs that parsed
    successfully never fail this validation, while ``ssh://`` URLs (whose
    userinfo is percent-decoded by ``urllib.parse``) are still gated.
    """
    if not user:
        raise ValueError("SSH user must be a non-empty string")
    if len(user) > _SSH_USER_MAX_LEN:
        raise ValueError(f"SSH user is too long ({len(user)} > {_SSH_USER_MAX_LEN} chars)")
    if not _SSH_USER_RE.match(user):
        # Do NOT echo the raw user value -- a hostile apm.yml could embed
        # control characters that survive log emission. Show only the length.
        raise ValueError(
            f"Invalid SSH user (length {len(user)}). "
            "Allowed: alphanumerics, '.', '+', '-', '_'; "
            "must not start with '-'."
        )
    return user


def build_ssh_url(
    host: str,
    repo_ref: str,
    port: int | None = None,
    user: str = "git",
) -> str:
    """Build an SSH clone URL for the given host and repo_ref (owner/repo).

    When ``port`` is set, emit the explicit ``ssh://`` form because SCP
    shorthand (``git@host:path``) cannot carry a port — the ``:`` is the path
    separator. Without a port, keep the compact SCP shorthand (no behavioural
    change for the common case).

    ``user`` defaults to ``"git"`` for backward compatibility with public
    GitHub / GitLab / Bitbucket which all expect that fixed account name.
    Non-default usernames (EMU SSH accounts, self-hosted servers with a
    different bot user) are passed through after ``validate_ssh_user``.
    """
    safe_user = validate_ssh_user(user)
    if port:
        return f"ssh://{safe_user}@{host}:{port}/{repo_ref}.git"
    return f"{safe_user}@{host}:{repo_ref}.git"


def build_https_clone_url(
    host: str,
    repo_ref: str,
    token: str | None = None,
    port: int | None = None,
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


def build_gitlab_https_clone_url(
    host: str,
    repo_ref: str,
    token: str,
    port: int | None = None,
) -> str:
    """Build a GitLab-compatible HTTPS clone URL using oauth2 + PAT (not GitHub x-access-token).

    GitLab accepts personal or OAuth tokens as the password with username ``oauth2``.
    Values are URL-encoded so tokens may contain reserved characters.
    ``port`` is embedded in the netloc when set for self-managed GitLab HTTPS.

    Note: callers must avoid logging raw token-bearing URLs; use sanitizers on errors.
    """
    user = urllib.parse.quote("oauth2", safe="")
    password = urllib.parse.quote(token, safe="")
    netloc = f"{host}:{port}" if port else host
    return f"https://{user}:{password}@{netloc}/{repo_ref}.git"


# Azure DevOps URL builders


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

# SSH-specific auth failure signals from OpenSSH stderr.
# Covers: missing key, agent has no identities, host key mismatch, and
# explicit server rejection ("no more authentication methods to try" is the
# final line OpenSSH emits after exhausting all auth methods).
# NOTE: connectivity errors ("could not resolve hostname", "connection refused")
# are intentionally NOT listed here -- those are transient network/firewall
# failures, not auth failures, and must defer to the real download phase.
_SSH_AUTH_FAILURE_SIGNALS = (
    "permission denied",
    "publickey",
    "no more authentication methods",
    "host key verification failed",
    "no supported authentication methods",
    "too many authentication failures",
    "agent refused operation",
)

# SSH connectivity failure signals -- network/firewall errors that are NOT
# auth failures.  The preflight probe defers these (continues) so the real
# download phase can surface them with full diagnostics.
_SSH_CONNECTIVITY_SIGNALS = (
    "could not resolve hostname",
    "connection refused",
    "network is unreachable",
    "connection timed out",
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


def is_ssh_auth_failure_signal(text: str | None) -> bool:
    """Return True if ``text`` matches an SSH auth failure signal.

    Accepts raw stderr from ``subprocess.run`` (git ls-remote over SSH).
    Matches case-insensitively.

    Covers OpenSSH error messages for: missing or rejected public key,
    exhausted authentication methods, and host key mismatch.

    Does NOT match connectivity/network errors such as DNS resolution
    failures ("could not resolve hostname") or firewall blocks
    ("connection refused") -- those are transient network conditions, not
    auth failures, and must be left to the real download phase to surface.
    See ``_SSH_CONNECTIVITY_SIGNALS`` for the complementary set.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(signal in lowered for signal in _SSH_AUTH_FAILURE_SIGNALS)


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


def build_ado_api_url(
    org: str, project: str, repo: str, path: str, ref: str = "main", host: str = "dev.azure.com"
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
    api_host = "dev.azure.com" if host == "ssh.dev.azure.com" else host
    encoded_path = urllib.parse.quote(path, safe="")
    quoted_org = urllib.parse.quote(org, safe="")
    quoted_project = urllib.parse.quote(project, safe="")
    quoted_repo = urllib.parse.quote(repo, safe="")
    quoted_ref = urllib.parse.quote(ref, safe="")
    org_path = "" if is_visualstudio_legacy_hostname(api_host) else f"{quoted_org}/"
    return (
        f"https://{api_host}/{org_path}{quoted_project}/_apis/git/repositories/{quoted_repo}/items"
        f"?path={encoded_path}&versionDescriptor.version={quoted_ref}&api-version=7.0"
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

    hostname = hostname.split("/")[0]  # Remove any path components

    # Single regex to validate all FQDN rules:
    # - Starts with alphanumeric
    # - Labels only contain alphanumeric and hyphens
    # - Labels don't start/end with hyphens
    # - At least two labels (one dot)
    pattern = (
        r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?)+$"
    )
    return bool(re.match(pattern, hostname))


def sanitize_token_url_in_message(message: str, host: str | None = None) -> str:
    """Sanitize occurrences of token-bearing https URLs for the given host in message.

    If host is None, default_host() is used. Replaces https://<anything>@host with https://***@host
    """
    if not host:
        host = default_host()

    # Escape host for regex
    host_re = re.escape(host)
    pattern = rf"https://[^@\s]+@{host_re}"
    return re.sub(pattern, f"https://***@{host}", message)
