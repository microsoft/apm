"""Declaration-aware orphan selection for installed packages (#3879).

Owner of which installed ``apm_modules/`` roots no longer belong to the
retained dependency graph, and of the backward lockfile edge scan that finds
candidate orphans. ``apm prune``, ``apm deps``, ``apm uninstall`` and the
install pipeline all route through it; the commands layer re-exports the
historical underscore names.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ..deps.lockfile import LockFile, lockfile_parent_identity
from ..models.dependency.materialization import (
    CachedMaterializationPathReader,
    installed_package_path,
)
from ..models.dependency.selection import parse_dependency_entry
from ..utils.path_security import PathTraversalError, validate_path_segments


def build_expected_install_paths(
    declared_deps, lockfile, apm_modules_dir: Path, *, preserve_installed_case: bool = False
) -> set:
    """Build expected package paths under *apm_modules_dir*.

    Combines direct deps (from ``apm.yml``) with the lock entries
    ``deps.reachability.retained_lock_keys`` retains, using ``get_install_path()`` for
    consistency with how packages are actually installed. Prune opts into
    recognizing existing case-equivalent paths without renaming them.
    """
    from ..deps.reachability import locked_marketplace_form, retained_lock_keys

    reader = CachedMaterializationPathReader()

    def expected_path(dependency):
        if preserve_installed_case:
            return installed_package_path(dependency, apm_modules_dir, reader=reader)
        return dependency.get_install_path(apm_modules_dir)

    expected = set()
    marketplace_index = lockfile.marketplace_index() if lockfile else None
    for dep in declared_deps:
        if dep.is_marketplace and lockfile:
            dep = locked_marketplace_form(dep, lockfile, marketplace_index)
        if dep.is_marketplace:
            # An object-form declaration with no lock row was never materialized (#3960).
            continue
        install_path = expected_path(dep)
        try:
            relative_path = install_path.relative_to(apm_modules_dir)
            expected.add(relative_path.as_posix())
        except ValueError:
            expected.add(str(install_path))

    if lockfile:
        retained = retained_lock_keys(
            lockfile, apm_modules_dir.parent, apm_modules_dir, list(declared_deps)
        )
        for dep in lockfile.get_package_dependencies():
            if dep.get_unique_key() in retained:
                install_path = expected_path(dep.to_dependency_ref())
                try:
                    relative_path = install_path.relative_to(apm_modules_dir)
                    expected.add(relative_path.as_posix())
                except ValueError:
                    pass
    return expected


def find_orphaned_packages(installed: Iterable[str], expected: set[str]) -> list[str]:
    """Select roots unrelated to the retained dependency graph.

    Keep both bundled descendants and whole roots containing needed children.
    Ancestor prefixes protect only those containing roots, not their siblings.
    Unlike dependency-list classification, deletion must retain recognized
    ancestors at every depth, including aliases and hidden subdirectories.
    """
    roots = set()
    retained = set(expected)
    for path in expected:
        try:
            validate_path_segments(path, context="orphan selection")
        except PathTraversalError:
            # Invalid tokens protect only an exact match, never a wider subtree.
            continue
        normalized = path.replace("\\", "/")
        roots.add(normalized)
        parts = normalized.split("/")
        retained.update("/".join(parts[:depth]) for depth in range(1, len(parts) + 1))

    orphaned = []
    for path in installed:
        normalized = path.replace("\\", "/")
        if normalized in retained:
            continue
        parts = normalized.split("/")
        if any("/".join(parts[:depth]) in roots for depth in range(1, len(parts))):
            continue
        orphaned.append(path)
    return sorted(orphaned)


def build_children_index(lockfile):
    """Index every recorded original parent in one pass over the locked edges."""
    children = {}
    for dep in lockfile.get_package_dependencies():
        for parent in dep.parent_keys:
            if parent is not None:
                children.setdefault(parent, []).append(dep)
    return children


def collect_orphan_candidates(
    lockfile: LockFile, packages_to_remove: list[object]
) -> tuple[set[str], set[str]]:
    """Share the backward edge scan without changing preview's cold-cache behavior."""
    removed_parents: set[str] = set()
    for package in packages_to_remove:
        try:
            reference = parse_dependency_entry(package)
            removed_parents.add(lockfile_parent_identity(reference, lockfile.replacements))
        except (ValueError, TypeError, AttributeError, KeyError):
            removed_parents.add(str(package))
    children = build_children_index(lockfile)
    candidates: set[str] = set()
    queue = list(removed_parents)
    while queue:
        for dependency in children.get(queue.pop(), ()):
            key = dependency.get_unique_key()
            if key in candidates:
                continue
            candidates.add(key)
            queue.append(lockfile_parent_identity(dependency, lockfile.replacements))
    return candidates, removed_parents
