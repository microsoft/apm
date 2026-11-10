"""Declared Git transport for lock-pinned scratch replay (#3374).

The lockfile pins *what* to fetch (host, repo, commit) but not the transport
the manifest declared (``git@host:...`` or ``ssh://`` vs ``https://``).
Install reads that transport from the declaring manifest, so a replay that
rebuilt references from the lockfile alone fell back to the shorthand HTTPS
default. This module recovers the declared transport from the same manifests
install reads -- the project's ``apm.yml`` for direct dependencies and each
materialized package's ``apm.yml`` for transitive ones -- and hands it to the
canonical transport owner (:mod:`apm_cli.deps.transport_selection`) through
``DependencyReference.explicit_scheme``. It never selects a transport itself.
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


@dataclass(frozen=True)
class DeclaredTransport:
    """The user-stated scheme (and SSH user) of one declared dependency."""

    scheme: str
    ssh_user: str | None = None


@dataclass
class DeclaredTransportIndex:
    """First-declaration-wins map from lock key to declared transport.

    Root declarations are recorded before any package, and packages are
    recorded in lockfile depth order, mirroring the breadth-first order in
    which install's resolver first admits each dependency key.
    """

    _by_key: dict[str, DeclaredTransport] = field(default_factory=dict)
    _resolver: APMDependencyResolver | None = field(default=None, repr=False)

    def record(
        self,
        dependencies: Iterable[DependencyReference],
        *,
        parent: DependencyReference | None = None,
    ) -> None:
        """Record explicit transports declared by one manifest."""
        for dep in dependencies:
            if dep.is_parent_repo_inheritance:
                if parent is None:
                    continue
                try:
                    dep = self._parent_resolver().expand_parent_repo_decl(parent, dep)
                except ValueError:
                    continue
            scheme = (dep.explicit_scheme or "").lower()
            if not scheme or dep.is_local or dep.replacement is not None:
                continue
            self._by_key.setdefault(
                dep.get_unique_key(),
                DeclaredTransport(scheme=scheme, ssh_user=dep.ssh_user),
            )

    def record_package(self, package_root: Path, parent: DependencyReference) -> None:
        """Record transports declared by a materialized package's ``apm.yml``."""
        manifest_path = package_root / "apm.yml"
        if not manifest_path.is_file():
            return
        from apm_cli.models.apm_package import APMPackage

        try:
            dependencies = APMPackage.from_apm_yml(manifest_path).get_apm_dependencies()
        except Exception:
            # Install's resolver also skips a package whose apm.yml cannot be
            # loaded; replay surfaces content problems through its own checks.
            return
        self.record(dependencies, parent=parent)

    def apply(
        self, lock_dep: LockedDependency, dep_ref: DependencyReference
    ) -> DependencyReference:
        """Return *dep_ref* carrying the transport its manifest declared."""
        if lock_dep.replacement is not None or dep_ref.explicit_scheme:
            return dep_ref
        declared = self._by_key.get(lock_dep.get_unique_key())
        if declared is None:
            return dep_ref
        return replace(dep_ref, explicit_scheme=declared.scheme, ssh_user=declared.ssh_user)

    def _parent_resolver(self) -> APMDependencyResolver:
        if self._resolver is None:
            from apm_cli.deps.apm_resolver import APMDependencyResolver

            self._resolver = APMDependencyResolver(read_only=True)
        return self._resolver
