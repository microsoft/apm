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

from ..utils.http_credentials import split_url_credentials, unfollowed_redirect_request

POLICY_FETCH_TIMEOUT = 10


def policy_get(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    ambient_credentials: bool = True,
) -> requests.Response:
    """GET *url*; return the response, or the unfollowed redirect response.

    Credentials embedded in *url* (``https://user:pass@host/...``) are sent as
    an explicit Basic header on that origin only.
    """
    url, url_credentials = split_url_credentials(url)
    return unfollowed_redirect_request(
        "GET",
        url,
        headers={**(headers or {}), **url_credentials},
        ambient_credentials=ambient_credentials,
        timeout=POLICY_FETCH_TIMEOUT,
    )
