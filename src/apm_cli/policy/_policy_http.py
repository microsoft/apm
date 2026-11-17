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

from ..utils.http_credentials import (
    CredentialScopedSession,
    RedirectRefusedError,
    origin_bounded_request,
)

POLICY_FETCH_TIMEOUT = 10


def policy_get(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    ambient_credentials: bool = True,
) -> requests.Response:
    """GET *url*; return the response, or the unfollowed redirect response."""
    with CredentialScopedSession() as session:
        try:
            return origin_bounded_request(
                session,
                "GET",
                url,
                base_url=url,
                headers=headers,
                ambient_credentials=ambient_credentials,
                timeout=POLICY_FETCH_TIMEOUT,
            )
        except RedirectRefusedError as refused:
            if refused.response is None:
                raise
            return refused.response
