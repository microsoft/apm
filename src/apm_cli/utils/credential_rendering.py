"""Canonical owner of how credential values are rendered in command output.

One decision lives here: "what does a credential-bearing value look like
when APM prints, logs, or exports it?". Every output path (``apm config``
reads, diagnostics, debug logs, script logs, SBOM export, error messages)
routes through these helpers instead of growing its own redaction:

* :func:`render_credential_value` -- a stored secret (registry token, PAT)
  renders as a fixed mask; presence is still reported.
* :func:`render_config_value` -- one configuration value, masked when its
  key names a credential, otherwise with URL userinfo and labelled secrets
  removed.
* :func:`redact_url_userinfo` -- strip ``user:password@`` from a URL.
* :func:`redact_secrets` -- replace credential-shaped substrings in free text.
* :func:`mask_known_values` -- replace known secret values in free text.

The values themselves are never transformed for use; only their displayed
form is.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

CREDENTIAL_MASK = "********"
CREDENTIAL_PRESENT = f"{CREDENTIAL_MASK} (set)"
NOT_SET = "Not set"
REDACTED = "[REDACTED]"
INVALID_URL_PLACEHOLDER = "<redacted-url>"

# Keyword + ``:``/``=`` separator redacts any value (optionally after an auth
# scheme word). Keyword + whitespace only redacts token-shaped values (>= 16
# chars) so prose such as "bearer token acquisition failed" stays readable.
SECRET_RE = re.compile(
    r"(?:"
    r"(?:token|password|secret|authorization)\s*[:=]\s*"
    r"(?:(?:bearer|basic|token)\s+)?"
    r"[\w.~!*\'();:@&=+$,/?#\[\]\-]{4,}"
    r"|"
    r"(?:token|password|secret|bearer|basic)\s+"
    r"[\w.~!*\'();:@&=+$,/?#\[\]\-]{16,}"
    r"|"
    r"://[^:@/\s]+:[^:@/\s]+@"
    r"|"
    r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[psour]_[A-Za-z0-9_]{20,})\b"
    r"|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"
    r")",
    re.IGNORECASE,
)

# Last dotted segment of a configuration key that names a stored secret.
_CREDENTIAL_KEY_SEGMENTS = frozenset({"token", "password", "secret", "pat", "api-key", "apikey"})
_CREDENTIAL_KEY_SUFFIXES = ("-token", "_token", "-password", "_password", "-secret", "_secret")


def redact_secrets(text: str) -> str:
    """Replace credential-shaped substrings in *text* with ``[REDACTED]``."""
    return SECRET_RE.sub(REDACTED, text)


def mask_known_values(text: str, values: Iterable[str]) -> str:
    """Replace every occurrence of each known secret value with ``[REDACTED]``.

    Values are replaced longest-first so a shorter secret that is a substring
    of a longer one cannot split it and leave its tail in cleartext.
    """
    if not text:
        return text
    redacted = text
    for value in sorted({v for v in values if v}, key=len, reverse=True):
        redacted = redacted.replace(value, REDACTED)
    return redacted


def redact_url_userinfo(url: str, *, drop_query: bool = False) -> str:
    """Return *url* without ``user:password@`` userinfo.

    With ``drop_query`` the query string is removed as well (it can carry
    ``?token=`` style credentials). A URL with nothing to remove is returned
    unchanged; a malformed URL that carries userinfo renders as a placeholder.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
        has_userinfo = "@" in (parts.netloc or "")
        if not has_userinfo and not (drop_query and parts.query):
            return url
        netloc = parts.netloc
        if has_userinfo:
            host = parts.hostname or ""
            if not host:
                return INVALID_URL_PLACEHOLDER
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            port = parts.port
            netloc = f"{host}:{port}" if port is not None else host
        query = "" if drop_query else parts.query
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    except (TypeError, ValueError):
        return INVALID_URL_PLACEHOLDER


def is_credential_key(key: str) -> bool:
    """Return whether configuration *key* names a stored credential value."""
    last = key.rsplit(".", 1)[-1].strip().lower()
    return last in _CREDENTIAL_KEY_SEGMENTS or last.endswith(_CREDENTIAL_KEY_SUFFIXES)


def render_credential_value(value: object) -> str:
    """Render a stored credential: a fixed mask when present, ``Not set`` otherwise."""
    if value is None or value == "":
        return NOT_SET
    return CREDENTIAL_PRESENT


def render_config_value(key: str, value: object) -> str:
    """Render one configuration value for command output.

    Credential keys render through :func:`render_credential_value`. Any other
    value renders as text with URL userinfo and labelled secrets removed.
    """
    if is_credential_key(key):
        return render_credential_value(value)
    if value is None:
        return NOT_SET
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if "://" in text:
        text = " ".join(redact_url_userinfo(part) for part in text.split(" "))
    return redact_secrets(text)


__all__ = [
    "CREDENTIAL_MASK",
    "CREDENTIAL_PRESENT",
    "NOT_SET",
    "REDACTED",
    "SECRET_RE",
    "is_credential_key",
    "mask_known_values",
    "redact_secrets",
    "redact_url_userinfo",
    "render_config_value",
    "render_credential_value",
]
