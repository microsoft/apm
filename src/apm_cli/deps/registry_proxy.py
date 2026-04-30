"""Registry-agnostic proxy configuration for APM.

Provides a ``RegistryConfig`` abstraction over VCS proxies (Artifactory,
Nexus, Gitea, etc.) that sit in front of GitHub/GitLab to enable
air-gapped and enterprise-proxy installs.

Environment variables (canonical)::

    PROXY_REGISTRY_URL      -- Full proxy base URL, e.g.
                            ``https://art.example.com/artifactory/github``
    PROXY_REGISTRY_TOKEN    -- Bearer token for the proxy.
    PROXY_REGISTRY_ONLY     -- Set to ``1``/``true``/``yes`` to block all
                            direct VCS downloads.

Deprecated aliases (still functional, emit ``DeprecationWarning``)::

    ARTIFACTORY_BASE_URL  -> PROXY_REGISTRY_URL
    ARTIFACTORY_APM_TOKEN -> PROXY_REGISTRY_TOKEN
    ARTIFACTORY_ONLY      -> PROXY_REGISTRY_ONLY
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Protocol, runtime_checkable  # noqa: F401, UP035
from urllib.parse import urlparse

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency


# ---------------------------------------------------------------------------
# RegistryConfig
# ---------------------------------------------------------------------------


@runtime_checkable
class RegistryClient(Protocol):
    """Interface for registry proxy backends.

    Each backend (Artifactory, Nexus, etc.) implements this protocol so
    the download pipeline can fetch files without knowing which registry
    type is in use.
    """

    def fetch_file(
        self,
        owner: str,
        repo: str,
        file_path: str,
        ref: str = "main",
        resilient_get: Callable | None = None,
    ) -> bytes | None:
        """Fetch a single file from the registry.

        Returns raw file bytes on success, or ``None`` when the file
        cannot be fetched (caller should fall back to full-archive
        download).
        """
        ...


@dataclass(frozen=True)
class RegistryConfig:
    """Immutable registry proxy configuration parsed from environment variables.

    Use :meth:`from_env` to construct; do not instantiate directly.

    Attributes
    ----------
    url:
        Full proxy base URL including the path prefix,
        e.g. ``"https://art.example.com/artifactory/github"``.
    host:
        Pure FQDN extracted from *url*,
        e.g. ``"art.example.com"``.
        Suitable for :func:`~apm_cli.core.auth.AuthResolver.classify_host`.
    prefix:
        URL path prefix extracted from *url*,
        e.g. ``"artifactory/github"``.
        Used when constructing download URLs.
    scheme:
        ``"https"`` or ``"http"``.
    token:
        Optional Bearer token for authenticating against the proxy.
    enforce_only:
        When ``True``, direct VCS downloads are blocked -- only the proxy
        may serve packages.
    """

    url: str
    host: str
    prefix: str
    scheme: str
    token: str | None
    enforce_only: bool

    # -- factory ------------------------------------------------------------

    @classmethod
    def from_env(cls) -> RegistryConfig | None:
        """Build a :class:`RegistryConfig` from the current environment.

        Reads the canonical ``PROXY_REGISTRY_*`` variables first; falls
        back to the deprecated ``ARTIFACTORY_*`` aliases (with a
        ``DeprecationWarning`` for each one that is used).

        Returns ``None`` when no registry URL is configured.
        """
        url = os.environ.get("PROXY_REGISTRY_URL", "").strip().rstrip("/")
        if not url:
            art_url = os.environ.get("ARTIFACTORY_BASE_URL", "").strip().rstrip("/")
            if art_url:
                warnings.warn(
                    "ARTIFACTORY_BASE_URL is deprecated; use PROXY_REGISTRY_URL instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                url = art_url

        if not url:
            return None

        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http"):
            return None
        host = parsed.hostname
        prefix = parsed.path.strip("/")
        if not host or not prefix:
            return None

        token = os.environ.get("PROXY_REGISTRY_TOKEN") or _read_deprecated_token()

        enforce_str = os.environ.get("PROXY_REGISTRY_ONLY", "")
        if not enforce_str:
            enforce_str = _read_deprecated_enforce_only()
        enforce_only = enforce_str.strip().lower() in ("1", "true", "yes")

        return cls(
            url=url,
            host=host,
            prefix=prefix,
            scheme=parsed.scheme,
            token=token,
            enforce_only=enforce_only,
        )

    # -- helpers ------------------------------------------------------------

    def get_headers(self) -> dict:
        """Return HTTP headers for authenticating against this registry."""
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def get_client(self) -> RegistryClient:
        """Return a :class:`RegistryClient` for this configuration.

        Currently returns an Artifactory backend.  When additional
        registry types are needed, this method can inspect the URL or
        a configuration hint to select the right backend.
        """
        from .artifactory_entry import ArtifactoryRegistryClient

        return ArtifactoryRegistryClient(config=self)

    def validate_lockfile_deps(self, locked_deps: list[LockedDependency]) -> list[LockedDependency]:
        """Return locked dependencies that conflict with registry-only mode.

        A *conflict* is a non-local dependency whose host is a direct VCS
        source (``github.com``, GHE Cloud ``*.ghe.com``, or a GHES host
        configured via ``GITHUB_HOST``) while :attr:`enforce_only` is
        ``True``.

        Uses :meth:`~apm_cli.core.auth.AuthResolver.classify_host` rather
        than ad hoc string matching so that GHE Cloud, GHES, and ADO hosts
        are handled correctly.

        Args:
            locked_deps: The list of :class:`LockedDependency` objects from
                an existing lockfile.

        Returns:
            List of :class:`LockedDependency` objects that conflict.  Empty
            when ``enforce_only`` is ``False`` or no conflicts exist.
        """
        if not self.enforce_only:
            return []

        from apm_cli.core.auth import AuthResolver

        conflicts: list[LockedDependency] = []
        for dep in locked_deps:
            if dep.source == "local":
                continue
            host = dep.host or "github.com"
            host_info = AuthResolver.classify_host(host)
            if host_info.kind in ("github", "ghe_cloud", "ghes"):
                conflicts.append(dep)
        return conflicts

    def find_missing_hashes(self, locked_deps: list[LockedDependency]) -> list[LockedDependency]:
        """Return registry-proxy entries that lack a ``content_hash``.

        A missing hash on a proxy entry means a tampered lockfile
        could redirect downloads without detection.  Callers should
        warn or error when this list is non-empty.
        """
        missing: list[LockedDependency] = []
        for dep in locked_deps:
            if dep.source == "local":
                continue
            if dep.registry_prefix and not dep.content_hash:
                missing.append(dep)
        return missing


# ---------------------------------------------------------------------------
# Convenience helper: read enforce-only flag (canonical or deprecated)
# ---------------------------------------------------------------------------


def is_enforce_only() -> bool:
    """Return ``True`` when registry-only mode is active.

    Checks ``PROXY_REGISTRY_ONLY`` first; falls back to the deprecated
    ``ARTIFACTORY_ONLY``.  Does **not** require a full :class:`RegistryConfig`
    to be available -- callers that only need the flag (e.g.
    :class:`~apm_cli.deps.github_downloader.GitHubPackageDownloader`) can
    use this without constructing the full config.
    """
    val = os.environ.get("PROXY_REGISTRY_ONLY", "").strip()
    if not val:
        val = os.environ.get("ARTIFACTORY_ONLY", "").strip()
    return val.lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Private helpers for deprecated env var reading
# ---------------------------------------------------------------------------


def _read_deprecated_token() -> str | None:
    token = os.environ.get("ARTIFACTORY_APM_TOKEN")
    if token:
        warnings.warn(
            "ARTIFACTORY_APM_TOKEN is deprecated; use PROXY_REGISTRY_TOKEN instead.",
            DeprecationWarning,
            stacklevel=3,
        )
    return token


def _read_deprecated_enforce_only() -> str:
    val = os.environ.get("ARTIFACTORY_ONLY", "")
    if val:
        warnings.warn(
            "ARTIFACTORY_ONLY is deprecated; use PROXY_REGISTRY_ONLY instead.",
            DeprecationWarning,
            stacklevel=3,
        )
    return val
