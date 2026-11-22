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


def _bundle_identity(path: Path) -> tuple[str, str] | None:
    """Return ``(package_id, approval_key)`` for the bundle at *path*, as install computes them."""
    from ...security.executables import local_bundle_approval_key

    info = detect_local_bundle(path)
    if info is None:
        return None
    try:
        version = str(info.plugin_json.get("version") or "")
        key = local_bundle_approval_key(info.package_id, version, info.source_dir, info.lockfile)
        return info.package_id, key
    finally:
        if info.temp_dir is not None:
            shutil.rmtree(info.temp_dir, ignore_errors=True)


def _is_bundle_path(package: str) -> bool:
    """Any existing bundle-shaped path, with or without ``./``, as install probes it."""
    path = Path(package)
    return path.exists() and looks_like_local_bundle(path)


def _is_bare_name(package: str) -> bool:
    return not DependencyReference.is_local_path(package) and not any(
        sep in package for sep in ("/", "@", ":")
    )


def _deployed(bundle_scope: _BundleScope) -> dict[str, Any]:
    from ...install.local_bundle_hook_trust import deployed_bundle_hook_keys, local_bundle_paths

    return deployed_bundle_hook_keys(
        bundle_scope.root, bundle_scope.targets, local_bundle_paths(bundle_scope.lock_dir)
    )


def _installed_slugs(bundle_scope: _BundleScope) -> set[str]:
    from ...install.local_bundle_pending import recorded_bundle_slugs

    return set(_deployed(bundle_scope)) | set(recorded_bundle_slugs(bundle_scope.lock_dir))


def _by_name_remedy(scope: Any):
    from ...core.remediation import LOCAL_BUNDLE_UNINSTALL_BY_NAME
    from ...core.scope import is_user_scope

    if is_user_scope(scope):
        return LOCAL_BUNDLE_UNINSTALL_BY_NAME.with_command("apm uninstall -g <installed-name>")
    return LOCAL_BUNDLE_UNINSTALL_BY_NAME


def _path_slug(package: str, bundle_scope: _BundleScope, *, scope: Any, logger: Any) -> str:
    """Map a bundle path to the name its hooks were installed under (``--as`` aware)."""
    identity = _bundle_identity(Path(package))
    if identity is None:
        logger.error(f"'{Path(package).name}' could not be read as a local bundle.")
        sys.exit(1)
    package_id, key = identity
    deployed = _deployed(bundle_scope)
    matches = sorted(slug for slug, state in deployed.items() if key in state.keys)
    if len(matches) > 1:
        logger.error(
            f"'{Path(package).name}' matches {len(matches)} installed local bundles "
            f"({', '.join(matches)}); no changes were made. {_by_name_remedy(scope).sentence()}"
        )
        sys.exit(1)
    return matches[0] if matches else package_id


def _resolve_scope(scope: Any, manifest_path: Path, logger: Any) -> _BundleScope | str:
    """Return the scope's bundle hook inputs, or the reason they cannot be resolved."""
    from ...core.scope import get_apm_dir, is_user_scope
    from ...install.local_bundle_hook_trust import (
        BundleHookTargetsUnresolved,
        bundle_hook_cleanup_targets,
        scope_bundle_hook_inputs,
    )
    from ...models.apm_package import APMPackage

    try:
        apm_package = APMPackage.from_apm_yml(manifest_path)
        root, targets, _allow = scope_bundle_hook_inputs(
            scope, apm_package, no_policy=True, logger=logger
        )
    except (BundleHookTargetsUnresolved, ValueError, OSError) as exc:
        return str(exc)
    # Hooks installed with --target may sit outside the manifest's targets.
    cleanup = bundle_hook_cleanup_targets(targets, user_scope=is_user_scope(scope))
    return _BundleScope(root, cleanup, get_apm_dir(scope), apm_package)


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
        if _is_bundle_path(package):
            selection = select_manifest_dependency(package, current_deps, lockfile)
            if selection.status is DependencySelectionStatus.NOT_FOUND:
                bundle_paths.append(package)
        elif _is_bare_name(package):
            names.append(package)
    if not bundle_paths and not names:
        return [], None

    bundle_scope = _resolve_scope(scope, manifest_path, logger)
    if isinstance(bundle_scope, str):
        if bundle_paths:
            logger.error(f"Local bundle hooks could not be located: {bundle_scope}")
            sys.exit(1)
        return [], None
    installed = _installed_slugs(bundle_scope) if names else set()
    slugs = [name for name in names if name in installed]
    for package in bundle_paths:
        slugs.append(_path_slug(package, bundle_scope, scope=scope, logger=logger))
    if not slugs:
        return [], None
    if len(slugs) != len(packages):
        others = [p for p in packages if p not in names or p not in installed]
        others = [p for p in others if p not in bundle_paths]
        from ...core.remediation import LOCAL_BUNDLE_UNINSTALL_APART
        from ...core.scope import is_user_scope

        remedy = LOCAL_BUNDLE_UNINSTALL_APART
        if is_user_scope(scope):
            remedy = remedy.with_command("apm uninstall -g <bundle>")
        logger.error(
            f"Local bundles cannot be uninstalled with other packages ({', '.join(others)}); "
            f"no changes were made. {remedy.sentence()}"
        )
        sys.exit(1)
    return list(dict.fromkeys(slugs)), bundle_scope


@dataclass(frozen=True)
class _Unconfirmed:
    hook_files: tuple[str, ...]


def _refuse_unsafe_cleanup(bundle_scope: _BundleScope, *, logger: Any, dry_run: bool) -> None:
    """Refuse before any write when hook configs or the lockfile cannot be safely rewritten."""
    from ...deps.lockfile import LockFile, get_lockfile_path, refuse_lockfile_before_mutation
    from ...install.local_bundle_hook_trust import bundle_hook_cleanup_blockers, local_bundle_paths
    from ...install.local_bundle_pending import pending_record_blocker

    blockers = bundle_hook_cleanup_blockers(bundle_scope.root, bundle_scope.targets)
    if pending := pending_record_blocker(bundle_scope.lock_dir):
        blockers.append(pending)
    if blockers:
        logger.error(
            f"Local bundle hooks were not removed: {', '.join(blockers)} cannot be safely "
            "read or rewritten; no changes were made. Replace a linked path with a regular "
            "file or directory, or repair the file, then retry."
        )
        sys.exit(1)
    if dry_run:
        return
    lock_path = get_lockfile_path(bundle_scope.lock_dir)
    refuse_lockfile_before_mutation(
        LockFile.read(lock_path),
        lock_path,
        would_mutate=bool(local_bundle_paths(bundle_scope.lock_dir)),
    )


def uninstall_local_bundles(
    slugs: list[str], bundle_scope: _BundleScope, *, scope: Any, logger: Any, dry_run: bool
) -> None:
    """Retire each selected bundle's hooks through the hook trust owner; exit 1 on failure."""
    from ...install.local_bundle_hook_trust import installed_local_bundles, retire_bundle_hooks
    from ...install.local_bundle_pending import drop_bundle_exec_type

    root, targets, lock_dir = bundle_scope.root, bundle_scope.targets, bundle_scope.lock_dir
    deployed = _deployed(bundle_scope)
    missing = [slug for slug in slugs if slug not in deployed]
    for slug in missing:
        logger.error(
            f"Local bundle '{slug}' has no hooks deployed. 'apm uninstall' removes a "
            "local bundle's deployed hooks only; no changes were made."
        )
    if missing:
        if others := sorted(deployed):
            logger.error(
                f"Local bundles with deployed hooks: {', '.join(others)}. "
                f"{_by_name_remedy(scope).sentence()}"
            )
        sys.exit(1)
    _refuse_unsafe_cleanup(bundle_scope, logger=logger, dry_run=dry_run)
    if dry_run:
        for slug in slugs:
            logger.dry_run_notice(f"Would remove hooks from local bundle '{slug}'")
        return

    unconfirmed: dict[str, tuple[str, ...]] = {}
    for slug in slugs:
        retired = retire_bundle_hooks(root, targets, slug, lock_dir=lock_dir, reason=None)
        if retired.failed_paths:
            unconfirmed[slug] = retired.failed_paths
    left = _deployed(bundle_scope)
    for slug, paths in unconfirmed.items():
        left.setdefault(slug, _Unconfirmed(paths))
    # Pending state goes only for a bundle whose hooks are confirmed gone.
    for slug in slugs:
        if slug not in left:
            drop_bundle_exec_type(lock_dir, slug, "hooks")
    if failed := [slug for slug in slugs if slug in left]:
        for slug in failed:
            files = ", ".join(sorted(left[slug].hook_files)) or "merged hook configuration"
            logger.error(
                f"Hooks from local bundle '{slug}' could not all be removed ({files}); "
                "they stay recorded. Remove or repair those files, then retry."
            )
        sys.exit(1)
    for slug in slugs:
        logger.success(f"Removed hooks from local bundle '{slug}'", symbol="check")
    _names, kept = installed_local_bundles(scope, bundle_scope.apm_package)
    if kept:
        from ...core.remediation import kept_note

        logger.warning(
            kept_note(
                f"{kept} other file(s) from local bundles",
                "'apm uninstall' removes a local bundle's hooks only",
            )
        )


def uninstall_selected_local_bundles(
    packages: tuple[str, ...] | list[str],
    current_deps: list,
    lockfile: Any,
    *,
    scope: Any,
    manifest_path: Path,
    logger: Any,
    dry_run: bool,
) -> bool:
    """Uninstall the local bundles *packages* name; ``False`` when they name none."""
    slugs, bundle_scope = select_local_bundles(
        packages, current_deps, lockfile, scope=scope, manifest_path=manifest_path, logger=logger
    )
    if bundle_scope is None:
        return False
    uninstall_local_bundles(slugs, bundle_scope, scope=scope, logger=logger, dry_run=dry_run)
    return True
