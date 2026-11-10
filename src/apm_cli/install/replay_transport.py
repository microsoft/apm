"""Declared Git transport for lock-pinned scratch replay (#3374).

The lockfile pins *what* to fetch (host, repo, commit) but not the transport
the manifest declared (``git@host:...`` or ``ssh://`` vs ``https://``).
Install reads that transport from the declaration its breadth-first resolver
selects, so a replay that rebuilt references from the lockfile alone fell back
to the shorthand HTTPS default.

This module records the same manifests install reads -- the project's
``apm.yml`` and each materialized package's ``apm.yml`` -- and replays the
resolver's selection order to find the declaration install used for each lock
key. Declarations are expanded through the resolver itself
(:meth:`APMDependencyResolver.expand_transitive_declaration`). The selected
scheme is handed to the canonical transport owner
(:mod:`apm_cli.deps.transport_selection`) through
``DependencyReference.explicit_scheme``; this module never selects a transport.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.apm_resolver import APMDependencyResolver
    from apm_cli.deps.lockfile import LockedDependency
    from apm_cli.models.dependency.reference import DependencyReference


def _resolution_key(dep: DependencyReference) -> str:
    return dep.get_resolution_key()


@dataclass
class DeclaredTransportIndex:
    """Mirror of install's declaration selection, keyed by lock unique key.

    Install enqueues root declarations (prod, then dev) and, level by level,
    the declarations of each level's selected packages in manifest order. The
    first declaration of each resolution key is admitted; per unique key the
    winner is the shallowest, then lexically smallest resolution key, and only
    winners are expanded. Shorthand declarations take part, so they keep a
    key on the default transport even when a later manifest declares SSH.
    """

    modules_dir: Path | None = None
    _root: list[DependencyReference] = field(default_factory=list)
    _children: dict[str, list[DependencyReference]] = field(default_factory=dict)
    _selected: dict[str, DependencyReference] | None = field(default=None, repr=False)
    _resolver: APMDependencyResolver | None = field(default=None, repr=False)

    def record(self, dependencies: Iterable[DependencyReference]) -> None:
        """Record root manifest declarations in install's enqueue order."""
        self._root.extend(dep for dep in dependencies if not dep.is_parent_repo_inheritance)
        self._selected = None

    def record_package(self, package_root: Path, lock_dep: LockedDependency) -> None:
        """Record declarations of the package materialized for *lock_dep*."""
        parent_key = lock_dep.get_unique_key()
        if parent_key in self._children:
            return
        # Expand against the declaration install selected (its node reference),
        # falling back to the lock-derived reference for replacement rows.
        parent = None if lock_dep.replacement is not None else self._selection().get(parent_key)
        if parent is None:
            parent = self.apply(lock_dep, lock_dep.to_dependency_ref())
        manifest_path = package_root / "apm.yml"
        if not manifest_path.is_file():
            return
        from apm_cli.models.apm_package import APMPackage

        try:
            package = APMPackage.from_apm_yml(
                manifest_path, source_path=package_root, create_config=False
            )
        except Exception:
            # Install's resolver also skips a package whose apm.yml cannot be
            # loaded; replay surfaces content problems through its own checks.
            return
        if not lock_dep.is_local_source and not package.source:
            package = replace(package, source=parent.repo_url)
        resolver = self._expansion_resolver()
        expanded = [
            dep
            for declared in package.get_apm_dependencies()
            if (dep := resolver.expand_transitive_declaration(parent, package, declared))
            is not None
        ]
        self._children[parent_key] = expanded
        self._selected = None

    def apply(
        self, lock_dep: LockedDependency, dep_ref: DependencyReference
    ) -> DependencyReference:
        """Return *dep_ref* carrying the transport its selected declaration states."""
        if lock_dep.replacement is not None or dep_ref.explicit_scheme:
            return dep_ref
        declared = self._selection().get(lock_dep.get_unique_key())
        scheme = (declared.explicit_scheme or "").lower() if declared is not None else ""
        if not scheme or declared.is_local:
            return dep_ref
        return replace(dep_ref, explicit_scheme=scheme, ssh_user=declared.ssh_user)

    def _selection(self) -> dict[str, DependencyReference]:
        if self._selected is None:
            self._selected = self._select()
        return self._selected

    def _select(self) -> dict[str, DependencyReference]:
        selected: dict[str, DependencyReference] = {}
        admitted: set[str] = set()
        level = list(self._root)
        while level:
            fresh: list[DependencyReference] = []
            for dep in level:
                key = _resolution_key(dep)
                if key in admitted or dep.get_unique_key() in selected:
                    continue
                admitted.add(key)
                fresh.append(dep)
            winners: dict[str, DependencyReference] = {}
            for dep in fresh:
                unique_key = dep.get_unique_key()
                current = winners.get(unique_key)
                if current is None or _resolution_key(dep) < _resolution_key(current):
                    winners[unique_key] = dep
            selected.update(winners)
            level = [
                child
                for dep in fresh
                if winners[dep.get_unique_key()] is dep
                for child in self._children.get(dep.get_unique_key(), ())
            ]
        return selected

    def _expansion_resolver(self) -> APMDependencyResolver:
        if self._resolver is None:
            from apm_cli.deps.apm_resolver import APMDependencyResolver

            self._resolver = APMDependencyResolver(read_only=True, apm_modules_dir=self.modules_dir)
        return self._resolver
