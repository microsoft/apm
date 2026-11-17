"""Keep HTTP credential headers on the origin they were built for.

``requests`` drops only ``Authorization`` when a redirect changes origin.
Provider-specific credential headers (GitLab ``PRIVATE-TOKEN``) would
otherwise follow the redirect, so every session that sends them strips the
whole set through :func:`strip_cross_origin_credential_headers`.
"""

from __future__ import annotations

import requests

CREDENTIAL_HEADER_NAMES: tuple[str, ...] = ("Authorization", "PRIVATE-TOKEN")


def strip_cross_origin_credential_headers(
    session: requests.Session,
    prepared_request: requests.PreparedRequest,
    response: requests.Response,
) -> None:
    """Remove credential headers when a redirect leaves the original origin."""
    if not session.should_strip_auth(response.request.url, prepared_request.url):
        return
    for name in CREDENTIAL_HEADER_NAMES:
        prepared_request.headers.pop(name, None)


class CredentialScopedSession(requests.Session):
    """Requests session whose credential headers never cross origins."""

    def rebuild_auth(
        self,
        prepared_request: requests.PreparedRequest,
        response: requests.Response,
    ) -> None:
        """Strip every credential header across origins, then apply defaults."""
        strip_cross_origin_credential_headers(self, prepared_request, response)
        super().rebuild_auth(prepared_request, response)
