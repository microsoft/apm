"""Versioned JSON envelope for marketplace and view command results."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

SCHEMA_VERSION = 1
_SOURCE_TOKEN = re.compile(
    r"""[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>"']+|[^\s/@:]+@[^\s/:]+:[^\s<>"']+"""
    r"""|(?<!\w)(?:/|~/|[A-Za-z]:[\\/])[^\s<>"']+"""
)


def success_envelope(data: object) -> dict[str, object]:
    """Wrap a JSON-compatible result without changing its data."""
    return {"schema_version": SCHEMA_VERSION, "data": data}


def error_envelope(code: str) -> dict[str, object]:
    """Wrap a stable machine-readable error code, not exception text."""
    return {"schema_version": SCHEMA_VERSION, "error": code}


def safe_text(value: str | None) -> str | None:
    """Project optional metadata without URL credentials, queries, or fragments."""
    if not value:
        return None

    def clean(match: re.Match[str]) -> str:
        token = match.group()
        if token.startswith(("/", "~/", "file://")) or re.match(r"^[A-Za-z]:[\\/]", token):
            return safe_source(token) or "[internal-cache]"
        if "://" not in token:
            token = "ssh://" + token.replace(":", "/", 1)
        try:
            parsed = urlsplit(token)
            return urlunsplit(
                (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", "")
            )
        except ValueError:
            return "[redacted-url]"

    result = _SOURCE_TOKEN.sub(clean, value)
    return None if result == "[internal-cache]" else result


def safe_install_path(value: str | None) -> str | None:
    """Retain an actual installation path unless it points into an internal cache."""
    if not value:
        return None
    from apm_cli.cache.paths import cache_roots_for_display
    from apm_cli.config import CONFIG_DIR

    try:
        path = Path(value).expanduser().resolve()
        roots = (*cache_roots_for_display(), Path(CONFIG_DIR) / "cache")
        if any(path.is_relative_to(root.resolve()) for root in roots):
            return None
        return path.as_posix()
    except (OSError, ValueError, RuntimeError):
        return None


def safe_source(value: str | None) -> str | None:
    """Sanitize a source URL or show a safe local source with home abbreviation."""
    if not value:
        return None
    if value.startswith("file://"):
        value = unquote(urlsplit(value).path)
    if value.startswith(("/", "./", "../", "~", "\\")) or re.match(r"^[A-Za-z]:[\\/]", value):
        local = safe_install_path(value)
        if local is None:
            return None
        path = Path(local)
        home = Path.home().resolve()
        if path.is_relative_to(home):
            relative = path.relative_to(home).as_posix()
            return "~" if relative == "." else f"~/{relative}"
        return local
    return safe_text(value)
