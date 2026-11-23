"""Surviving local declarations that share a removed package's install slot, and
what a surviving package ships."""

from __future__ import annotations

from pathlib import Path

from ...models.apm_package import DependencyReference
from ...models.dependency.selection import (
    parse_dependency_entry as _parse_dependency_entry,
)
from ...utils.path_security import PathTraversalError


def _surviving_local_refs_at_install_path(
    package: object,
    surviving_dependencies: list[object],
    apm_modules_dir: Path,
) -> list[DependencyReference]:
    """Return surviving local declarations that share a package install slot."""
    try:
        removed_ref = _parse_dependency_entry(package)
        removed_path = removed_ref.get_install_path(apm_modules_dir)
    except (ValueError, TypeError, AttributeError, KeyError, PathTraversalError):
        return []

    survivors: list[DependencyReference] = []
    for entry in surviving_dependencies:
        try:
            survivor = _parse_dependency_entry(entry)
            if survivor.is_local and survivor.get_install_path(apm_modules_dir) == removed_path:
                survivors.append(survivor)
        except (ValueError, TypeError, AttributeError, KeyError, PathTraversalError):
            continue
    return survivors


def survivor_ships_hooks(lockfile: object, key: str, modules_dir: Path) -> bool:
    """Whether the materialized copy of the surviving locked package *key* carries hook files."""
    from ...integration.hook_integrator import HookIntegrator

    dependency = (getattr(lockfile, "dependencies", None) or {}).get(key)
    if dependency is None:
        return False
    try:
        install_path = dependency.to_dependency_ref().get_install_path(modules_dir)
    except (ValueError, PathTraversalError):
        return False
    return install_path.is_dir() and bool(HookIntegrator().find_hook_files(install_path))
