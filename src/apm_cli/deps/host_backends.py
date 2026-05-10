"""Vendor-specific URL/API construction for remote git hosts.

Replaces the conditional `if is_github / elif is_ado / else generic` ladders
that used to live in ``download_strategies.build_repo_url`` and the various
``download_*`` methods on :class:`GitHubPackageDownloader`. Each supported
host kind is represented by a small immutable backend object that exposes
URL builders, API URLs, and capability flags. A dispatch function picks the
right backend by consulting :meth:`AuthResolver.classify_host`.

Pattern: Strategy via Protocol + dispatch dict. The three GitHub-family
backends (GitHub, GHE Cloud, GHES) share URL builders through a small
``_GitHubFamilyBase`` to avoid copy/paste; ADO and Generic stand alone.
There is no runtime registry. Adding a new vendor is one new class plus
one new entry in ``_BACKEND_BY_KIND``, never a new branch in an
``if/elif`` ladder.

Design constraints (see plan in WIP/host-backends-refactor):

- Backends are stateless: each carries only its :class:`HostInfo`. Tokens,
  auth contexts, ports, and ssh/https-or-http preferences flow as method
  arguments so the same backend instance can serve every dependency on a
  given host.
- ``build_clone_*`` returns a clone URL suitable for ``git clone``. Bearer
  tokens are NOT embedded in the URL -- they are injected via git env vars
  by ``download_strategies``; the backend signals this via
  ``auth_scheme="bearer"``.
- ``build_commits_api_url`` returns ``None`` for hosts where no cheap
  commit-resolution endpoint exists (ADO, generic). Callers fall back to
  the explicit ref string in that case.
- ``build_contents_api_urls`` returns an ordered list of API URL
  candidates. Generic (Gitea/Gogs) hosts return v1 *and* v3 candidates
  for negotiation; GitHub family returns exactly one URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..core.auth import HostInfo
from ..utils.github_host import (
    build_ado_https_clone_url,
    build_ado_ssh_url,
    build_https_clone_url,
    build_ssh_url,
    default_host,
    is_github_hostname,
)

if TYPE_CHECKING:
    from ..core.auth import AuthResolver
    from ..models.apm_package import DependencyReference


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class HostBackend(Protocol):
    """Vendor-specific URL/API construction for one remote git host kind.

    All concrete backends are immutable dataclasses carrying just the
    :class:`HostInfo` describing the host. Methods take whatever runtime
    inputs they need (dep_ref, token, auth_scheme) so a single backend
    instance can serve many dependencies on the same host.
    """

    host_info: HostInfo

    @property
    def kind(self) -> str:
        """Host kind: ``"github"``, ``"ghe_cloud"``, ``"ghes"``, ``"ado"``, or ``"generic"``."""
        ...

    @property
    def is_github_family(self) -> bool:
        """True for github.com, *.ghe.com, and configured GHES hosts."""
        ...

    @property
    def is_generic(self) -> bool:
        """True for non-GitHub-family non-ADO hosts (GitLab, Bitbucket, Gitea, ...).

        Used by :meth:`GitHubPackageDownloader._resolve_dep_token` to decide
        whether to defer to git credential helpers instead of using a
        pre-resolved token.
        """
        ...

    def build_clone_https_url(
        self,
        dep_ref: DependencyReference,
        *,
        token: str | None,
        auth_scheme: str = "basic",
    ) -> str:
        """Build the HTTPS clone URL.

        ``token`` may be ``None`` (anonymous), a non-empty string (basic auth
        embedded in URL), or the empty string ``""`` (explicitly suppress
        per-instance default -- used by transport plans for plain HTTPS).

        ``auth_scheme="bearer"`` indicates the token will be injected via
        git env vars; the URL must NOT embed credentials in this case.
        """
        ...

    def build_clone_ssh_url(self, dep_ref: DependencyReference) -> str:
        """Build the SSH clone URL."""
        ...

    def build_clone_http_url(self, dep_ref: DependencyReference) -> str:
        """Build a plain HTTP (insecure) clone URL.

        Only used when ``dep_ref.is_insecure`` is true; APM never
        downgrades automatically. ADO raises ValueError because Azure
        DevOps does not accept HTTP at all.
        """
        ...

    def build_commits_api_url(self, dep_ref: DependencyReference, ref: str) -> str | None:
        """Build the URL for the cheap commit-resolution API.

        Returns ``None`` when the host has no equivalent endpoint (ADO,
        generic). Callers then fall back to using ``ref`` directly.
        """
        ...

    def build_contents_api_urls(
        self,
        owner: str,
        repo: str,
        file_path: str,
        ref: str,
    ) -> list[str]:
        """Return ordered Contents-API URL candidates for fetching a file.

        GitHub family returns exactly one URL.  Generic hosts (Gitea/Gogs)
        return v1 then v3 candidates so callers can negotiate the API
        version on 404.
        """
        ...


# ---------------------------------------------------------------------------
# Concrete backends
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
        return build_ssh_url(host, dep_ref.repo_url, port=getattr(dep_ref, "port", None))

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


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


_BACKEND_BY_KIND: dict[str, type] = {
    "github": GitHubBackend,
    "ghe_cloud": GHECloudBackend,
    "ghes": GHESBackend,
    "ado": ADOBackend,
    "generic": GenericGitBackend,
}


def backend_for(
    dep_ref: DependencyReference | None,
    auth_resolver: AuthResolver,
    *,
    fallback_host: str | None = None,
) -> HostBackend:
    """Pick the right :class:`HostBackend` for *dep_ref*.

    ``auth_resolver.classify_host`` is the single source of truth for
    host kind classification -- this function is a thin dispatch layer
    that wraps the resulting :class:`HostInfo` in a backend object.

    Args:
        dep_ref: The dependency reference. ``None`` is allowed for
            instance-default resolution (uses ``fallback_host`` or
            :func:`default_host`).
        auth_resolver: The :class:`AuthResolver` instance. Used solely
            for the static :meth:`classify_host` method -- no auth
            resolution side effects.
        fallback_host: Host to use when ``dep_ref`` is ``None`` or has
            no host. Defaults to :func:`default_host`.

    Returns:
        The :class:`HostBackend` for the resolved host.
    """
    if dep_ref is not None and dep_ref.host:
        host = dep_ref.host
        port = getattr(dep_ref, "port", None)
    else:
        host = fallback_host or default_host()
        port = None

    # ADO short-circuit: when ``dep_ref`` itself reports Azure DevOps the
    # backend is unambiguous regardless of ``classify_host`` (which may be
    # mocked or defective in tests/diagnostic paths).
    if dep_ref is not None:
        try:
            if dep_ref.is_azure_devops():
                info = auth_resolver.classify_host(host, port=port)
                if not isinstance(info, HostInfo):
                    info = HostInfo(
                        host=host,
                        kind="ado",
                        has_public_repos=False,
                        api_base=f"https://{host}",
                        port=port,
                    )
                return ADOBackend(host_info=info)
        except (AttributeError, TypeError):
            pass

    info = auth_resolver.classify_host(host, port=port)
    cls: type | None = None
    if isinstance(info, HostInfo):
        cls = _BACKEND_BY_KIND.get(info.kind)
    if cls is None:
        # Defensive fallback path for mocked / future ``classify_host``
        # results: route by hostname so callers that wire only a partial
        # mock (typical in unit tests) still get the right backend.
        host_lower = (host or "").lower()
        if is_github_hostname(host):
            if host_lower == "github.com":
                cls = GitHubBackend
                kind = "github"
                api_base = "https://api.github.com"
            elif host_lower.endswith(".ghe.com"):
                cls = GHECloudBackend
                kind = "ghe_cloud"
                api_base = f"https://{host}/api/v3"
            else:
                cls = GHESBackend
                kind = "ghes"
                api_base = f"https://{host}/api/v3"
            info = HostInfo(
                host=host,
                kind=kind,
                has_public_repos=host_lower == "github.com",
                api_base=api_base,
                port=port,
            )
        else:
            cls = GenericGitBackend
            if not isinstance(info, HostInfo):
                info = HostInfo(
                    host=host,
                    kind="generic",
                    has_public_repos=False,
                    api_base=f"https://{host}",
                    port=port,
                )
    return cls(host_info=info)


def backend_for_host(
    host: str,
    auth_resolver: AuthResolver,
    *,
    port: int | None = None,
) -> HostBackend:
    """Pick the right :class:`HostBackend` for a bare hostname.

    Variant of :func:`backend_for` for callers that have a host string
    but no :class:`DependencyReference` (e.g. registry probes, marketplace
    builder).
    """
    info = auth_resolver.classify_host(host, port=port)
    cls = _BACKEND_BY_KIND.get(info.kind, GenericGitBackend)
    return cls(host_info=info)
