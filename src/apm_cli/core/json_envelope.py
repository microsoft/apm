"""Versioned JSON envelope for marketplace and view command results."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

SCHEMA_VERSION = 1
_SOURCE_TOKEN = re.compile(
    r"""[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>"]+|[^\s/@]+@[^\s/:]+:[^\s<>"]+"""
    r"""|(?<!\w)(?:/|~/|[A-Za-z]:[\\/])[^\s<>"]+"""
)


def success_envelope(data: object) -> dict[str, object]:
    """Wrap a JSON-compatible result without changing its data."""
    return {"schema_version": SCHEMA_VERSION, "data": data}


def error_envelope(code: str) -> dict[str, object]:
    """Wrap a stable machine-readable error code, not exception text."""
    return {"schema_version": SCHEMA_VERSION, "error": code}


def safe_text(value: object) -> str | None:
    """Project optional metadata without URL credentials, queries, or fragments."""
    if not isinstance(value, str) or not value:
        return None

    from apm_cli.cache.paths import cache_roots_for_display
    from apm_cli.config import CONFIG_DIR

    text = value
    try:
        roots = (*cache_roots_for_display(), Path(CONFIG_DIR) / "cache")
        for root in roots:
            root_str = root.resolve().as_posix()
            if text == root_str or text.startswith(root_str + "/"):
                return "[internal-cache]"
            # Find and redact complete paths that start with this root
            # Match paths that may contain spaces: root + "/" + anything until whitespace followed by lowercase word
            root_pattern = re.escape(root_str) + r"(?:/[^\s<>\"]*(?:\s+[^\s<>\"]+)*)*"
            matches = list(re.finditer(root_pattern, text))
            for match in reversed(matches):
                path_str = match.group()
                try:
                    path_obj = Path(path_str).resolve()
                    if any(path_obj.is_relative_to(r.resolve()) for r in roots):
                        text = text[: match.start()] + "[internal-cache]" + text[match.end() :]
                except (OSError, ValueError, RuntimeError):
                    pass
    except (OSError, ValueError, RuntimeError):
        pass

    def clean(match: re.Match[str]) -> str:
        token = match.group()
        if token.startswith(("/", "~/", "file://")) or re.match(r"^[A-Za-z]:[\\/]", token):
            return safe_source(token) or "[internal-cache]"
        if "://" not in token:
            # SSH URL: user:password@host:port or user@host:path
            # Parse it to extract and redact credentials
            try:
                at_index = token.rfind("@")
                if at_index != -1:
                    # Has credentials - redact them by taking only the part after @
                    host_part = token[at_index + 1 :]
                    parsed = urlsplit("ssh://" + host_part)
                    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
                else:
                    # No @ sign - just format as SSH URL
                    token = "ssh://" + token.replace(":", "/", 1)
            except ValueError:
                return "[redacted-url]"
        try:
            parsed = urlsplit(token)
            return urlunsplit(
                (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", "")
            )
        except ValueError:
            return "[redacted-url]"

    result = _SOURCE_TOKEN.sub(clean, text)
    return None if result in {"[internal-cache]", "[redacted-url]"} else result


def safe_install_path(value: object) -> str | None:
    """Retain an actual installation path; cache or unresolvable paths are unavailable."""
    if not isinstance(value, str) or not value:
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


def safe_source(value: object) -> str | None:
    """Sanitize a source URL or show a safe local source with home abbreviation."""
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("file://"):
        try:
            value = unquote(urlsplit(value).path)
        except ValueError:
            return None
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
