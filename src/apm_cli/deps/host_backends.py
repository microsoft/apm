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

Concrete backend implementations live in the private sibling module
:mod:`._host_backend_impls` so that both files stay ≤ 500 lines.
All public names are re-exported from this module; importers need not
(and should not) reference ``_host_backend_impls`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..core.auth import HostInfo
from ..utils.github_host import default_host, is_github_hostname
from ._host_backend_impls import (
    ADOBackend,
    GenericGitBackend,
    GHECloudBackend,
    GHESBackend,
    GitHubBackend,
    GitLabBackend,
)

if TYPE_CHECKING:
    from ..core.auth import AuthResolver
    from ..models.apm_package import DependencyReference

__all__ = [
    "ADOBackend",
    "GHECloudBackend",
    "GHESBackend",
    "GenericGitBackend",
    "GitHubBackend",
    "GitLabBackend",
    "HostBackend",
    "backend_for",
    "backend_for_host",
]


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
# Dispatch
# ---------------------------------------------------------------------------


_BACKEND_BY_KIND: dict[str, type] = {
    "github": GitHubBackend,
    "ghe_cloud": GHECloudBackend,
    "ghes": GHESBackend,
    "ado": ADOBackend,
    "gitlab": GitLabBackend,
    "generic": GenericGitBackend,
}


def _resolve_fallback_cls_and_info(
    host: str,
    host_lower: str,
    info: object,
    port: int | None,
) -> tuple[type, HostInfo]:
    """Defensive fallback: route by hostname when classify_host returns no kind.

    Used when ``_BACKEND_BY_KIND.get(info.kind)`` yields ``None`` (mocked or
    future ``classify_host`` results that don't match a registered kind).
    Returns ``(backend_cls, host_info)`` ready for instantiation.
    """
    if is_github_hostname(host):
        if host_lower == "github.com":
            cls: type = GitHubBackend
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
        resolved_info: HostInfo = HostInfo(
            host=host,
            kind=kind,
            has_public_repos=host_lower == "github.com",
            api_base=api_base,
            port=port,
        )
    else:
        cls = GenericGitBackend
        if isinstance(info, HostInfo):
            resolved_info = info
        else:
            resolved_info = HostInfo(
                host=host,
                kind="generic",
                has_public_repos=False,
                api_base=f"https://{host}",
                port=port,
            )
    return cls, resolved_info


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
        cls, info = _resolve_fallback_cls_and_info(host, host_lower, info, port)
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
