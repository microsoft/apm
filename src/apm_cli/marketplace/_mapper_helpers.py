"""Helper utilities shared by output mapper implementations.

Extracted from output_mappers to keep that module under 400 lines.
"""

from __future__ import annotations

from typing import Any

from .errors import BuildError


def _duplicate_name_warnings(plugins: list[dict[str, Any]]) -> list[str]:
    seen_names: dict[str, str] = {}
    warnings: list[str] = []
    for plugin in plugins:
        name = plugin["name"]
        source = plugin.get("source", {})
        if isinstance(source, str):
            source_label = source
        else:
            source_label = source.get("path") or source.get("repo") or source.get("repository", "?")
        if name in seen_names:
            warnings.append(
                f"Duplicate package name '{name}': "
                f"'{seen_names[name]}' and '{source_label}'. "
                f"Consumers will see duplicate entries in browse."
            )
        else:
            seen_names[name] = source_label
    return warnings


def _is_display_version(value: str | None) -> bool:
    if not value:
        return False
    stripped = value.strip()
    if any(stripped.startswith(char) for char in ("^", "~", ">", "<", "=")):
        return False
    return not (" " in stripped or "*" in stripped or "x" in stripped.lower().split(".")[-1:])


def _subtract_plugin_root(source: str, plugin_root: str) -> str:
    from pathlib import PurePosixPath

    norm_source = source.lstrip("./") if source.startswith("./") else source
    norm_root = plugin_root.lstrip("./") if plugin_root.startswith("./") else plugin_root
    norm_root = norm_root.rstrip("/")
    norm_source = norm_source.rstrip("/")

    src_path = PurePosixPath(norm_source)
    root_path = PurePosixPath(norm_root)

    relative = src_path.relative_to(root_path)
    result = str(relative)

    if not result or result == ".":
        raise BuildError(
            f"subtracting pluginRoot '{plugin_root}' from source '{source}' yields empty path"
        )

    if result.startswith("/"):
        raise BuildError(f"pluginRoot subtraction produced absolute path: '{result}'")
    if ".." in result.split("/"):
        raise BuildError(f"pluginRoot subtraction produced path with traversal: '{result}'")

    return "./" + result
