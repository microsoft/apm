"""Compatibility upgrade for cached legacy marketplace plugins."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.constants import APM_DIR, APM_YML_FILENAME
from apm_cli.deps.plugin_parser import has_normalized_plugin_skill_sources_receipt
from apm_cli.install.errors import DirectDependencyError
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.models.apm_package import APMPackage, PackageType
from apm_cli.models.validation import (
    gather_detection_evidence,
    validate_legacy_marketplace_plugin,
)
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    has_symlink_component,
)

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency, LockFile

_LEGACY_PLUGIN_APM_VERSION = "0.28.0"
_PLUGIN_SKILL_SOURCES_RECEIPT = Path(APM_DIR) / ".plugin-skill-sources.json"


def preserve_normalized_marketplace_plugin_type(
    package_path: Path,
    locked_dependency: LockedDependency | None,
    detected_type: PackageType,
) -> PackageType:
    """Keep parser-normalized plugin caches typed as marketplace plugins."""
    if (
        locked_dependency is not None
        and locked_dependency.package_type == PackageType.MARKETPLACE_PLUGIN.value
        and has_normalized_plugin_skill_sources_receipt(package_path)
    ):
        return PackageType.MARKETPLACE_PLUGIN
    return detected_type


def matches_fresh_legacy_plugin_hash(
    package_path: Path,
    dep_key: str,
    *,
    lockfile: LockFile | None,
    package_type: PackageType | None,
) -> bool:
    """Accept only the known receipt-only hash delta on a fresh 0.28 restore."""
    locked_dependency = lockfile.get_dependency(dep_key) if lockfile is not None else None
    if (
        lockfile is None
        or lockfile.apm_version != _LEGACY_PLUGIN_APM_VERSION
        or locked_dependency is None
        or not locked_dependency.content_hash
        or locked_dependency.package_type != PackageType.MARKETPLACE_PLUGIN.value
        or package_type != PackageType.MARKETPLACE_PLUGIN
    ):
        return False

    receipt = package_path / _PLUGIN_SKILL_SOURCES_RECEIPT
    if not receipt.is_file() or receipt.is_symlink():
        return False
    _reject_unsafe_cache_paths(package_path, dep_key, (receipt,))

    staging = ResolutionStagingSession(package_path.parent)
    replacement = staging.prepare_replacement(package_path)
    try:
        shutil.copytree(package_path, replacement, symlinks=True)
        (replacement / _PLUGIN_SKILL_SOURCES_RECEIPT).unlink()
        legacy_hash = compute_package_hash(replacement)
    except BaseException:
        staging.rollback()
        raise
    cleanup_issues = staging.rollback()
    if cleanup_issues:
        raise _staging_cleanup_error(dep_key, cleanup_issues)
    return legacy_hash == locked_dependency.content_hash


def upgrade_cached_legacy_plugin(
    package_path: Path,
    dep_key: str,
    *,
    lockfile: LockFile | None,
    fetched_this_run: bool,
) -> APMPackage | None:
    """Repair receipt-less 0.28 plugin metadata before cached integration."""
    locked_dependency = lockfile.get_dependency(dep_key) if lockfile is not None else None
    if (
        fetched_this_run
        or lockfile is None
        or lockfile.apm_version != _LEGACY_PLUGIN_APM_VERSION
        or locked_dependency is None
        or locked_dependency.package_type != PackageType.MARKETPLACE_PLUGIN.value
    ):
        return None

    if has_normalized_plugin_skill_sources_receipt(package_path):
        return None

    apm_yml_path = package_path / APM_YML_FILENAME
    apm_dir = package_path / APM_DIR
    if not apm_yml_path.is_file():
        raise _unsafe_upgrade_error(dep_key, package_path, "required apm.yml is missing")
    if not apm_dir.is_dir():
        raise _unsafe_upgrade_error(dep_key, package_path, "required .apm directory is missing")

    evidence = gather_detection_evidence(package_path)
    if not evidence.has_plugin_manifest or evidence.plugin_json_path is None:
        raise _unsafe_upgrade_error(
            dep_key,
            package_path,
            "the locked marketplace plugin manifest is missing or unreadable",
        )

    _reject_unsafe_cache_paths(
        package_path,
        dep_key,
        (apm_yml_path, apm_dir, evidence.plugin_json_path),
    )
    expected_hash = locked_dependency.content_hash
    if not expected_hash:
        raise _unsafe_upgrade_error(
            dep_key, package_path, "the legacy lock entry has no content hash"
        )
    actual_hash = compute_package_hash(package_path)
    if actual_hash != expected_hash:
        raise _unsafe_upgrade_error(
            dep_key,
            package_path,
            f"content hash mismatch (expected {expected_hash}, got {actual_hash})",
        )

    return _normalize_legacy_plugin_transactionally(
        package_path,
        dep_key,
        evidence.plugin_json_path,
    )


def _normalize_legacy_plugin_transactionally(
    package_path: Path,
    dep_key: str,
    plugin_json_path: Path,
) -> APMPackage:
    """Preflight in isolation and restore live bytes if normalization fails."""
    _preflight_legacy_plugin_normalization(package_path, dep_key, plugin_json_path)

    rollback = ResolutionStagingSession(package_path.parent)
    backup = rollback.prepare_replacement(package_path)
    try:
        shutil.copytree(package_path, backup, symlinks=True)
    except BaseException:
        rollback.rollback()
        raise
    try:
        result = validate_legacy_marketplace_plugin(
            package_path,
            plugin_json_path,
            source_path=package_path,
        )
        if not result.is_valid or result.package is None:
            raise _invalid_plugin_error(dep_key, package_path, result.errors)
    except BaseException as exc:
        failed_live = backup.with_name(f"{backup.name}-failed-live")
        if package_path.exists():
            package_path.replace(failed_live)
        rollback.publish_replacement(backup)
        cleanup_issues = rollback.commit()
        if cleanup_issues:
            raise _staging_cleanup_error(dep_key, cleanup_issues) from exc
        raise
    rollback.discard_replacement(backup)
    cleanup_issues = rollback.commit()
    if cleanup_issues:
        raise _staging_cleanup_error(dep_key, cleanup_issues)
    return result.package


def _preflight_legacy_plugin_normalization(
    package_path: Path,
    dep_key: str,
    plugin_json_path: Path,
) -> None:
    """Run every normalization write against a disposable package copy."""
    staging = ResolutionStagingSession(package_path.parent)
    replacement = staging.prepare_replacement(package_path)
    try:
        shutil.copytree(package_path, replacement, symlinks=True)
        staged_plugin_json = replacement / plugin_json_path.relative_to(package_path)
        result = validate_legacy_marketplace_plugin(
            replacement,
            staged_plugin_json,
            source_path=replacement,
        )
        if not result.is_valid or result.package is None:
            raise _invalid_plugin_error(dep_key, package_path, result.errors)
    except BaseException:
        staging.rollback()
        raise
    staging.discard_replacement(replacement)
    cleanup_issues = staging.commit()
    if cleanup_issues:
        raise _staging_cleanup_error(dep_key, cleanup_issues)


def _invalid_plugin_error(
    dep_key: str,
    package_path: Path,
    errors: list[str],
) -> DirectDependencyError:
    details = "; ".join(errors) or "validator returned no package"
    return DirectDependencyError(
        f"Cached Claude Plugin '{dep_key}' at '{package_path}' is invalid: {details}. "
        "Remove the cached directory or run 'apm deps clean --yes', then retry."
    )


def _staging_cleanup_error(
    dep_key: str,
    cleanup_issues: list[tuple[Path, str]],
) -> DirectDependencyError:
    details = "; ".join(f"{path}: {message}" for path, message in cleanup_issues)
    return DirectDependencyError(
        f"Cached Claude Plugin '{dep_key}' staging cleanup failed: {details}"
    )


def _reject_unsafe_cache_paths(
    package_path: Path,
    dep_key: str,
    paths: tuple[Path, ...],
) -> None:
    """Reject metadata paths that could redirect compatibility writes."""
    try:
        unsafe = package_path.is_symlink()
        for path in paths:
            if has_symlink_component(package_path, path):
                unsafe = True
                continue
            ensure_path_within(path, package_path)
    except (OSError, PathTraversalError) as exc:
        raise _unsafe_upgrade_error(
            dep_key, package_path, f"cache metadata path validation failed: {exc}"
        ) from exc
    if unsafe:
        raise _unsafe_upgrade_error(dep_key, package_path, "cache metadata contains a symlink")


def _unsafe_upgrade_error(
    dep_key: str,
    package_path: Path,
    reason: str,
) -> DirectDependencyError:
    return DirectDependencyError(
        f"Cached Claude Plugin '{dep_key}' at '{package_path}' cannot be upgraded safely: "
        f"{reason}. Remove the cached directory or run 'apm deps clean --yes', then retry."
    )
