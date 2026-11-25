"""Register a local plugin bundle's hooks through the canonical hook integrator.

A plugin bundle ships its hooks as plugin-native ``hooks.json`` plus
``hooks/`` resources. Copying them as files would leave them unregistered,
so this module stages the verified bundle bytes, interprets them with the
same Claude-plugin normalization a source install uses, and hands the
resulting package view to :meth:`HookIntegrator.integrate_hooks_for_target`.
It never writes hook configuration itself (#3700).
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils.path_security import ensure_path_within, validate_path_segments
from .local_bundle_paths import is_plugin_hook_path


@dataclass
class BundleHookResult:
    """Files the hook integrator deployed for one local bundle."""

    scripts: list[str] = field(default_factory=list)
    script_hashes: dict[str, str] = field(default_factory=dict)
    # Scripts actually written this run, never adopted identical files (#4223).
    written: list[str] = field(default_factory=list)
    hook_files: int = 0
    skipped: int = 0


def integrate_local_bundle_hooks(
    bundle_info: Any,
    pack_files: dict[str, str],
    project_root: Path,
    *,
    targets: Any,
    slug: str,
    hooks_approved: bool | None,
    approval_key: str | None,
    force: bool,
    dry_run: bool,
    scope: Any,
    diagnostics: Any = None,
    logger: Any = None,
) -> BundleHookResult:
    """Register the bundle's plugin hooks for every hook-capable target.

    *hooks_approved* is ``True`` when approved, ``False`` when pending
    approval, and ``None`` when the user denied this exact bundle's hooks.
    """
    from .local_bundle_paths import bundle_pack_files, bundle_slug_validation_error

    result = BundleHookResult()
    hook_paths = sorted(rel for rel in pack_files if is_plugin_hook_path(rel))
    # A reinstall whose hooks will not register retires the earlier version's.
    retire = not dry_run and bundle_slug_validation_error(slug) is None
    if not hook_paths:
        if retire:
            _retire_bundle_hooks(project_root, targets, slug, scope=scope, diagnostics=diagnostics)
        return result
    if not hooks_approved:
        result.skipped = len(hook_paths)
        retired = retire and _retire_bundle_hooks(
            project_root, targets, slug, scope=scope, diagnostics=diagnostics
        )
        from ..core.scope import InstallScope
        from ..security.executables import local_bundle_approval_hint

        lead = (
            f"Retired hooks from local bundle '{slug}'"
            if retired
            else f"Skipped {len(hook_paths)} bundle hook file(s) from {slug}"
        )
        user_scope = scope == InstallScope.USER
        if hooks_approved is None:
            from .local_bundle_handler import bundle_denied_hint

            reason = bundle_denied_hint(str(approval_key), "hooks", user_scope=user_scope)
        else:
            reason = (
                "hooks are executable and not approved for this exact bundle content. "
                + local_bundle_approval_hint(str(approval_key), "hooks", user_scope=user_scope)
            )
        _warn(f"{lead}: {reason}", slug, diagnostics, logger)
        return result
    hook_targets = [target for target in targets if target.supports("hooks")]
    if not hook_targets:
        return result

    verified_files = bundle_pack_files(bundle_info)
    if not verified_files:
        result.skipped = len(hook_paths)
        if retire:
            _retire_bundle_hooks(project_root, targets, slug, scope=scope, diagnostics=diagnostics)
        _warn(
            f"Skipped {len(hook_paths)} bundle hook file(s) from {slug}: the bundle has "
            "no integrity manifest. Repack it with 'apm pack' to register its hooks.",
            slug,
            diagnostics,
            logger,
        )
        return result

    if slug_error := bundle_slug_validation_error(slug):
        result.skipped = len(hook_paths)
        _warn(
            f"Skipped bundle hooks for unsafe slug {slug!r}: {slug_error}",
            slug,
            diagnostics,
            logger,
        )
        return result
    if dry_run:
        if logger is not None:
            logger.verbose_detail(f"[dry-run] would register hooks from {len(hook_paths)} file(s)")
        return result

    from ..core.scope import InstallScope
    from ..integration.hook_integrator import HookIntegrator
    from ..utils.content_hash import compute_file_hash

    user_scope = scope == InstallScope.USER
    managed_files = _prior_bundle_claims(project_root, user_scope=user_scope)
    with tempfile.TemporaryDirectory(prefix="apm-bundle-hooks-") as tmp:
        package_info = _bundle_hook_package(bundle_info, verified_files, Path(tmp) / slug, slug)
        package_info.local_bundle_approval = approval_key
        # Entries recorded under an earlier key are replaced, never kept beside.
        from .local_bundle_hook_trust import retire_merged_bundle_entries

        retire_merged_bundle_entries(project_root, targets, slug)
        integrator = HookIntegrator()
        for target in hook_targets:
            outcome = integrator.integrate_hooks_for_target(
                target,
                package_info,
                project_root,
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
                scope=scope,
                user_scope=user_scope,
            )
            result.hook_files += outcome.files_integrated
            _record_per_file_approval(
                target, project_root, slug, approval_key, outcome.target_paths, user_scope
            )
            for target_path in outcome.target_paths:
                dest = Path(target_path)
                record = _record_path(dest, project_root, user_scope=user_scope)
                if record in result.script_hashes:
                    continue
                result.scripts.append(record)
                result.script_hashes[record] = compute_file_hash(dest)
        written = {
            _record_path(Path(path), project_root, user_scope=user_scope)
            for path in integrator.take_rendered_writes()
        }
        result.written = [record for record in result.scripts if record in written]
    if result.hook_files and logger is not None:
        logger.verbose_detail(f"Registered {result.hook_files} hook file(s) from bundle '{slug}'")
    return result


def _bundle_hook_package(bundle_info: Any, verified_files: dict[str, str], stage: Path, slug: str):
    """Stage integrity-listed bundle bytes and load them as the Claude plugin they are.

    Only files in ``pack.bundle_files`` are staged, and the staged copy is
    re-verified, so hooks come only from bytes the approval key covers.
    """
    from ..bundle.local_bundle import verify_bundle_integrity
    from ..models.apm_package import PackageInfo
    from ..models.validation import PackageType, validate_legacy_marketplace_plugin
    from ..utils.helpers import find_plugin_json

    bundle_dir = Path(bundle_info.source_dir)
    stage.mkdir(parents=True)
    for rel in verified_files:
        validate_path_segments(rel, context="bundle hook staging path")
        src = bundle_dir / rel
        if not src.is_file() or src.is_symlink():
            continue
        dest = stage / rel
        ensure_path_within(dest, stage)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest, follow_symlinks=False)
    if errors := verify_bundle_integrity(stage, bundle_info.lockfile):
        raise ValueError(f"Bundle hooks from {slug!r} changed after verification: {errors[0]}")
    validation = validate_legacy_marketplace_plugin(stage, find_plugin_json(stage))
    if validation.errors or validation.package is None:
        raise ValueError(
            f"Bundle hooks from {slug!r} could not be read: {'; '.join(validation.errors)}"
        )
    return PackageInfo(
        package=validation.package,
        install_path=stage,
        package_type=PackageType.MARKETPLACE_PLUGIN,
        local_bundle_id=slug,
    )


def _retire_bundle_hooks(
    project_root: Path,
    targets: Any,
    slug: str,
    *,
    scope: Any,
    diagnostics: Any = None,
) -> bool:
    """Retire what this bundle deployed earlier through the hook trust owner (#4216).

    True when the bundle had deployed hooks and all of them were retired. Hooks
    that could not be retired stay for a retry and fail the install: recorded
    as an error diagnostic, or raised when there is no collector.
    """
    from ..core.scope import InstallScope, get_apm_dir
    from .local_bundle_hook_trust import BundleHookRetirementIncomplete, retire_bundle_hooks

    user_scope = scope == InstallScope.USER
    retired = retire_bundle_hooks(
        project_root,
        targets,
        slug,
        lock_dir=get_apm_dir(InstallScope.USER) if user_scope else project_root,
        reason=None,
    )
    if not retired.failed_paths:
        return bool(retired)
    error = BundleHookRetirementIncomplete({slug: retired.failed_paths})
    if diagnostics is None:
        raise error
    diagnostics.error(str(error), package=slug)
    return False


def _prior_bundle_claims(project_root: Path, *, user_scope: bool = False) -> set[str]:
    """Return unedited bundle claims, project-relative, that a reinstall may overwrite.

    An edited claim is left out so the hook integrator's collision check keeps
    the edit unless ``--force`` is passed, as the bundle file copy does. At
    user scope the claims live in the user lockfile (``$APM_HOME``), recorded
    relative to the home deploy root.
    """
    from ..core.deployment_ledger import DeploymentLedgerCodec
    from ..core.scope import InstallScope, get_apm_dir
    from ..deps.lockfile import LockFile, get_lockfile_path
    from ..utils.content_hash import compute_file_hash

    lock_dir = get_apm_dir(InstallScope.USER) if user_scope else project_root
    lockfile = LockFile.read(get_lockfile_path(lock_dir))
    if lockfile is None:
        return set()
    ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
    claims: set[str] = set()
    for value in DeploymentLedgerCodec.local_bundle_values(ledger):
        path = Path(value) if Path(value).is_absolute() else project_root / value
        recorded = lockfile.local_deployed_file_hashes.get(value)
        if not path.is_file() or recorded != compute_file_hash(path):
            continue
        claims.add(_record_path(path, project_root, user_scope=False))
    return claims


def _record_per_file_approval(
    target: Any,
    project_root: Path,
    slug: str,
    approval_key: str | None,
    target_paths: Any,
    user_scope: bool,
) -> None:
    """Record the approval key for a per-file hook target's bundle hook files (#4027)."""
    from ..integration.hook_integrator import _APM_HOOKS_SIDECAR, _MERGE_HOOK_TARGETS
    from ..integration.hook_ownership import write_per_file_bundle_record

    if target.name in _MERGE_HOOK_TARGETS:
        return
    hooks_dir = project_root / target.root_dir / "hooks"
    hook_files = [
        _record_path(Path(path), project_root, user_scope=user_scope)
        for path in target_paths
        if Path(path).suffix == ".json" and Path(path).parent == hooks_dir
    ]
    sidecar = project_root / target.root_dir / _APM_HOOKS_SIDECAR
    write_per_file_bundle_record(
        sidecar, slug, approval_key, hook_files, project_root=project_root, user_scope=user_scope
    )


def _record_path(dest: Path, project_root: Path, *, user_scope: bool) -> str:
    """Record project paths relative to the project and user paths absolutely."""
    if not user_scope and dest.is_relative_to(project_root):
        return dest.relative_to(project_root).as_posix()
    return dest.as_posix()


def _warn(message: str, slug: str, diagnostics: Any, logger: Any) -> None:
    if diagnostics is not None:
        diagnostics.warn(message=message, package=slug)
    elif logger is not None:
        logger.warning(message)


def hooks_denied_bundle(allow_executables: Any, approval_key: str | None) -> bool:
    """Whether a deny of this exact bundle, not a missing approval, withholds its hooks."""
    if approval_key is None:
        return False
    from ..security.executables import EXEC_TYPE_HOOKS
    from .local_bundle_handler import bundle_type_denied

    return bundle_type_denied(allow_executables, approval_key, EXEC_TYPE_HOOKS)
