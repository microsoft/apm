"""Portable ``${CLAUDE_PLUGIN_ROOT}`` handling for MCP and LSP declarations.

Canonical owner of how a plugin-root placeholder in an MCP or LSP declaration
becomes a native path (#3825). Declarations keep the authored placeholder, so
the lockfile, ``--frozen`` and ``apm audit`` compare one location-independent
form. Only a target render expands it: to the target's documented project-root
reference for a package inside the project, otherwise to the absolute path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

PLUGIN_ROOT_PLACEHOLDER = "${CLAUDE_PLUGIN_ROOT}"

# Server-info key carrying the materialized package root to the adapter render.
PLUGIN_ROOT_INFO_KEY = "_plugin_root"

# Inert stand-in for a target's project-root reference. No env, input or
# runtime-template placeholder pattern matches it, so adapter placeholder
# translation cannot rewrite the reference before ``finalize_project_root``.
_PROJECT_ROOT_STANDIN = "@@APM_PROJECT_ROOT@@"


def contains_plugin_root(value: Any) -> bool:
    """Return whether *value* references the plugin-root placeholder."""
    if isinstance(value, str):
        return PLUGIN_ROOT_PLACEHOLDER in value
    if isinstance(value, dict):
        return any(contains_plugin_root(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_plugin_root(item) for item in value)
    return False


def plugin_root_of(dependency: Any) -> str | None:
    """Return the bound package root of *dependency*, if it has one."""
    root = getattr(dependency, "plugin_root", None)
    return root if isinstance(root, str) and root else None


def bind_plugin_roots(dependencies: Iterable[Any] | None, package_path: Path) -> None:
    """Record the materialized package root on declarations that reference it."""
    root = str(package_path.resolve())
    for dependency in dependencies or ():
        to_dict = getattr(dependency, "to_dict", None)
        if hasattr(dependency, "plugin_root") and callable(to_dict):
            if contains_plugin_root(to_dict()):
                dependency.plugin_root = root


def _substitute(value: Any, replacement: str) -> Any:
    if isinstance(value, str):
        return value.replace(PLUGIN_ROOT_PLACEHOLDER, replacement)
    if isinstance(value, dict):
        return {key: _substitute(item, replacement) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, replacement) for item in value]
    if isinstance(value, tuple):
        return tuple(_substitute(item, replacement) for item in value)
    return value


def _project_relative(plugin_root: str, project_root: Path) -> str | None:
    """Return the POSIX path of *plugin_root* below *project_root*, if inside it."""
    try:
        relative = Path(plugin_root).resolve().relative_to(Path(project_root).resolve())
    except (OSError, ValueError):
        return None
    return relative.as_posix()


def render_plugin_root(
    value: Any,
    *,
    plugin_root: str | None,
    project_root: Path | None,
    project_relative: bool,
) -> Any:
    """Expand the placeholder for one target render.

    With *project_relative* and a package inside *project_root*, the root is
    written as the target's project-root reference (resolved by
    :func:`finalize_project_root`); otherwise as the absolute package path.
    """
    if not plugin_root or not contains_plugin_root(value):
        return value
    replacement = str(Path(plugin_root))
    if project_relative and project_root is not None:
        relative = _project_relative(plugin_root, project_root)
        if relative is not None:
            replacement = (
                _PROJECT_ROOT_STANDIN if relative == "." else f"{_PROJECT_ROOT_STANDIN}/{relative}"
            )
    return _substitute(value, replacement)


def with_absolute_plugin_root(
    dependency: Any, fields: Iterable[str], *, root: str | None = None
) -> Any:
    """Return *dependency* with *fields* expanded at an absolute package root.

    *root* defaults to the bound package root. For values no target renders
    portably, such as registry overlays, and for replaying a recorded root.
    """
    import dataclasses

    root = root or plugin_root_of(dependency)
    if root is None or not dataclasses.is_dataclass(dependency):
        return dependency

    changes = {
        name: render_plugin_root(
            getattr(dependency, name), plugin_root=root, project_root=None, project_relative=False
        )
        for name in fields
    }
    return dataclasses.replace(dependency, **changes)


def finalize_project_root(value: Any, reference: str | None) -> Any:
    """Replace the inert project-root stand-in with the target's *reference*."""
    if isinstance(value, str):
        if _PROJECT_ROOT_STANDIN not in value:
            return value
        if reference is None:
            raise ValueError("A project-relative plugin root needs a target reference")
        return value.replace(_PROJECT_ROOT_STANDIN, reference)
    if isinstance(value, dict):
        return {key: finalize_project_root(item, reference) for key, item in value.items()}
    if isinstance(value, list):
        return [finalize_project_root(item, reference) for item in value]
    if isinstance(value, tuple):
        return tuple(finalize_project_root(item, reference) for item in value)
    return value


def _placeholder_pairs(current: Any, stored: Any, pairs: list[tuple[str, str]]) -> bool:
    """Collect differing string leaves; False when anything else differs."""
    if isinstance(current, dict):
        if not isinstance(stored, dict) or current.keys() != stored.keys():
            return False
        return all(_placeholder_pairs(current[key], stored[key], pairs) for key in current)
    if isinstance(current, list):
        if not isinstance(stored, list) or len(current) != len(stored):
            return False
        return all(
            _placeholder_pairs(item, other, pairs)
            for item, other in zip(current, stored, strict=True)
        )
    if isinstance(current, str) and isinstance(stored, str) and current != stored:
        if PLUGIN_ROOT_PLACEHOLDER not in current:
            return False
        pairs.append((current, stored))
        return True
    return current == stored


def _expanded_root(authored: str, stored: str) -> str | None:
    """Return the single root that turns *authored* into *stored*, if any."""
    parts = authored.split(PLUGIN_ROOT_PLACEHOLDER)
    pattern = re.escape(parts[0]) + "(?P<root>.+?)" + re.escape(parts[1])
    pattern += "".join("(?P=root)" + re.escape(part) for part in parts[2:])
    match = re.fullmatch(pattern, stored, flags=re.DOTALL)
    return match.group("root") if match else None


def _is_absolute(path: str) -> bool:
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def _is_normalized_root(root: str) -> bool:
    """An absolute root with no empty, ``.`` or ``..`` segment."""
    normalized = root.replace("\\", "/")
    if not _is_absolute(root) or normalized.endswith("/"):
        return False
    segments = normalized.split("/")[1:]
    if PureWindowsPath(root).drive:
        segments = segments[1:] if normalized.startswith("//") else segments
    return all(segment not in ("", ".", "..") for segment in segments)


def legacy_absolute_root(
    current: Any,
    stored: Any,
    *,
    plugin_root: str | None,
    project_root: Path | None,
) -> str | None:
    """Return the absolute root at which *stored* is *current* expanded, if any.

    Releases before #3825 recorded the placeholder already expanded. Such a
    value is the same declaration when one normalized absolute root, either
    this package's root or a path ending in the same project-relative install
    path (another clone), turns every placeholder into the stored text.
    """
    if not plugin_root or not contains_plugin_root(current):
        return None
    pairs: list[tuple[str, str]] = []
    if not _placeholder_pairs(current, stored, pairs) or not pairs:
        return None
    roots = {_expanded_root(authored, value) for authored, value in pairs}
    if len(roots) != 1 or None in roots:
        return None
    (root,) = roots
    if root is None or not _is_normalized_root(root):
        return None
    normalized = root.replace("\\", "/")
    same_root = normalized == str(Path(plugin_root)).replace("\\", "/").rstrip("/")
    relative = _project_relative(plugin_root, project_root) if project_root is not None else None
    moved_clone = relative not in (None, ".") and normalized.endswith("/" + str(relative))
    if (same_root or moved_clone) and _substitute(current, root) == stored:
        return root
    return None


def legacy_absolute_equivalent(
    current: Any,
    stored: Any,
    *,
    plugin_root: str | None,
    project_root: Path | None,
) -> bool:
    """Whether *stored* is *current* recorded with its plugin root expanded."""
    return (
        legacy_absolute_root(current, stored, plugin_root=plugin_root, project_root=project_root)
        is not None
    )
