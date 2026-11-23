"""Versioned JSON envelope for marketplace and view command results."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
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
            # The root can contain spaces; only extend past it through
            # contiguous path segments so following prose remains untouched.
            root_pattern = re.escape(root_str) + r"(?:/[^\s<>\"']*)?"
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
        if (
            token.startswith(("/", "~/"))
            or token[:7].lower() == "file://"
            or re.match(r"^[A-Za-z]:[\\/]", token)
        ):
            return safe_source(token) or "[internal-cache]"
        if "://" not in token:
            # SCP-style source (user@host:org/repo): drop credentials, keep host and path.
            host_part = token[token.rfind("@") + 1 :].replace(":", "/", 1)
            token = "ssh://" + host_part
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

    if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", value):
        # A Windows path cannot be resolved here; resolving would anchor it to the cwd.
        return PureWindowsPath(value).as_posix()
    try:
        path = Path(value).expanduser().resolve()
        roots = (*cache_roots_for_display(), Path(CONFIG_DIR) / "cache")
        if any(path.is_relative_to(root.resolve()) for root in roots):
            return None
        return path.as_posix()
    except (OSError, ValueError, RuntimeError):
        return None


def safe_plugin_source(value: object) -> str | None:
    """Project a marketplace plugin source without anchoring it to the working directory.

    Relative locators are relative to the marketplace, so they are normalized
    textually instead of resolved against the invoking directory.
    """
    if not isinstance(value, str) or not value:
        return None
    is_relative = not (
        value.startswith(("/", "~", "\\"))
        or "://" in value
        or re.match(r"^[A-Za-z]:[\\/]", value)
        or re.match(r"^[^\s/@]+@[^\s/:]+:", value)
    )
    if not is_relative:
        return safe_source(value)
    if not value.isprintable():
        return None
    parts = [part for part in value.replace("\\", "/").split("/") if part not in {"", "."}]
    if ".." in parts:
        return None
    return "/".join(parts) or "."


def _file_uri_to_path(uri: str) -> str | None:
    """Convert a file URI to a path; Windows drive and UNC forms stay recognizable."""
    try:
        parts = urlsplit(uri)
        path = unquote(parts.path)
        host = parts.hostname
    except ValueError:
        return None
    if re.match(r"^/[A-Za-z]:(?:/|$)", path):
        return path[1:]
    if host and host != "localhost":
        return f"//{host}{path}"
    return path


def safe_source(value: object) -> str | None:
    """Sanitize a source URL or show a safe local source with home abbreviation."""
    if not isinstance(value, str) or not value:
        return None
    if value[:7].lower() == "file://":
        value = _file_uri_to_path(value)
        if value is None:
            return None
    if re.match(r"^//[^/]", value):
        # Network share: keep the host; resolving would collapse the leading slashes.
        return PurePosixPath(value).as_posix()
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
