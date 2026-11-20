"""Keep HTTP credentials on the origin they were built for.

Two decisions live here:

* Header scope on auto-followed redirects. ``requests`` drops only
  ``Authorization`` when a redirect changes origin. Provider-specific
  credential headers (GitLab ``PRIVATE-TOKEN``) would otherwise follow the
  redirect, so every session that sends them strips the whole set through
  :func:`strip_cross_origin_credential_headers`.
* The redirect follow decision. :func:`origin_bounded_request` sends every
  hop with ``allow_redirects=False`` and follows a redirect only when the
  normalized target keeps the configured scheme, host and effective port and
  stays under the configured base path. Credentials are re-sent only on those
  in-bound hops, and ambient ``.netrc`` credentials never replace them.
"""

from __future__ import annotations

import base64
import contextlib
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

import requests

CREDENTIAL_HEADER_NAMES: tuple[str, ...] = (
    "Authorization",
    "PRIVATE-TOKEN",
    "Proxy-Authorization",
)

REDIRECT_STATUS_CODES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECT_HOPS = 3

# Refusal reasons carried by RedirectRefusedError.reason.
REFUSED_OUTSIDE_ORIGIN = "outside-origin"
REFUSED_OUTSIDE_BASE_PATH = "outside-base-path"
REFUSED_HTTPS_DOWNGRADE = "https-downgrade"
REFUSED_USERINFO = "userinfo"
REFUSED_UNSUPPORTED_LOCATION = "unsupported-location"
REFUSED_METHOD_CHANGE = "method-change"
REFUSED_HOP_LIMIT = "hop-limit"

_REFUSAL_TEXT: Mapping[str, str] = {
    REFUSED_OUTSIDE_ORIGIN: "the target is on another origin",
    REFUSED_OUTSIDE_BASE_PATH: "the target is outside the configured base path",
    REFUSED_HTTPS_DOWNGRADE: "the target downgrades HTTPS to HTTP",
    REFUSED_USERINFO: "the target embeds credentials",
    REFUSED_UNSUPPORTED_LOCATION: "the target is not a valid HTTP(S) URL",
    REFUSED_METHOD_CHANGE: "only 307 and 308 may redirect this request method",
    REFUSED_HOP_LIMIT: f"more than {MAX_REDIRECT_HOPS} redirects",
}

_METHOD_PRESERVING_STATUS: frozenset[int] = frozenset({307, 308})
_REDIRECTABLE_ANY_STATUS: frozenset[str] = frozenset({"GET", "HEAD"})
_DEFAULT_PORTS: Mapping[str, int] = {"http": 80, "https": 443}
_ENCODED_PATH_DELIMITER = re.compile(r"%(?:2e|2f|5c|25)", re.IGNORECASE)
_DECODED_PATH_DELIMITER: Mapping[str, str] = {"%2e": ".", "%2f": "/", "%5c": "/", "%25": "%"}
_MAX_PATH_DECODE_ROUNDS = 4


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


class _NoAmbientCredentials(requests.auth.AuthBase):
    """Explicit no-op auth so ``requests`` never loads ambient netrc credentials.

    ``Session.prepare_request`` consults ``.netrc`` only when no ``auth`` is
    given. Passing this keeps the caller's credential header authoritative
    while proxy and CA-bundle environment settings stay in effect.
    """

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        return request


_NO_AMBIENT_CREDENTIALS = _NoAmbientCredentials()


class CredentialScopedSession(requests.Session):
    """Requests session whose credential headers never cross origins.

    ``ambient_credentials=False`` also keeps ``requests`` from attaching
    ``.netrc`` credentials, for clients that send only the credentials they
    were given (the MCP registry client).
    """

    def __init__(self, *, ambient_credentials: bool = True) -> None:
        super().__init__()
        if not ambient_credentials:
            self.auth = _NO_AMBIENT_CREDENTIALS

    def rebuild_auth(
        self,
        prepared_request: requests.PreparedRequest,
        response: requests.Response,
    ) -> None:
        """Strip every credential header across origins, then apply defaults."""
        strip_cross_origin_credential_headers(self, prepared_request, response)
        if self.auth is _NO_AMBIENT_CREDENTIALS:
            return  # requests would re-read .netrc for the redirect target here.
        super().rebuild_auth(prepared_request, response)


class RedirectRefusedError(requests.RequestException):
    """A redirect left the configured origin bounds and was not followed.

    ``response`` is the unfollowed redirect response; ``origin`` is the
    configured ``scheme://host[:port]`` the request was bound to. The message
    never echoes the redirect target, which may carry signed query tokens.
    """

    def __init__(
        self,
        *,
        reason: str,
        origin: str,
        response: requests.Response | None = None,
    ) -> None:
        detail = _REFUSAL_TEXT.get(reason, reason)
        super().__init__(f"redirect from {origin} refused: {detail}", response=response)
        self.reason = reason
        self.origin = origin


def origin_root(url: str) -> str:
    """Return ``scheme://netloc/`` for *url* (the widest in-origin base)."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def _remove_dot_segments(path: str) -> str:
    """Remove ``.`` and ``..`` segments per RFC 3986 section 5.2.4."""
    output: list[str] = []
    for segment in path.split("/"):
        if segment == "..":
            if len(output) > 1:
                output.pop()
        elif segment != ".":
            output.append(segment)
    result = "/".join(output)
    if path.endswith(("/.", "/..")):
        result += "/"
    return result if result.startswith("/") else "/" + result


def _collapse_slashes(path: str) -> str:
    return re.sub(r"/{2,}", "/", path)


def _comparable_paths(path: str) -> tuple[str, ...] | None:
    """Normalize a URL path for the base-path containment check only.

    Percent-encoded dots, slashes, backslashes and percent signs are decoded
    (repeatedly, to defeat double encoding) and backslashes become ``/``.
    Clients disagree on whether empty segments collapse before or after dot
    segments are removed, so both orders are returned and the caller requires
    every variant to stay in bounds. Returns ``None`` when encoded delimiters
    survive the decode limit: the path cannot be judged and is refused.
    """
    decoded = path or "/"
    for _ in range(_MAX_PATH_DECODE_ROUNDS):
        expanded = _ENCODED_PATH_DELIMITER.sub(
            lambda match: _DECODED_PATH_DELIMITER[match.group(0).lower()], decoded
        )
        if expanded == decoded:
            break
        decoded = expanded
    else:
        if _ENCODED_PATH_DELIMITER.search(decoded):
            return None
    decoded = decoded.replace("\\", "/")
    if not decoded.startswith("/"):
        decoded = "/" + decoded
    return (
        _collapse_slashes(_remove_dot_segments(decoded)),
        _remove_dot_segments(_collapse_slashes(decoded)),
    )


def _path_within(path: str, base_path: str) -> bool:
    return not base_path or path == base_path or path.startswith(base_path + "/")


def _comparable_host(hostname: str | None) -> str | None:
    """Return an ASCII, lowercase host, or ``None`` when it cannot be compared."""
    if not hostname or "%" in hostname:
        # ``%`` marks an IPv6 zone ID or a percent-encoded host; both refused.
        return None
    # Non-ASCII hosts compare by exact text: IDNA 2003 (stdlib) and IDNA 2008
    # (used by the HTTP stack) map some names differently, so an encoded
    # comparison could match a host the request would not actually contact.
    return hostname.lower()


def _origin_key(url: str) -> tuple[str, str, int] | None:
    """Return ``(scheme, ascii-host, effective-port)`` or ``None`` if invalid."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    host = _comparable_host(parts.hostname)
    if host is None:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return scheme, host, port if port is not None else _DEFAULT_PORTS[scheme]


def origin_label(url: str) -> str:
    """Return ``scheme://host[:port]`` of *url* for diagnostics (no userinfo)."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.rpartition('@')[2].lower()}"


def origin_bound_refusal(url: str, *, base_url: str) -> str | None:
    """Return why *url* falls outside *base_url*'s origin and path, else ``None``.

    Same origin means equal scheme, IDNA-lowercased host and effective port.
    The normalized path must equal the normalized base path or sit under it.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return REFUSED_USERINFO
    target = _origin_key(url)
    if target is None:
        return REFUSED_UNSUPPORTED_LOCATION
    base = _origin_key(base_url)
    if base is None or target != base:
        if base is not None and base[0] == "https" and target[0] == "http":
            return REFUSED_HTTPS_DOWNGRADE
        return REFUSED_OUTSIDE_ORIGIN
    base_paths = _comparable_paths(urllib.parse.urlsplit(base_url).path)
    target_paths = _comparable_paths(parts.path)
    if base_paths is None or target_paths is None:
        return REFUSED_OUTSIDE_BASE_PATH
    base_path = base_paths[1].rstrip("/")
    if not all(_path_within(path, base_path) for path in target_paths):
        return REFUSED_OUTSIDE_BASE_PATH
    return None


def _carries_credentials(headers: Mapping[str, str]) -> bool:
    names = {name.lower() for name in CREDENTIAL_HEADER_NAMES}
    return any(name.lower() in names for name in headers)


def _without_credentials(headers: Mapping[str, str]) -> dict[str, str]:
    names = {name.lower() for name in CREDENTIAL_HEADER_NAMES}
    return {name: value for name, value in headers.items() if name.lower() not in names}


def _close_quietly(response: requests.Response) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


def origin_bounded_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    base_url: str,
    headers: Mapping[str, str] | None = None,
    data: Any = None,
    max_hops: int = MAX_REDIRECT_HOPS,
    follow_cross_origin_without_credentials: bool = False,
    ambient_credentials: bool = True,
    **kwargs: Any,
) -> requests.Response:
    """Send one request, following only redirects that stay inside *base_url*.

    Every hop is sent with ``allow_redirects=False``. A 301/302/303/307/308
    with a ``Location`` is followed only when the resolved target keeps the
    base origin and path (see :func:`origin_bound_refusal`), never downgrades
    HTTPS, embeds no userinfo, and fits in *max_hops*. 307 and 308 keep the
    method and body; 301, 302 and 303 are followed only for GET and HEAD.
    Anything else raises :class:`RedirectRefusedError` carrying the
    unfollowed response. A response without ``Location`` is returned as is.

    Credentials are re-sent only on in-bound hops. Requests that carry a
    credential header never pick up ambient ``.netrc`` credentials; pass
    ``ambient_credentials=False`` to exclude them even when no credential
    header is sent (for example an anonymous step of an auth chain).

    ``follow_cross_origin_without_credentials`` keeps a legacy anonymous
    follow: an out-of-bound HTTPS hop is followed with every credential header
    removed instead of being refused.
    """
    for reserved in ("allow_redirects", "auth"):
        if reserved in kwargs:
            raise TypeError(f"origin_bounded_request owns {reserved!r}")
    if data is not None and follow_cross_origin_without_credentials:
        raise TypeError("a request body cannot follow redirects to another origin")
    method = method.upper()
    request_headers = dict(headers or {})
    origin = origin_label(base_url)
    initial_refusal = origin_bound_refusal(url, base_url=base_url)
    if initial_refusal is not None:
        raise ValueError(f"request URL is outside {origin}: {_REFUSAL_TEXT[initial_refusal]}")
    if not ambient_credentials or _carries_credentials(request_headers):
        kwargs["auth"] = _NO_AMBIENT_CREDENTIALS
    if data is not None:
        kwargs["data"] = data
    current = url
    hops = 0
    while True:
        response = session.request(
            method,
            url=current,
            headers=request_headers,
            allow_redirects=False,
            **kwargs,
        )
        location = (
            response.headers.get("Location")
            if response.status_code in REDIRECT_STATUS_CODES
            else None
        )
        if not location:
            return response
        target = urllib.parse.urljoin(current, location)
        reason = origin_bound_refusal(target, base_url=base_url)
        if (
            reason in (REFUSED_OUTSIDE_ORIGIN, REFUSED_OUTSIDE_BASE_PATH)
            and follow_cross_origin_without_credentials
            and urllib.parse.urlsplit(target).scheme.lower() == "https"
        ):
            request_headers = _without_credentials(request_headers)
            kwargs["auth"] = _NO_AMBIENT_CREDENTIALS
            reason = None
        if (
            reason is None
            and response.status_code not in _METHOD_PRESERVING_STATUS
            and method not in _REDIRECTABLE_ANY_STATUS
        ):
            reason = REFUSED_METHOD_CHANGE
        if reason is None and hops >= max_hops:
            reason = REFUSED_HOP_LIMIT
        if reason is not None:
            raise RedirectRefusedError(reason=reason, origin=origin, response=response)
        _close_quietly(response)
        hops += 1
        current = target


def split_url_credentials(url: str) -> tuple[str, dict[str, str]]:
    """Move userinfo out of *url* into an explicit HTTP Basic credential header.

    Returns the URL without userinfo and the header (empty when the URL has
    none). The percent-decoded user and password match what ``requests``
    derives from a URL. The header then follows the same origin rules as any
    other credential; redirect targets that embed userinfo stay refused.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.username is None and parts.password is None:
        return url, {}
    host = parts.netloc.rpartition("@")[2]
    stripped = urllib.parse.urlunsplit(
        (parts.scheme, host, parts.path, parts.query, parts.fragment)
    )
    user = urllib.parse.unquote(parts.username or "")
    password = urllib.parse.unquote(parts.password or "")
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return stripped, {"Authorization": f"Basic {token}"}


def unfollowed_redirect_request(
    method: str,
    url: str,
    *,
    base_url: str | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Send one request through :func:`origin_bounded_request`; follow no redirect.

    Every 3xx -- same-origin or not -- comes back as the unfollowed response
    instead of raising, so an auth-outcome classifier or caller can inspect
    it. *base_url* (default: *url*) only bounds the initial request URL.
    """
    with CredentialScopedSession() as session:
        try:
            return origin_bounded_request(
                session, method, url, base_url=base_url or url, max_hops=0, **kwargs
            )
        except RedirectRefusedError as refused:
            if refused.response is None:
                raise
            return refused.response
