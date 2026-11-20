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
* :func:`redact_secrets` -- replace credential-shaped substrings in free text,
  including the value of credential flags such as ``--api-key VALUE``.
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
# Values already rendered as a mask are left as they are. URL userinfo is
# redacted when it carries a password (which may itself contain ``:``) or a
# token-shaped username.
SECRET_RE = re.compile(
    r"(?:"
    r"(?:token|password|secret|authorization|api[_-]?key)\s*[:=]\s*"
    r"(?:(?:bearer|basic|token)\s+)?"
    r"(?!\[REDACTED\]|\*{8})"
    r"[\w.~!*\'();:@&=+$,/?#\[\]\-]{4,}"
    r"|"
    r"(?:token|password|secret|bearer|basic|api[_-]?key)\s+"
    r"(?!\[REDACTED\])"
    r"[\w.~!*\'();:@&=+$,/?#\[\]\-]{16,}"
    r"|"
    r"://[^@/\s]*:[^@/\s]*@"
    r"|"
    r"://[^:@/\s]{16,}@"
    r"|"
    r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[psour]_[A-Za-z0-9_]{20,})\b"
    r"|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"
    r")",
    re.IGNORECASE,
)

# A command-line flag whose name marks its value as a credential, such as
# ``--api-key VALUE``, ``--token=VALUE`` or ``--client-secret VALUE``. The
# flag name stays readable; only the value is replaced.
_SECRET_FLAG_RE = re.compile(
    r"(?<![\w-])(--?(?:[A-Za-z0-9]+[-_])*"
    r"(?:token|password|passwd|secret|auth|authorization|api[-_]?key|apikey|credentials?|pat)"
    r"(?:[-_][A-Za-z0-9]+)*)"
    r"(=|\s+)(?!-|\[REDACTED\])"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s'\"]+)",
    re.IGNORECASE,
)

# A URL embedded anywhere in free text, e.g. ``--endpoint=https://...``.
_EMBEDDED_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")

# Last dotted segment of a configuration key that names a stored secret.
_CREDENTIAL_KEY_SEGMENTS = frozenset({"token", "password", "secret", "pat", "api-key", "apikey"})
_CREDENTIAL_KEY_SUFFIXES = ("-token", "_token", "-password", "_password", "-secret", "_secret")


def redact_secrets(text: str) -> str:
    """Replace credential-shaped substrings in *text* with ``[REDACTED]``."""
    flagged = _SECRET_FLAG_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)
    return SECRET_RE.sub(REDACTED, flagged)


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


def _mask_parameters(text: str) -> str:
    """Mask every value of a ``name=value&...`` string (or the whole string)."""
    if "=" not in text:
        return CREDENTIAL_MASK
    return "&".join(
        f"{name}={CREDENTIAL_MASK}" if sep else name
        for name, sep, _value in (pair.partition("=") for pair in text.split("&"))
    )


def redact_url_userinfo(url: str, *, drop_query: bool = False) -> str:
    """Return *url* for display without credentials.

    Removes ``user:password@`` userinfo and masks every query value (and
    ``key=value`` fragment values), since query parameters such as
    ``?token=`` or ``?sig=`` can carry credentials under any name or
    encoding. With ``drop_query`` the query is removed instead. A URL with
    nothing to remove is returned unchanged; a malformed URL that carries
    userinfo renders as a placeholder.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
        has_userinfo = "@" in (parts.netloc or "")
        has_fragment_values = "=" in parts.fragment
        if not (has_userinfo or parts.query or has_fragment_values):
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
        query = "" if drop_query or not parts.query else _mask_parameters(parts.query)
        fragment = _mask_parameters(parts.fragment) if has_fragment_values else parts.fragment
        return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))
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
    text = _EMBEDDED_URL_RE.sub(lambda match: redact_url_userinfo(match.group(0)), str(value))
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
