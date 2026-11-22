"""Installed packages whose content agent-context writers read (compile, run, preview)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple


class InstalledDependency(NamedTuple):
    """One installed package: lock key, reference, materialized root and deployed files."""

    key: str
    ref: Any
    root: Path
    deployed: frozenset[str]


def installed_dependency_subjects(root: Path, *, user_scope: bool) -> list[InstalledDependency]:
    """Every installed package whose content agent-context writers read under *root*.

    Mirrors :func:`~apm_cli.primitives.discovery.get_dependency_declaration_order`:
    locked packages, the scope manifest's installed direct dependencies (a lost
    or partial lock still leaves them readable) and lockfile-proven local bundle
    staging. Sorted by key; the first record of a key wins.
    """
    from apm_cli.core.scope import scope_manifest_path
    from apm_cli.deps.lockfile import LockFile, resolve_lockfile_path_for_read
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.models.dependency.reference import DependencyReference

    found: dict[str, InstalledDependency] = {}
    modules_root = root / "apm_modules"
    lockfile = LockFile.read(resolve_lockfile_path_for_read(root, read_only=True))
    if lockfile is not None:
        for key, dep in lockfile.dependencies.items():
            if key != ".":
                ref = dep.to_dependency_ref()
                found.setdefault(
                    key,
                    InstalledDependency(
                        key, ref, ref.get_install_path(modules_root), frozenset(dep.deployed_files)
                    ),
                )
        for slug in lockfile.local_staging_slugs():
            ref = DependencyReference(repo_url=f"_local/{slug}", is_local=True, source="local")
            found.setdefault(
                ref.get_unique_key(),
                InstalledDependency(ref.get_unique_key(), ref, modules_root / slug, frozenset()),
            )
    manifest = scope_manifest_path(root, user_scope=user_scope)
    declared: list[Any] = []
    if manifest.is_file() and modules_root.is_dir():
        try:
            declared = APMPackage.from_apm_yml(manifest).get_apm_dependencies()
        except ValueError:
            # The calling command reports an invalid manifest before it reads content.
            declared = []
    for dep in declared:
        # Marketplace identities resolve at install; the lock names them (an
        # unresolved one has no install path).
        if dep.is_marketplace:
            continue
        install_path = dep.get_install_path(modules_root)
        if install_path.is_dir():
            found.setdefault(
                dep.get_unique_key(),
                InstalledDependency(dep.get_unique_key(), dep, install_path, frozenset()),
            )
    return [found[key] for key in sorted(found)]
