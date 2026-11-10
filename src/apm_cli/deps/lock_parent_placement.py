"""Legacy lockfile parent placement (canonical owner).

Single owner for the question: "which locked entry declared this transitive
dependency?" for lockfiles without a ``replacements`` block. Those lockfiles
record ``resolved_by`` as the parent's ``repo_url`` (first-wins) plus a
``depth``. Sibling virtual subdirectory packages from one repository share
that ``repo_url``, so ``resolved_by`` alone cannot tell them apart (#3510).

Placement keeps the lockfile as the source of truth and only consults the
installed manifests of same-depth candidates when the lock data is ambiguous.
A child is attached only when exactly one candidate's manifest declares it;
anything else (several declarers, unreadable or missing manifests) stays
unresolved so callers keep their existing fallback.

``apm deps tree`` and ``apm deps why`` both route through this module.
Replacement-aware lockfiles record exact parent keys and never need it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.constants import APM_YML_FILENAME

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency

DeclaredChildren = Callable[["LockedDependency"], "frozenset[str] | None"]
"""Return the unique keys a locked package's manifest declares, or ``None``
when that cannot be established."""


@dataclass(frozen=True)
class LegacyParentPlacement:
    """Placement of transitive lock entries under their declaring parent.

    Attributes:
        parents: Child unique key -> the locked entry that declared it.
        unresolved: Transitive entries whose declaring parent could not be
            established unambiguously, in input order.
    """

    parents: dict[str, LockedDependency]
    unresolved: tuple[LockedDependency, ...]


def _no_manifest_evidence(_dep: LockedDependency) -> frozenset[str] | None:
    return None


def place_legacy_lock_parents(
    deps: Iterable[LockedDependency],
    declared_children: DeclaredChildren | None = None,
) -> LegacyParentPlacement:
    """Place every transitive entry (``depth > 1``) under one parent entry.

    Candidates are the entries one level shallower whose unique key, or
    failing that whose ``repo_url``, equals the child's ``resolved_by``.
    When several candidates remain, *declared_children* narrows them to the
    ones whose manifest declares the child; any candidate without manifest
    evidence keeps the child unresolved.
    """
    entries = list(deps)
    reader = declared_children or _no_manifest_evidence
    by_unique_key: dict[tuple[int, str], list[LockedDependency]] = {}
    by_repo_url: dict[tuple[int, str], list[LockedDependency]] = {}
    for entry in entries:
        by_unique_key.setdefault((entry.depth, entry.get_unique_key()), []).append(entry)
        by_repo_url.setdefault((entry.depth, entry.repo_url), []).append(entry)

    parents: dict[str, LockedDependency] = {}
    unresolved: list[LockedDependency] = []
    for dep in entries:
        if dep.depth <= 1:
            continue
        lookup = (dep.depth - 1, dep.resolved_by or "")
        candidates = by_unique_key.get(lookup) or by_repo_url.get(lookup, [])
        if len(candidates) > 1:
            candidates = _declaring_candidates(dep, candidates, reader)
        if len(candidates) == 1:
            parents[dep.get_unique_key()] = candidates[0]
        else:
            unresolved.append(dep)
    return LegacyParentPlacement(parents=parents, unresolved=tuple(unresolved))


def _declaring_candidates(
    dep: LockedDependency,
    candidates: list[LockedDependency],
    reader: DeclaredChildren,
) -> list[LockedDependency]:
    child_key = dep.get_unique_key()
    declaring: list[LockedDependency] = []
    for candidate in candidates:
        declared = reader(candidate)
        if declared is None:
            return []
        if child_key in declared:
            declaring.append(candidate)
    return declaring


def installed_manifest_children(apm_modules_dir: Path) -> DeclaredChildren:
    """Return a cached, read-only reader of installed manifests.

    ``{git: parent, path: ...}`` declarations are expanded through the
    resolver's canonical parent-reference owner before their keys are
    collected. An installed package without ``apm.yml`` declares no APM
    dependencies. A missing install directory, an unreadable manifest, or an
    inherited reference that cannot be expanded yields ``None``.
    """
    from apm_cli.deps.apm_resolver import APMDependencyResolver
    from apm_cli.models.apm_package import APMPackage

    resolver = APMDependencyResolver(apm_modules_dir=apm_modules_dir, read_only=True)
    cache: dict[str, frozenset[str] | None] = {}

    def read(dep: LockedDependency) -> frozenset[str] | None:
        key = dep.get_unique_key()
        if key in cache:
            return cache[key]
        declared: frozenset[str] | None = None
        try:
            parent_ref = dep.to_dependency_ref()
            install_dir = parent_ref.get_install_path(apm_modules_dir)
            manifest = install_dir / APM_YML_FILENAME
            if manifest.is_file():
                package = APMPackage.from_apm_yml(
                    manifest, source_path=install_dir, create_config=False
                )
                declared = frozenset(
                    (
                        resolver.expand_parent_repo_decl(parent_ref, ref)
                        if ref.is_parent_repo_inheritance
                        else ref
                    ).get_unique_key()
                    for ref in package.get_apm_dependencies()
                )
            elif install_dir.is_dir():
                declared = frozenset()
        except (OSError, ValueError):
            declared = None
        cache[key] = declared
        return declared

    return read
