"""Azure DevOps URL builders/parsers extracted from :mod:`apm_cli.utils.github_host`.

Extracted to keep ``github_host.py`` under the 800-line threshold (issue
#1078 Stage 2) while preserving 100% behavioural equivalence.  This module
is private (``_`` prefix).  All public names are re-exported from
``github_host.py`` so ``apm_cli.utils.github_host.NAME`` continues to
resolve correctly.

Rule B note: unlike ``_github_host_artifactory``, the functions here DO
reference module-level names that tests monkeypatch on ``github_host``
(``is_azure_devops_hostname``, ``is_visualstudio_legacy_hostname``).  They
are therefore resolved late, through the facade module object, so a
``monkeypatch.setattr("apm_cli.utils.github_host.is_azure_devops_hostname", ...)``
is still observed here.
"""

from __future__ import annotations

import urllib.parse


def _gh():
    """Return the ``github_host`` facade module (late, for Rule B)."""
    from . import github_host

    return github_host


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
    org_path = "" if _gh().is_visualstudio_legacy_hostname(api_host) else f"{quoted_org}/"
    return (
        f"https://{api_host}/{org_path}{quoted_project}/_apis/git/repositories/{quoted_repo}/items"
        f"?path={encoded_path}&versionDescriptor.version={quoted_ref}&api-version=7.0"
    )


def parse_ado_repo_url(url: str | None) -> tuple[str, str, str] | None:
    """Decompose an Azure DevOps repo URL into ``(org, project, repo)``.

    Accepts the standard ``_git`` clone shape on both ADO hostnames:

    - ``https://dev.azure.com/{org}/{project}/_git/{repo}`` (org in path)
    - ``https://{org}.visualstudio.com/{project}/_git/{repo}`` (org in subdomain)

    A trailing ``.git`` and any segments after ``{repo}`` are ignored. Path
    segments are percent-decoded. Returns ``None`` when the URL is not an ADO
    host, lacks the ``_git`` marker, or cannot be decomposed.
    """
    if not url:
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    hostname = parsed.hostname or ""
    if not _gh().is_azure_devops_hostname(hostname):
        return None

    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    segments = [urllib.parse.unquote(s) for s in path.split("/") if s]
    if "_git" not in segments:
        return None
    git_idx = segments.index("_git")
    if git_idx + 1 >= len(segments):
        return None
    repo = segments[git_idx + 1]
    before = segments[:git_idx]

    if _gh().is_visualstudio_legacy_hostname(hostname):
        # Legacy ``*.visualstudio.com``: org lives in the subdomain.
        org, project = hostname.split(".")[0], (before[-1] if before else "")
    else:
        # ``dev.azure.com``: org and project both precede ``_git``.
        org, project = (before[0], before[1]) if len(before) >= 2 else ("", "")

    if not (org and project and repo):
        return None
    return org, project, repo
