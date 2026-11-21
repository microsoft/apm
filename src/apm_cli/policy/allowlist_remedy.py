"""The ``apm uninstall`` next step of a failed ``dependency-allowlist`` check (#4570).

``apm audit --ci`` offers ``apm uninstall <entries>`` only when this module
proves that the uninstall clears every denied dependency without breaking
another policy rule:

* each entry is selected exactly by the uninstall selection owner and is a
  shell-safe ASCII word;
* a dependency the current apm.yml declares is removed directly, whatever
  depth its lock row recorded; a transitive one goes with the direct
  dependencies the lock traces it to;
* the uninstall orphan owner removes every denied transitive dependency and
  no surviving package still reaches a denied one (forward reachability);
* no removed entry satisfies a package the policy requires.

Anything it cannot prove falls back to the admin-only advice.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..deps.lockfile import LockFile
    from ..models.apm_package import DependencyReference
    from .schema import DependencyPolicy

# One shell word every supported shell passes through unchanged.
_SHELL_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]*")


@dataclass(frozen=True)
class AllowlistProject:
    """The project state an uninstall proposal is proven against."""

    project_root: Path
    modules_dir: Path
    lock: LockFile | None
    production: tuple[object, ...]  # raw apm.yml dependencies.apm entries
    development: tuple[object, ...]  # raw apm.yml devDependencies.apm entries
    policy: DependencyPolicy


def uninstall_selectors(
    denied: Sequence[DependencyReference], project: AllowlistProject
) -> list[str]:
    """Return the ``apm uninstall`` arguments that clear *denied*, or ``[]`` if unproven."""
    if project.lock is None or not denied:
        return []
    from ..models.dependency.selection import parse_dependency_entry

    declared: dict[str, object] = {}
    for entry in (*project.production, *project.development):
        try:
            declared.setdefault(parse_dependency_entry(entry).get_unique_key(), entry)
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
    roots = _removal_roots(denied, declared, project.lock)
    if roots is None:
        return []
    selectors = [_selector(key, declared[key], project) for key in roots]
    if not all(selectors) or _removes_required(roots, declared, project.policy):
        return []
    denied_keys = {dep.get_unique_key() for dep in denied}
    if not _uninstall_clears(denied_keys, [declared[key] for key in roots], project):
        return []
    return [selector for selector in selectors if selector]


def _removal_roots(
    denied: Sequence[DependencyReference], declared: dict[str, object], lock: LockFile
) -> list[str] | None:
    """Map each denied dependency to the declared entries whose removal drops it."""
    from ..integration.mcp_config_view import DirectDependencyIndex

    index = DirectDependencyIndex(lock)
    roots: list[str] = []
    for dep in denied:
        key = dep.get_unique_key()
        found = (key,) if key in declared else tuple(sorted(index.roots(key) or ()))
        if not found or any(root not in declared for root in found):
            return None
        roots.extend(root for root in found if root not in roots)
    return roots


def _selector(key: str, entry: object, project: AllowlistProject) -> str | None:
    """Return a shell-safe identifier the uninstall selection maps to exactly *entry*."""
    from ..models.dependency.selection import (
        DependencySelectionStatus,
        parse_dependency_entry,
        select_manifest_dependency,
    )

    manifest = [*project.production, *project.development]
    candidates = (key, entry) if isinstance(entry, str) else (key,)
    for candidate in dict.fromkeys(candidates):
        if not _SHELL_SAFE.fullmatch(candidate):
            continue
        selection = select_manifest_dependency(candidate, manifest, project.lock)
        if selection.status is not DependencySelectionStatus.MATCHED:
            continue
        try:
            selected = parse_dependency_entry(selection.manifest_entry).get_unique_key()
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
        if selected == key:
            return candidate
    return None


def _removes_required(
    roots: Sequence[str], declared: dict[str, object], policy: DependencyPolicy
) -> bool:
    """Whether removing *roots* drops a package the policy requires."""
    if not policy.effective_require:
        return False
    from ..models.dependency.selection import parse_dependency_entry
    from .matcher import DependencyPolicyIndex

    removed = DependencyPolicyIndex.from_dependencies(
        parse_dependency_entry(declared[key]) for key in roots
    )
    return any(
        removed.find_name(required.split("#", 1)[0]) is not None
        for required in policy.effective_require
    )


def _uninstall_clears(
    denied_keys: set[str], removed: list[object], project: AllowlistProject
) -> bool:
    """Prove, through the orphan owner, that the uninstall drops every denied key.

    A denied transitive dependency must be among the uninstall's removed
    descendants. A denied entry removed directly must be one the surviving
    packages no longer reach (the owner's installed-path inspection).
    """
    from ..install.orphans import (
        InstalledPaths,
        OrphanOperation,
        ReachabilityProbe,
        decide_orphan_removal,
    )
    from ..utils.paths import portable_relpath

    lock = project.lock
    production, development = _survivors(removed, project)
    survivors = (*production, *development)
    roots = {key: ref for key, ref in _refs_by_key(removed).items() if key in denied_keys}
    # A removed entry the lock records as transitive is still another package's child.
    if any(getattr(lock.dependencies.get(key), "depth", None) != 1 for key in roots):
        return False
    probe = ReachabilityProbe(
        project_root=project.project_root,
        apm_modules_dir=project.modules_dir,
        direct_refs=tuple(production),
        dev_refs=tuple(development),
    )
    paths = {
        key: portable_relpath(ref.get_install_path(project.modules_dir), project.modules_dir)
        for key, ref in roots.items()
    }
    try:
        transitive = decide_orphan_removal(
            operation=OrphanOperation.UNINSTALL,
            roots=removed,
            candidate_universe=lock,
            installed_paths=None,
            resolved_keys=None,
            intended=None,
            materialized=None,
            reachability=probe,
            deployment_claims=None,
        )
        direct = decide_orphan_removal(
            operation=OrphanOperation.INSPECT,
            roots=(),
            candidate_universe=lock,
            installed_paths=InstalledPaths(
                root=project.modules_dir, packages=frozenset(paths.values())
            ),
            resolved_keys=None,
            intended=survivors,
            materialized=None,
            reachability=ReachabilityProbe(
                project_root=project.project_root,
                apm_modules_dir=project.modules_dir,
                direct_refs=survivors,
                dev_refs=(),
            ),
            deployment_claims=None,
        )
    except (OSError, ValueError):
        return False
    if not set(paths.values()) <= set(direct.removed_paths):
        return False
    return denied_keys - set(roots) <= transitive.removed_keys


def _survivors(
    removed: list[object], project: AllowlistProject
) -> tuple[list[DependencyReference], list[DependencyReference]]:
    """Parse the direct dependencies that survive removing *removed*, per section."""
    from ..models.dependency.selection import parse_dependency_entry

    gone = {ref.get_identity() for ref in map(parse_dependency_entry, removed)}
    sections: list[list[DependencyReference]] = [[], []]
    for section, entries in zip(sections, (project.production, project.development), strict=True):
        for entry in entries:
            try:
                ref = parse_dependency_entry(entry)
            except (ValueError, TypeError, AttributeError, KeyError):
                continue
            if ref.get_identity() not in gone:
                section.append(ref)
    return sections[0], sections[1]


def _refs_by_key(entries: list[object]) -> dict[str, DependencyReference]:
    from ..models.dependency.selection import parse_dependency_entry

    refs = map(parse_dependency_entry, entries)
    return {ref.get_unique_key(): ref for ref in refs}
