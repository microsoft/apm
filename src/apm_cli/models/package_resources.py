"""Selection authority for non-activating, working-draft package resources."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

from ..agent_plugins.assets import AssetInventory, normalized_path_key
from ..agent_plugins.constants import MAX_COMPONENT_ASSET_ENTRIES
from ..agent_plugins.ir import AgentPluginAsset
from ..utils.path_security import (
    ensure_path_within,
    has_symlink_component,
    validate_portable_relative_path,
)

_RESERVED = frozenset(
    {
        "apm.yml",
        "apm.lock",
        "apm.lock.yaml",
        "plugin.json",
        "mcp.json",
        "apm_modules",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
    }
)


def _validate_resource_path(path: str) -> None:
    validate_portable_relative_path(path, context="resources path")
    if any(
        part.startswith(".") or normalized_path_key(part) in _RESERVED for part in path.split("/")
    ):
        raise ValueError(
            f"Unsafe resources path {path!r}: hidden paths, package metadata, and "
            "build/cache directories are not resources. Select dedicated content directories."
        )


def parse_resource_roots(value: object) -> tuple[str, ...]:
    """Parse explicit, nonoverlapping directory roots; absence is handled by the caller."""
    if not isinstance(value, list) or not value:
        raise ValueError("'resources' must be a non-empty list of package-relative directories")
    if len(value) > MAX_COMPONENT_ASSET_ENTRIES:
        raise ValueError("'resources' exceeds the package entry budget")
    roots: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("'resources' entries must be package-relative directory strings")
        _validate_resource_path(item)
        roots.append(item)
    ordered = sorted((tuple(normalized_path_key(root).split("/")), root) for root in roots)
    for (previous, left), (current, right) in pairwise(ordered):
        if current[: len(previous)] == previous:
            raise ValueError(f"Overlapping or case-aliased resources roots: {left}, {right}")
    return tuple(roots)


def collect_package_resources(
    root: Path, roots: tuple[str, ...], inventory: AssetInventory
) -> tuple[AgentPluginAsset, ...]:
    """Inventory declared directories without interpreting any file as a primitive."""
    assets: list[AgentPluginAsset] = []
    for relative in roots:
        directory = root / relative
        ensure_path_within(directory, root)
        if has_symlink_component(root, directory, raise_on_error=True) or not directory.is_dir():
            raise ValueError(f"Resource directory {relative!r} is missing or symlinked")
        # Inventorying parents also catches a declared root's case aliases.
        parent = root
        for part in relative.split("/"):
            siblings = inventory.list_component_candidates(parent)
            matches = [
                entry.name
                for entry in siblings
                if normalized_path_key(entry.name) == normalized_path_key(part)
            ]
            if matches != [part]:
                raise ValueError(f"Resource directory {relative!r} has an ambiguous path")
            parent /= part
        selected = inventory.collect_component(directory)
        if not selected:
            raise ValueError(
                f"Resource directory {relative!r} is empty; select content directories"
            )
        for asset in selected:
            _validate_resource_path(asset.path)
        assets.extend(selected)
    return tuple(assets)
