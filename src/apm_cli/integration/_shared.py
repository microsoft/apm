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


def resolve_locked_apm_yml_paths(
    apm_modules_dir: Path,
    lock_path: Path | None,
) -> tuple[list[Path] | None, builtins.set]:
    """Resolve apm.yml paths from the lockfile.

    Returns ``(locked_paths_or_None, direct_paths_set)``.
    When *locked_paths* is ``None`` the caller should fall back to rglob.
    """
    locked_paths: builtins.set | None = None
    direct_paths: builtins.set = builtins.set()

    if lock_path and lock_path.exists():
        lockfile = LockFile.read(lock_path)
        if lockfile is not None:
            locked_paths = builtins.set()
            for dep in lockfile.get_package_dependencies():
                if dep.repo_url:
                    yml = (
                        apm_modules_dir / dep.repo_url / dep.virtual_path / "apm.yml"
                        if dep.virtual_path
                        else apm_modules_dir / dep.repo_url / "apm.yml"
                    )
                    locked_paths.add(yml.resolve())
                    if dep.depth == 1:
                        direct_paths.add(yml.resolve())

    if locked_paths is not None:
        resolved = [path for path in sorted(locked_paths) if path.exists()]
        return resolved, direct_paths

    return None, direct_paths
