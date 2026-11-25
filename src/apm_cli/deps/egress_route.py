"""Route of every outbound request: the ``egress-route-policy`` owner.

Each outbound surface asks :func:`route_for` how its request may leave the
machine. Registry-only mode (``PROXY_REGISTRY_ONLY``) is decided here, from
one closed table, instead of at each call site:

* ``DIRECT`` -- the surface may contact its own origin.
* ``PROXY`` -- dependency content comes only through ``PROXY_REGISTRY_URL``;
  a direct request to the source host is not sent.
* ``REFUSED`` -- the request is not sent at all.

Outside registry-only mode every surface is ``DIRECT``; whether a configured
proxy still serves a download is the registry-proxy router's choice
(:class:`~apm_cli.deps.artifactory_orchestrator.ArtifactoryRouter`).

The org policy fetch stays ``DIRECT`` in registry-only mode by decision: it is
the governance channel, documented in ``enterprise/registry-proxy``. An
explicit ``apm self-update`` stays ``DIRECT`` too (a user-initiated fetch of
APM itself); the automatic version check is not sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType


class EgressSurface(Enum):
    """Closed set of outbound request kinds that registry-only mode governs."""

    SOURCE_ARCHIVE = "source-archive"
    """Package content (archive, clone, or single file) from the source host."""

    SOURCE_PROBE = "source-probe"
    """Existence or credential probe against the source host."""

    SOURCE_ENUMERATION = "source-enumeration"
    """Ref, tag, or commit listing against the source host."""

    MCP_REGISTRY = "mcp-registry"
    """MCP server metadata lookup."""

    MARKETPLACE_FILE = "marketplace-file"
    """Marketplace catalog or plugin manifest file."""

    POLICY_FETCH = "policy-fetch"
    """Org ``apm-policy.yml`` fetch (the governance channel)."""

    VERSION_CHECK = "version-check"
    """Automatic check for a newer APM release, run before a command."""

    SELF_UPDATE = "self-update"
    """Explicit ``apm self-update``: APM release metadata and installer."""


class EgressRoute(Enum):
    """How one request may leave the machine."""

    DIRECT = "direct"
    PROXY = "proxy"
    REFUSED = "refused"


# Registry-only routes. Every surface has exactly one entry. The two DIRECT
# entries are explicit exceptions, not omissions: POLICY_FETCH is the
# governance channel, and SELF_UPDATE is a user-initiated fetch of APM itself.
# The automatic VERSION_CHECK is not user-initiated, so it is not sent.
_REGISTRY_ONLY_ROUTES: Mapping[EgressSurface, EgressRoute] = MappingProxyType(
    {
        EgressSurface.SOURCE_ARCHIVE: EgressRoute.PROXY,
        EgressSurface.SOURCE_PROBE: EgressRoute.REFUSED,
        EgressSurface.SOURCE_ENUMERATION: EgressRoute.REFUSED,
        EgressSurface.MCP_REGISTRY: EgressRoute.REFUSED,
        EgressSurface.MARKETPLACE_FILE: EgressRoute.PROXY,
        EgressSurface.POLICY_FETCH: EgressRoute.DIRECT,
        EgressSurface.VERSION_CHECK: EgressRoute.REFUSED,
        EgressSurface.SELF_UPDATE: EgressRoute.DIRECT,
    }
)


def _registry_only_active() -> bool:
    """Return whether registry-only mode is configured for this process."""
    from . import registry_proxy

    return registry_proxy.is_enforce_only()


def route_for(surface: EgressSurface, *, operator_mirror: bool = False) -> EgressRoute:
    """Return the route *surface* must take.

    ``operator_mirror`` marks a request whose endpoint the operator configured
    as the sanctioned mirror (for example ``MCP_REGISTRY_URL``); it may go
    direct to that endpoint in registry-only mode.
    """
    if not _registry_only_active() or operator_mirror:
        return EgressRoute.DIRECT
    return _REGISTRY_ONLY_ROUTES[surface]


def is_direct_exception(surface: EgressSurface) -> bool:
    """Return whether *surface* goes direct only as a registry-only exception.

    Callers announce such a route so a proxy-only operator sees the one
    request that does not go through the proxy.
    """
    return _registry_only_active() and _REGISTRY_ONLY_ROUTES[surface] is EgressRoute.DIRECT


def is_refused(surface: EgressSurface, *, operator_mirror: bool = False) -> bool:
    """Return whether *surface* may not be sent at all."""
    return route_for(surface, operator_mirror=operator_mirror) is EgressRoute.REFUSED


def is_proxy_only(surface: EgressSurface) -> bool:
    """Return whether *surface* may reach dependency content only through the proxy."""
    return route_for(surface) is EgressRoute.PROXY


__all__ = [
    "EgressRoute",
    "EgressSurface",
    "is_direct_exception",
    "is_proxy_only",
    "is_refused",
    "route_for",
]
