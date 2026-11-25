"""Single HTTP entry point for org-policy fetches.

Every policy fetch (URL, GitHub, Azure DevOps, GitLab) refuses redirects
away from the requested resource. The follow decision belongs to
:func:`~apm_cli.utils.http_credentials.origin_bounded_request`; an unfollowed
redirect comes back as its 3xx response, so callers report it and the auth
chain can classify a sign-in redirect.
"""

from __future__ import annotations

from collections.abc import Mapping

import requests

from ..deps.egress_route import EgressRoute, EgressSurface, is_direct_exception, route_for
from ..utils.http_credentials import (
    origin_label,
    split_url_credentials,
    unfollowed_redirect_request,
)

POLICY_FETCH_TIMEOUT = 10

_announced_direct_origins: set[str] = set()


def _announce_direct_policy_route(url: str) -> None:
    """Say once per origin that registry-only mode fetches org policy directly."""
    if not is_direct_exception(EgressSurface.POLICY_FETCH):
        return
    origin = origin_label(url)
    if origin in _announced_direct_origins:
        return
    _announced_direct_origins.add(origin)
    from ..utils.console import _rich_info

    _rich_info(
        f"PROXY_REGISTRY_ONLY is set: org policy is fetched directly from {origin}"
        " (the governance channel does not go through the proxy)",
        symbol="info",
    )


def policy_get(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    ambient_credentials: bool = True,
) -> requests.Response:
    """GET *url*; return the response, or the unfollowed redirect response.

    Credentials embedded in *url* (``https://user:pass@host/...``) are sent as
    an explicit Basic header on that origin only. The route comes from the
    egress-route owner, which keeps this governance channel direct in
    registry-only mode.
    """
    from ..core.policy_fetch_ledger import note_fetch_attempt

    note_fetch_attempt("http")
    route = route_for(EgressSurface.POLICY_FETCH)
    if route is not EgressRoute.DIRECT:
        raise requests.RequestException(f"policy fetch route is {route.value}")
    url, url_credentials = split_url_credentials(url)
    _announce_direct_policy_route(url)
    return unfollowed_redirect_request(
        "GET",
        url,
        headers={**(headers or {}), **url_credentials},
        ambient_credentials=ambient_credentials,
        timeout=POLICY_FETCH_TIMEOUT,
    )
