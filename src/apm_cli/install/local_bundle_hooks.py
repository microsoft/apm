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
    hook_files: int = 0
    skipped: int = 0


def integrate_local_bundle_hooks(
    bundle_info: Any,
    pack_files: dict[str, str],
    project_root: Path,
    *,
    targets: Any,
    slug: str,
    hooks_approved: bool,
    approval_key: str | None,
    force: bool,
    dry_run: bool,
    scope: Any,
    diagnostics: Any = None,
    logger: Any = None,
) -> BundleHookResult:
    """Register the bundle's plugin hooks for every hook-capable target."""
    result = BundleHookResult()
    hook_paths = sorted(rel for rel in pack_files if is_plugin_hook_path(rel))
    if not hook_paths:
        return result
    if not hooks_approved:
        result.skipped = len(hook_paths)
        _warn(
            f"Skipped {len(hook_paths)} bundle hook file(s) from {slug}: hooks are "
            "executable and not approved for this exact bundle content. To approve "
            "this exact local bundle, add:\n"
            "executables:\n"
            "  allow:\n"
            f'    "{approval_key}":\n'
            "      hooks: true\n"
            "Then rerun the install.",
            slug,
            diagnostics,
            logger,
        )
        return result
    hook_targets = [target for target in targets if target.supports("hooks")]
    if not hook_targets:
        return result
    from .local_bundle_paths import bundle_slug_validation_error

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
    managed_files = _prior_bundle_claims(project_root)
    with tempfile.TemporaryDirectory(prefix="apm-bundle-hooks-") as tmp:
        package_info = _bundle_hook_package(bundle_info, pack_files, Path(tmp) / slug, slug)
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
            for target_path in outcome.target_paths:
                dest = Path(target_path)
                record = _record_path(dest, project_root, user_scope=user_scope)
                if record in result.script_hashes:
                    continue
                result.scripts.append(record)
                result.script_hashes[record] = compute_file_hash(dest)
    if result.hook_files and logger is not None:
        logger.verbose_detail(f"Registered {result.hook_files} hook file(s) from bundle '{slug}'")
    return result


def _bundle_hook_package(bundle_info: Any, pack_files: dict[str, str], stage: Path, slug: str):
    """Stage verified bundle bytes and load them as the Claude plugin they are."""
    from ..models.apm_package import PackageInfo
    from ..models.validation import PackageType, validate_legacy_marketplace_plugin
    from ..utils.helpers import find_plugin_json

    bundle_dir = Path(bundle_info.source_dir)
    stage.mkdir(parents=True)
    manifests = ("plugin.json", ".claude-plugin/plugin.json")
    for rel in (*pack_files, *manifests):
        validate_path_segments(rel, context="bundle hook staging path")
        src = bundle_dir / rel
        if not src.is_file() or src.is_symlink():
            continue
        dest = stage / rel
        ensure_path_within(dest, stage)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest, follow_symlinks=False)
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


def _prior_bundle_claims(project_root: Path) -> set[str]:
    """Return paths a previous local install recorded, so reinstall replaces them."""
    from ..deps.lockfile import LockFile, get_lockfile_path

    lockfile = LockFile.read(get_lockfile_path(project_root))
    return set(lockfile.local_deployed_files) if lockfile is not None else set()


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
