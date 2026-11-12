"""Shared helpers for MCP and LSP integrators.

Extracted to satisfy the R0801 (duplicate-code) lint gate.
"""

from __future__ import annotations

import builtins
from pathlib import Path

from apm_cli.deps.lockfile import LockFile


def deduplicate_deps(deps: list) -> list:
    """Deduplicate dependency entries by name; first occurrence wins.

    Root deps are listed before transitive, so root overlays take
    precedence.  Works with any object that has a ``name`` attribute,
    plain dicts with a ``"name"`` key, or bare strings.
    """
    seen_names: builtins.set = builtins.set()
    result: list = []
    for dep in deps:
        if hasattr(dep, "name"):
            name = dep.name
        elif isinstance(dep, dict):
            name = dep.get("name", "")
        else:
            name = str(dep)
        if not name:
            if dep not in result:
                result.append(dep)
            continue
        if name not in seen_names:
            seen_names.add(name)
            result.append(dep)
    return result


def resolve_locked_apm_yml_sources(
    apm_modules_dir: Path,
    lock_path: Path | None,
) -> tuple[list[tuple[Path, object]] | None, builtins.set]:
    """Resolve package manifest paths and dependency records from the lockfile.

    Returns ``(locked_sources_or_None, direct_paths_set)``. Each source pairs an
    ``apm.yml`` path with its locked dependency so consumers can retain exact
    identity and provenance. When *locked_sources* is ``None`` the caller should
    fall back to rglob.
    """
    locked_sources: dict[Path, object] | None = None
    direct_paths: builtins.set = builtins.set()

    if lock_path and lock_path.exists():
        lockfile = LockFile.read(lock_path)
        if lockfile is not None:
            locked_sources = {}
            for dep in lockfile.get_package_dependencies():
                if dep.repo_url:
                    package_root = dep.to_dependency_ref().get_install_path(apm_modules_dir)
                    yml = package_root / "apm.yml"
                    if yml.is_symlink():
                        raise ValueError(f"Locked package manifest must not be a symlink: {yml}")
                    resolved_yml = yml.resolve()
                    try:
                        resolved_yml.relative_to(package_root.resolve())
                    except ValueError as exc:
                        raise ValueError(
                            f"Locked package manifest escapes its package root: {yml}"
                        ) from exc
                    locked_sources[resolved_yml] = dep
                    if dep.depth == 1:
                        direct_paths.add(resolved_yml)

    if locked_sources is not None:
        resolved = [
            (path, locked_sources[path]) for path in sorted(locked_sources) if path.exists()
        ]
        return resolved, direct_paths

    return None, direct_paths


def resolve_locked_apm_yml_paths(
    apm_modules_dir: Path,
    lock_path: Path | None,
) -> tuple[list[Path] | None, builtins.set]:
    """Resolve manifest paths while preserving the legacy path-only API."""
    sources, direct_paths = resolve_locked_apm_yml_sources(apm_modules_dir, lock_path)
    if sources is None:
        return None, direct_paths
    return [path for path, _dependency in sources], direct_paths
