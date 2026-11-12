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
_PLUGIN_ROOT_TOKEN_REFERENCE = re.compile(_PLUGIN_ROOT_TOKEN)
_QUOTED_PLUGIN_ROOT_PATH_REFERENCES = {
    quote: re.compile(rf"{_PLUGIN_ROOT_TOKEN}([\\/](?:\\.|[^{quote}\\])+)(?={quote})")
    for quote in "\"'"
}
_PLUGIN_ROOT_REFERENCE = re.compile(rf"{_PLUGIN_ROOT_TOKEN}[^\s\"']*")
_RELATIVE_SCRIPT_PATH = re.compile(r"(?<![.$])(\.[\\/][^\s\"']+)")


def normalize_quoted_plugin_root(command: str) -> str:
    """Move a split closing quote after its plugin-relative path."""

    def normalize(match: re.Match[str]) -> str:
        path = re.sub(r"\\([ \t;&|<>()])", r"\1", match["path"])
        return f'"{match["var"]}{path}"'

    return _QUOTED_PLUGIN_ROOT_SPLIT.sub(normalize, command)


def iter_plugin_root_paths(command: str) -> Iterator[re.Match[str]]:
    """Yield supported plugin-root references that include a path."""
    end = 0
    for token in _PLUGIN_ROOT_TOKEN_REFERENCE.finditer(command):
        if token.start() < end:
            continue
        quote = command[token.start() - 1] if token.start() else ""
        pattern = _QUOTED_PLUGIN_ROOT_PATH_REFERENCES.get(quote, _PLUGIN_ROOT_PATH_REFERENCE)
        match = pattern.match(command, token.start())
        if match is not None:
            end = match.end()
            yield match


def plugin_root_relative_path(path: str) -> str:
    """Decode shell-escaped separators and spaces into a package-relative path."""
    unescaped = re.sub(r"\\([ \t;&|<>()])", r"\1", path)
    return unescaped.replace("\\", "/").lstrip("/")


def relocated_plugin_root_reference(match: re.Match[str], relative_path: str) -> str:
    """Keep the authored variable and shell escapes when relocating its path."""
    original_path = match.group(1)
    quoted = match.start() > 0 and match.string[match.start() - 1] in "\"'"
    if not quoted:
        for character in " \t;&|<>()":
            if f"\\{character}" in original_path:
                relative_path = relative_path.replace(character, f"\\{character}")
    variable = match.group(0)[: -len(original_path)]
    return f"{variable}/{relative_path}"


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
