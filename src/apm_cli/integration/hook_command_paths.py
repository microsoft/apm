"""Canonical parsing for plugin-root paths in hook commands."""

from __future__ import annotations

import re
from collections.abc import Iterator

PLUGIN_ROOT_NAMES = (
    "CLAUDE_PLUGIN_ROOT",
    "CURSOR_PLUGIN_ROOT",
    "KIRO_PLUGIN_ROOT",
    "PLUGIN_ROOT",
)

_PLUGIN_ROOT_TOKEN = rf"\$\{{(?:{'|'.join(map(re.escape, PLUGIN_ROOT_NAMES))})\}}"
_PLUGIN_ROOT_PATH = r"[\\/](?:\\[ \t;&|<>()]|[^\s\"';&|<>()])+"
_QUOTED_PLUGIN_ROOT_SPLIT = re.compile(
    rf"(?P<quote>[\"'])(?P<var>{_PLUGIN_ROOT_TOKEN})"
    rf"(?P=quote)(?P<path>{_PLUGIN_ROOT_PATH})"
)
_PLUGIN_ROOT_PATH_REFERENCE = re.compile(rf"{_PLUGIN_ROOT_TOKEN}({_PLUGIN_ROOT_PATH})")
_PLUGIN_ROOT_REFERENCE = re.compile(rf"{_PLUGIN_ROOT_TOKEN}[^\s\"']*")
_RELATIVE_SCRIPT_PATH = re.compile(r"(?<![.$])(\.[\\/][^\s\"']+)")


def normalize_quoted_plugin_root(command: str) -> str:
    """Move a split closing quote after its plugin-relative path."""
    return _QUOTED_PLUGIN_ROOT_SPLIT.sub(lambda match: f'"{match["var"]}{match["path"]}"', command)


def iter_plugin_root_paths(command: str) -> Iterator[re.Match[str]]:
    """Yield supported plugin-root references that include a path."""
    return iter(_PLUGIN_ROOT_PATH_REFERENCE.finditer(command))


def plugin_root_relative_path(path: str) -> str:
    """Decode shell-escaped separators and spaces into a package-relative path."""
    unescaped = re.sub(r"\\([ \t;&|<>()])", r"\1", path)
    return unescaped.replace("\\", "/").lstrip("/")


def unresolved_plugin_root_references(command: str) -> tuple[str, ...]:
    """Return residual supported references once each, preserving command order.

    Detection is diagnostic only; containment enforcement remains with the caller.
    """
    return tuple(dict.fromkeys(_PLUGIN_ROOT_REFERENCE.findall(command)))


def residual_plugin_root_has_path(command: str, reference: str) -> bool:
    """Return whether a residual is followed by a direct or split-quoted path."""
    suffix = rf"{re.escape(reference)}(?:[\\/]|[\"'][\\/])"
    return re.search(suffix, command) is not None


def iter_relative_script_paths(command: str) -> Iterator[re.Match[str]]:
    """Yield relative script references without re-matching plugin-root paths."""
    return iter(_RELATIVE_SCRIPT_PATH.finditer(command))
