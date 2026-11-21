"""Uninstall a local plugin bundle's hooks (#3700).

A local bundle (``apm install <bundle>``) is not declared in ``apm.yml``. Its
hooks carry per-bundle ownership (merged-entry source markers, per-file
approval records and ``hooks/<slug>/`` scripts), so ``apm uninstall`` selects
a bundle by its path or name and retires those hooks through the hook trust
owner, :func:`~apm_cli.install.local_bundle_hook_trust.retire_bundle_hooks`.
Other files a bundle deployed share one lockfile owner and are kept.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...bundle.local_bundle import detect_local_bundle, looks_like_local_bundle
from ...models.apm_package import DependencyReference
from ...models.dependency.selection import DependencySelectionStatus, select_manifest_dependency


@dataclass(frozen=True)
class _BundleScope:
    root: Path
    targets: Any
    lock_dir: Path
    apm_package: Any


def _bundle_slug(path: Path) -> str | None:
    info = detect_local_bundle(path)
    if info is None:
        return None
    try:
        return info.package_id
    finally:
        if info.temp_dir is not None:
            shutil.rmtree(info.temp_dir, ignore_errors=True)


def _is_bare_name(package: str) -> bool:
    return not DependencyReference.is_local_path(package) and not any(
        sep in package for sep in ("/", "@", ":")
    )


def _installed_slugs(bundle_scope: _BundleScope) -> set[str]:
    from ...install.local_bundle_hook_trust import deployed_bundle_hook_keys, local_bundle_paths
    from ...install.local_bundle_pending import recorded_bundle_slugs

    deployed = deployed_bundle_hook_keys(
        bundle_scope.root, bundle_scope.targets, local_bundle_paths(bundle_scope.lock_dir)
    )
    return set(deployed) | set(recorded_bundle_slugs(bundle_scope.lock_dir))


def _resolve_scope(scope: Any, manifest_path: Path, logger: Any) -> _BundleScope:
    from ...core.scope import get_apm_dir
    from ...install.local_bundle_hook_trust import (
        BundleHookTargetsUnresolved,
        scope_bundle_hook_inputs,
    )
    from ...models.apm_package import APMPackage

    try:
        apm_package = APMPackage.from_apm_yml(manifest_path)
        root, targets, _allow = scope_bundle_hook_inputs(
            scope, apm_package, no_policy=True, logger=logger
        )
    except (BundleHookTargetsUnresolved, ValueError, OSError) as exc:
        logger.error(f"Local bundle hooks could not be located: {exc}")
        sys.exit(1)
    return _BundleScope(root, targets, get_apm_dir(scope), apm_package)


def select_local_bundles(
    packages: tuple[str, ...] | list[str],
    current_deps: list,
    lockfile: Any,
    *,
    scope: Any,
    manifest_path: Path,
    logger: Any,
) -> tuple[list[str], _BundleScope | None]:
    """Return the bundle slugs *packages* name, or ``([], None)`` when they name none.

    A path is a bundle when it has a bundle's shape and is not declared in
    ``apm.yml``; a bare name is a bundle when a local bundle of that name is
    installed. Mixing bundles with packages, or naming a bundle path that is
    not a readable bundle, exits 1 before any write.
    """
    bundle_paths: list[str] = []
    names: list[str] = []
    for package in packages:
        if DependencyReference.is_local_path(package) and looks_like_local_bundle(Path(package)):
            selection = select_manifest_dependency(package, current_deps, lockfile)
            if selection.status is DependencySelectionStatus.NOT_FOUND:
                bundle_paths.append(package)
        elif _is_bare_name(package):
            names.append(package)
    if not bundle_paths and not names:
        return [], None

    bundle_scope = _resolve_scope(scope, manifest_path, logger)
    installed = _installed_slugs(bundle_scope) if names else set()
    slugs = [name for name in names if name in installed]
    for package in bundle_paths:
        slug = _bundle_slug(Path(package))
        if slug is None:
            logger.error(f"'{Path(package).name}' could not be read as a local bundle.")
            sys.exit(1)
        slugs.append(slug)
    if not slugs:
        return [], None
    if len(slugs) != len(packages):
        others = [p for p in packages if p not in names or p not in installed]
        others = [p for p in others if p not in bundle_paths]
        logger.error(
            f"Uninstall local bundles apart from other packages ({', '.join(others)}): "
            "run 'apm uninstall' once for the bundles and once for the rest; "
            "no changes were made."
        )
        sys.exit(1)
    return list(dict.fromkeys(slugs)), bundle_scope


def uninstall_local_bundles(
    slugs: list[str], bundle_scope: _BundleScope, *, scope: Any, logger: Any, dry_run: bool
) -> None:
    """Retire each selected bundle's hooks through the hook trust owner; exit 1 on failure."""
    from ...install.local_bundle_hook_trust import (
        deployed_bundle_hook_keys,
        installed_local_bundles,
        local_bundle_paths,
        retire_bundle_hooks,
    )
    from ...install.local_bundle_pending import drop_bundle_exec_type, recorded_bundle_slugs

    root, targets, lock_dir = bundle_scope.root, bundle_scope.targets, bundle_scope.lock_dir
    deployed = deployed_bundle_hook_keys(root, targets, local_bundle_paths(lock_dir))
    recorded = set(recorded_bundle_slugs(lock_dir, exec_type="hooks"))
    missing = [slug for slug in slugs if slug not in deployed and slug not in recorded]
    for slug in missing:
        logger.error(
            f"Local bundle '{slug}' has no hooks installed. 'apm uninstall' removes a "
            "local bundle's hooks only; no changes were made."
        )
    if missing:
        sys.exit(1)
    if dry_run:
        for slug in slugs:
            logger.dry_run_notice(f"Would remove hooks from local bundle '{slug}'")
        return

    for slug in slugs:
        retire_bundle_hooks(root, targets, slug, lock_dir=lock_dir, reason=None)
        drop_bundle_exec_type(lock_dir, slug, "hooks")
    left = deployed_bundle_hook_keys(root, targets, local_bundle_paths(lock_dir))
    if failed := [slug for slug in slugs if slug in left]:
        for slug in failed:
            logger.error(
                f"Hooks from local bundle '{slug}' could not all be removed. Check that the "
                "hook configuration files are writable, then retry."
            )
        sys.exit(1)
    for slug in slugs:
        logger.success(f"Removed hooks from local bundle '{slug}'", symbol="check")
    _names, kept = installed_local_bundles(scope, bundle_scope.apm_package)
    if kept:
        logger.warning(
            f"Kept {kept} other file(s) from local bundles: 'apm uninstall' removes a "
            "local bundle's hooks only."
        )
