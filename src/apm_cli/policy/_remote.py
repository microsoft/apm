"""Git remote-URL identity parsing for org-policy discovery.

Split out of ``discovery.py`` (which grew past the file-length ratchet): the
canonical splitter :func:`_remote_url_parts` and the host-specific
:func:`_parse_remote_url` live here. ``discovery.py`` re-exports both and reads
the ``origin`` remote via its own ``_git_remote_origin_url``; the architecture
guard ``install-deployment-policy-remote-origin-owner`` keeps these the single
owners of git-remote reading and URL parsing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import urlparse

from ..cache.url_normalize import SCP_LIKE_RE
from ..utils.git_env import get_git_executable
from ..utils.github_host import (
    is_azure_devops_hostname,
    is_visualstudio_legacy_hostname,
    parse_ado_repo_url,
)


class _Unset:
    """Sentinel type: an argument was not provided (distinct from ``None``)."""


_UNSET = _Unset()


def _git_remote_origin_url(project_root: Path) -> str | None:
    """Return the ``origin`` remote URL, or ``None`` when unavailable.

    Canonical reader of the project's ``origin`` remote, shared by
    :func:`_extract_org_host_port_from_git_remote` and GitLab subgroup
    discovery so the ``git remote get-url`` subprocess lives in one place.
    """
    try:
        result = subprocess.run(
            [get_git_executable(), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=project_root,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _extract_org_host_port_from_git_remote(
    project_root: Path,
    *,
    remote_url: str | None | _Unset = _UNSET,
) -> tuple[str, str, int | None] | None:
    """Extract ``(org, host, port)`` from git remote origin.

    ``remote_url`` lets a caller that already read ``origin`` (e.g.
    ``discovery._auto_discover``) pass it in so the ``git remote get-url``
    subprocess runs exactly once per discovery. When left unset the origin is
    read here; an explicit ``None`` means "no remote" and is not re-read.
    """
    if isinstance(remote_url, _Unset):
        remote_url = _git_remote_origin_url(project_root)
    if not remote_url:
        return None
    try:
        parsed_identity = _parse_remote_url(remote_url)
    except ValueError:
        return None
    if parsed_identity is None:
        return None
    port = None
    if "://" in remote_url:
        try:
            port = urlparse(remote_url).port
        except ValueError:
            return None
    return parsed_identity[0], parsed_identity[1], port


def _extract_org_from_git_remote(
    project_root: Path,
) -> tuple[str, str] | None:
    """Extract (org, host) from git remote origin URL.

    Handles:
    - https://github.com/contoso/my-project.git -> ("contoso", "github.com")
    - git@github.com:contoso/my-project.git -> ("contoso", "github.com")
    - https://github.example.com/contoso/my-project.git -> ("contoso", "github.example.com")
    """
    identity = _extract_org_host_port_from_git_remote(project_root)
    return (identity[0], identity[1]) if identity is not None else None


def _remote_url_parts(url: str) -> tuple[str, list[str]] | None:
    """Split a git remote URL into ``(host, path_segments)``.

    Canonical splitter shared by :func:`_parse_remote_url` (which applies
    host-specific org interpretation on top) and GitLab subgroup discovery
    (:func:`discovery._gitlab_namespace_descending`). Handles SCP-like SSH URLs
    with any username (not just ``git@``) and ``scheme://`` URLs. Path segments
    are cleaned of empty parts and the trailing ``.git`` suffix; no host-specific
    interpretation (ADO ``v3/`` prefix, visualstudio subdomain, ...) is applied
    here -- that stays the caller's responsibility.

    Returns ``None`` when the URL cannot be split.
    """
    if not url:
        return None

    # SCP-like SSH: <user>@<host>:<path> -- any user, not just `git`.
    # Closes #1159 for non-`git` SSH users (EMU, custom GHE accounts).
    scp_match = SCP_LIKE_RE.match(url)
    if scp_match:
        host = scp_match.group("host")
        path_part = scp_match.group("path")
        segments = [p for p in path_part.rstrip("/").removesuffix(".git").split("/") if p]
        if not host or not segments:
            return None
        return (host, segments)

    # HTTPS: https://github.com/owner/repo.git
    if "://" in url:
        try:
            parsed = urlparse(url)
        except Exception:
            # urlparse only raises ValueError in practice, but the legacy
            # parser swallowed any exception here; preserve that. The
            # ``/tfs/`` ValueError comes from parse_ado_repo_url (in
            # _parse_remote_url), never from urlparse, so nothing to re-raise.
            return None
        host = parsed.hostname or ""
        segments = [
            p for p in parsed.path.strip("/").removesuffix(".git").rstrip("/").split("/") if p
        ]
        if not host or not segments:
            return None
        return (host, segments)

    return None


def _parse_remote_url(url: str) -> tuple[str, str] | None:
    """Parse a git remote URL into (org, host).

    Accepts SCP-style SSH URLs with any username (not just ``git@``), so
    EMU/GHE deployments that use a non-``git`` SSH user
    (e.g. ``enterprise-user@ghe.corp.com:org/repo.git``) parse correctly.
    Also handles Azure DevOps SSH URLs which carry an extra ``v3/``
    path prefix (``git@ssh.dev.azure.com:v3/<org>/<project>/<repo>``).

    Returns None if URL can't be parsed.
    """
    parts = _remote_url_parts(url)
    if parts is None:
        return None
    host, segments = parts

    # SCP-like SSH: <user>@<host>:<path> -- any user, not just `git`.
    if SCP_LIKE_RE.match(url):
        # Azure DevOps SSH carries a leading 'v3/' segment that is
        # NOT the org. The org is the second segment.
        if host == "ssh.dev.azure.com" and segments[0] == "v3" and len(segments) >= 2:
            return (segments[1], host)
        return (segments[0], host)

    # HTTPS: https://github.com/owner/repo.git
    # ADO:   https://dev.azure.com/org/project/_git/repo
    try:
        if is_azure_devops_hostname(host):
            ado_coordinates = parse_ado_repo_url(url)
            if ado_coordinates is None:
                return None
            return ado_coordinates[0], host
        if is_visualstudio_legacy_hostname(host):
            return (host[: -len(".visualstudio.com")], host)
        return (segments[0], host)
    except ValueError as exc:
        if "mounted below '/tfs/'" in str(exc):
            raise
        return None
    except Exception:
        return None
