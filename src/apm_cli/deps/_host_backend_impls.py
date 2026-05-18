"""Concrete HostBackend implementations for each supported vendor.

Private module -- consumers **must** import from the public
:mod:`apm_cli.deps.host_backends` module, not from here directly.
All classes defined here are re-exported from that public namespace.

Split from ``host_backends.py`` so both files stay ≤ 500 lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.auth import HostInfo
from ..utils.github_host import (
    build_ado_https_clone_url,
    build_ado_ssh_url,
    build_gitlab_https_clone_url,
    build_https_clone_url,
    build_ssh_url,
)

if TYPE_CHECKING:
    from ..models.apm_package import DependencyReference


# ---------------------------------------------------------------------------
# GitHub-family shared base
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GitHubFamilyBase:
    """Shared composition base for github.com / GHE Cloud / GHES backends.

    Not a Protocol implementer on its own -- concrete subclasses set
    ``kind`` and (optionally) override ``build_commits_api_url`` to use
    the right API base.

    NOTE: not exposed as ``HostBackend`` -- always use a concrete subclass.
    """

    host_info: HostInfo

    @property
    def is_github_family(self) -> bool:
        return True

    @property
    def is_generic(self) -> bool:
        return False

    def build_clone_https_url(
        self,
        dep_ref: DependencyReference,
        *,
        token: str | None,
        auth_scheme: str = "basic",
    ) -> str:
        # Bearer scheme is ADO-specific; GitHub family always uses basic.
        # Defensive: fall through to plain HTTPS without embedding the token.
        port = getattr(dep_ref, "port", None)
        embed_token = token if (token and auth_scheme != "bearer") else None
        return build_https_clone_url(
            self._url_host(dep_ref), dep_ref.repo_url, token=embed_token, port=port
        )

    def build_clone_ssh_url(self, dep_ref: DependencyReference) -> str:
        return build_ssh_url(
            self._url_host(dep_ref),
            dep_ref.repo_url,
            port=getattr(dep_ref, "port", None),
            user=getattr(dep_ref, "ssh_user", None) or "git",
        )

    def build_clone_http_url(self, dep_ref: DependencyReference) -> str:
        port = getattr(dep_ref, "port", None)
        host = self._url_host(dep_ref)
        netloc = f"{host}:{port}" if port else host
        return f"http://{netloc}/{dep_ref.repo_url}.git"

    def _url_host(self, dep_ref: DependencyReference) -> str:
        # Prefer the host carried on the dependency reference itself, but
        # fall back to ``host_info.host`` when the dep_ref has none. The
        # backend was already classified for this host, so the fallback
        # is safe and makes URL construction robust against partially
        # constructed DependencyReference objects.
        return getattr(dep_ref, "host", None) or self.host_info.host or ""

    def build_commits_api_url(self, dep_ref: DependencyReference, ref: str) -> str | None:
        # GitHub-family commits API: GET {api_base}/repos/{owner}/{repo}/commits/{ref}
        # api_base differs across github.com / *.ghe.com / GHES.
        try:
            owner, repo = dep_ref.repo_url.split("/", 1)
        except ValueError:
            return None
        # Treat already-resolved 40-char SHAs as a no-op -- caller should
        # short-circuit the network round-trip.
        if re.match(r"^[a-f0-9]{40}$", (ref or "").lower()):
            return None
        return f"{self.host_info.api_base}/repos/{owner}/{repo}/commits/{ref}"

    def build_contents_api_urls(
        self,
        owner: str,
        repo: str,
        file_path: str,
        ref: str,
    ) -> list[str]:
        # GitHub Contents API: a single canonical URL, no version negotiation.
        return [f"{self.host_info.api_base}/repos/{owner}/{repo}/contents/{file_path}?ref={ref}"]


# ---------------------------------------------------------------------------
# GitHub-family concrete backends
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitHubBackend(_GitHubFamilyBase):
    """Backend for github.com (the public GitHub Cloud host)."""

    @property
    def kind(self) -> str:
        return "github"


@dataclass(frozen=True)
class GHECloudBackend(_GitHubFamilyBase):
    """Backend for ``*.ghe.com`` (GitHub Enterprise Cloud -- Data Residency)."""

    @property
    def kind(self) -> str:
        return "ghe_cloud"


@dataclass(frozen=True)
class GHESBackend(_GitHubFamilyBase):
    """Backend for self-hosted GitHub Enterprise Server (configured via GITHUB_HOST)."""

    @property
    def kind(self) -> str:
        return "ghes"


# ---------------------------------------------------------------------------
# ADO backend
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ADOBackend:
    """Backend for Azure DevOps (cloud and on-prem server).

    ADO has its own URL builders that take ``ado_organization``,
    ``ado_project``, ``ado_repo`` triplets instead of a flat
    ``owner/repo``. Bearer-scheme tokens are injected via git env vars,
    not embedded in the URL -- the orchestrator handles that.
    """

    host_info: HostInfo

    @property
    def kind(self) -> str:
        return "ado"

    @property
    def is_github_family(self) -> bool:
        return False

    @property
    def is_generic(self) -> bool:
        return False

    def build_clone_https_url(
        self,
        dep_ref: DependencyReference,
        *,
        token: str | None,
        auth_scheme: str = "basic",
    ) -> str:
        # ADO's HTTPS host comes from the dependency itself; ``host_info``
        # is for classification only.
        host = getattr(dep_ref, "host", None) or self.host_info.host
        if not getattr(dep_ref, "ado_organization", None):
            raise ValueError(
                "ADO dependency is missing ado_organization; cannot construct clone URL"
            )
        # Bearer scheme: token goes into env vars, NOT into the URL.
        if auth_scheme == "bearer":
            return build_ado_https_clone_url(
                dep_ref.ado_organization,
                dep_ref.ado_project,
                dep_ref.ado_repo,
                token=None,
                host=host,
            )
        # Empty-string token => caller wants explicit "no credential in URL".
        embed_token = token if token else None
        return build_ado_https_clone_url(
            dep_ref.ado_organization,
            dep_ref.ado_project,
            dep_ref.ado_repo,
            token=embed_token,
            host=host,
        )

    def build_clone_ssh_url(self, dep_ref: DependencyReference) -> str:
        if not getattr(dep_ref, "ado_organization", None):
            raise ValueError(
                "ADO dependency is missing ado_organization; cannot construct clone URL"
            )
        return build_ado_ssh_url(dep_ref.ado_organization, dep_ref.ado_project, dep_ref.ado_repo)

    def build_clone_http_url(self, dep_ref: DependencyReference) -> str:
        # ADO does not support plain HTTP clones; surface a clear error
        # instead of building an URL that will fail with a confusing TLS
        # error several layers deeper.
        raise ValueError("Azure DevOps does not support plain HTTP cloning; use HTTPS or SSH.")

    def build_commits_api_url(self, dep_ref: DependencyReference, ref: str) -> str | None:
        # No GitHub-equivalent cheap commit-resolution endpoint is wired
        # for ADO; callers fall back to using ``ref`` directly.
        return None

    def build_contents_api_urls(
        self,
        owner: str,
        repo: str,
        file_path: str,
        ref: str,
    ) -> list[str]:
        # ADO file download goes through ``download_ado_file`` which uses
        # the ADO REST Items API, not the GitHub Contents API. Returning
        # an empty list signals "do not call the Contents API path" so
        # the orchestrator routes through the dedicated ADO method.
        return []


# ---------------------------------------------------------------------------
# GitLab backend
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitLabBackend:
    """Backend for GitLab (gitlab.com and self-managed instances).

    GitLab uses REST v4 for both commits and raw file fetches. Tokens are
    embedded in clone URLs as ``oauth2:<token>@`` (not GitHub's
    ``x-access-token``) when a PAT is available; otherwise falls back to
    plain HTTPS so git credential helpers can supply auth.
    """

    host_info: HostInfo

    @property
    def kind(self) -> str:
        return "gitlab"

    @property
    def is_github_family(self) -> bool:
        return False

    @property
    def is_generic(self) -> bool:
        # Token resolution paths treat GitLab the same as a generic host
        # (defer to credential helpers / GITLAB_* env vars), not as a
        # GitHub-family host.
        return True

    def build_clone_https_url(
        self,
        dep_ref: DependencyReference,
        *,
        token: str | None,
        auth_scheme: str = "basic",
    ) -> str:
        port = getattr(dep_ref, "port", None)
        host = getattr(dep_ref, "host", None) or self.host_info.host
        # Bearer scheme is ADO-only; embed PAT as oauth2 basic when given.
        if token and auth_scheme != "bearer":
            return build_gitlab_https_clone_url(host, dep_ref.repo_url, token, port=port)
        return build_https_clone_url(host, dep_ref.repo_url, token=None, port=port)

    def build_clone_ssh_url(self, dep_ref: DependencyReference) -> str:
        host = getattr(dep_ref, "host", None) or self.host_info.host
        return build_ssh_url(
            host,
            dep_ref.repo_url,
            port=getattr(dep_ref, "port", None),
            user=getattr(dep_ref, "ssh_user", None) or "git",
        )

    def build_clone_http_url(self, dep_ref: DependencyReference) -> str:
        port = getattr(dep_ref, "port", None)
        host = getattr(dep_ref, "host", None) or self.host_info.host
        netloc = f"{host}:{port}" if port else host
        return f"http://{netloc}/{dep_ref.repo_url}.git"

    def build_commits_api_url(self, dep_ref: DependencyReference, ref: str) -> str | None:
        # GitLab REST v4 commits endpoint: requires URL-encoded "namespace/project".
        if re.match(r"^[a-f0-9]{40}$", (ref or "").lower()):
            return None
        try:
            import urllib.parse as _up

            project = _up.quote(dep_ref.repo_url, safe="")
        except Exception:
            return None
        return f"{self.host_info.api_base}/projects/{project}/repository/commits/{ref}"

    def build_contents_api_urls(
        self,
        owner: str,
        repo: str,
        file_path: str,
        ref: str,
    ) -> list[str]:
        # GitLab raw file: GET /api/v4/projects/{id}/repository/files/{path}/raw?ref=...
        import urllib.parse as _up

        project = _up.quote(f"{owner}/{repo}", safe="")
        encoded_path = _up.quote(file_path, safe="")
        return [
            f"{self.host_info.api_base}/projects/{project}/repository/files/{encoded_path}/raw?ref={ref}"
        ]


# ---------------------------------------------------------------------------
# Generic git backend
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenericGitBackend:
    """Backend for non-GitHub non-ADO managed hosts (GitLab, Gitea, Gogs, Bitbucket).

    These hosts have heterogeneous APIs but support a common shape:
    HTTPS / SSH clones plus a Gitea-compatible Contents API at
    ``/api/v1/`` with a ``/api/v3/`` fallback for v3-only deployments.

    GitLab is currently classified as ``"generic"`` and accessed via the
    full repo URL (clone + sparse checkout), not the Contents API.
    """

    host_info: HostInfo

    @property
    def kind(self) -> str:
        return "generic"

    @property
    def is_github_family(self) -> bool:
        return False

    @property
    def is_generic(self) -> bool:
        return True

    def build_clone_https_url(
        self,
        dep_ref: DependencyReference,
        *,
        token: str | None,
        auth_scheme: str = "basic",
    ) -> str:
        # Generic hosts: never embed tokens in the URL. Auth comes from
        # git credential helpers. Bearer scheme is ADO-only.
        port = getattr(dep_ref, "port", None)
        host = getattr(dep_ref, "host", None) or self.host_info.host
        return build_https_clone_url(host, dep_ref.repo_url, token=None, port=port)

    def build_clone_ssh_url(self, dep_ref: DependencyReference) -> str:
        host = getattr(dep_ref, "host", None) or self.host_info.host
        return build_ssh_url(
            host,
            dep_ref.repo_url,
            port=getattr(dep_ref, "port", None),
            user=getattr(dep_ref, "ssh_user", None) or "git",
        )

    def build_clone_http_url(self, dep_ref: DependencyReference) -> str:
        port = getattr(dep_ref, "port", None)
        host = getattr(dep_ref, "host", None) or self.host_info.host
        netloc = f"{host}:{port}" if port else host
        return f"http://{netloc}/{dep_ref.repo_url}.git"

    def build_commits_api_url(self, dep_ref: DependencyReference, ref: str) -> str | None:
        # No standardized cheap commit-resolution endpoint across generic
        # hosts. Callers fall back to using ``ref`` directly.
        return None

    def build_contents_api_urls(
        self,
        owner: str,
        repo: str,
        file_path: str,
        ref: str,
    ) -> list[str]:
        # Gitea/Gogs Contents API: /api/v1/repos/{owner}/{repo}/contents/{file_path}?ref={ref}
        # Some legacy deployments only expose v3 (mirroring GitHub) -- try both.
        host = self.host_info.host
        return [
            f"https://{host}/api/v1/repos/{owner}/{repo}/contents/{file_path}?ref={ref}",
            f"https://{host}/api/v3/repos/{owner}/{repo}/contents/{file_path}?ref={ref}",
        ]
