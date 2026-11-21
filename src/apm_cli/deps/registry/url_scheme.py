"""Registry URL scheme acceptance (OpenAPM v0.1 req-sc-006).

The single owner that decides whether a ``registries.<name>.url`` is
accepted, for every manifest contract and every manifest that declares
registries. An ``https://`` URL is accepted. An ``http://`` URL is accepted
only when the entry sets ``insecure: true`` or the host is a loopback or
RFC 1918 private address. Any other scheme is rejected.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from ...utils.credential_rendering import redact_url_userinfo
from ...utils.net import is_local_network_host


def _is_local_host(host: str) -> bool:
    """True for a loopback host or an RFC 1918 private IPv4 literal (utils/net.py owns it)."""
    return is_local_network_host(host)


def registry_url_error(name: str, url: object, *, insecure: object = False) -> str | None:
    """Return why registry *name* may not use *url*, or ``None`` when accepted.

    *insecure* is the entry's ``insecure`` value; only the boolean ``True``
    opts a non-local ``http://`` URL in. A diagnostic names the registry and
    renders the URL only through the credential owner (``redact_url_userinfo``).
    """
    if insecure is not None and not isinstance(insecure, bool):
        return f"Registry {name!r}: 'insecure' must be a boolean (got {type(insecure).__name__})"
    if not isinstance(url, str) or not url.strip():
        return f"Registry {name!r} is missing required field 'url:'"
    if not url.strip().lower().startswith(("https://", "http://")):
        return (
            f"Registry {name!r} URL must start with https:// or http:// "
            f"(got {redact_url_userinfo(url)!r})"
        )
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return f"Registry {name!r} URL is not a valid URL (got {redact_url_userinfo(url)!r})"
    if not host:
        return f"Registry {name!r} URL has no host (got {redact_url_userinfo(url)!r})"
    if parts.scheme == "https" or insecure is True or _is_local_host(host):
        return None
    return (
        f"Registry {name!r} uses http:// to a non-local host ({host}). "
        "Use https://, or set 'insecure: true' on this registry entry."
    )
