"""Manifest write and dry-run bootstrap helpers for install."""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from apm_cli.constants import APM_YML_FILENAME
from apm_cli.core.command_logger import InstallLogger


def _merge_packages_into_yml(
    validated_packages,
    apm_yml_entries,
    current_deps,
    data,
    dep_section,
    apm_yml_path,
    *,
    dev=False,
    logger=None,
):
    """Append *validated_packages* to the dependency list and write apm.yml."""
    dep_label = "devDependencies" if dev else "apm.yml"
    for package in validated_packages:
        current_deps.append(apm_yml_entries.get(package, package))
        if logger:
            logger.verbose_detail(f"Added {package} to {dep_label}")

    data[dep_section]["apm"] = current_deps

    try:
        from apm_cli.utils.yaml_io import dump_yaml_roundtrip

        dump_yaml_roundtrip(data, apm_yml_path)
        if logger:
            logger.success(
                f"Updated {APM_YML_FILENAME} with {len(validated_packages)} new package(s)"
            )
    except Exception as e:
        (logger or InstallLogger()).error(f"Failed to write {APM_YML_FILENAME}: {e}")
        sys.exit(1)


def _prepare_dry_run_manifest_path(
    manifest_path: Path,
    *,
    dry_run: bool,
    user_scope: bool,
    has_packages: bool,
) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    """Redirect an absent user manifest to temporary storage during previews."""
    if not (dry_run and user_scope and has_packages and not manifest_path.exists()):
        return manifest_path, None

    temp_dir = tempfile.TemporaryDirectory(prefix="apm-dry-run-")
    return Path(temp_dir.name) / manifest_path.name, temp_dir


def _prepare_user_scope_for_install(
    *,
    dry_run: bool,
    logger: InstallLogger,
    ensure_user_dirs: Callable[[], None],
    warn_unsupported_user_scope: Callable[[], str | None],
) -> None:
    """Prepare user-scope install paths unless the invocation is read-only."""
    if not dry_run:
        ensure_user_dirs()
        logger.progress("Installing to user scope (~/.apm/)")
    else:
        logger.progress("Previewing user-scope install (~/.apm/)")
    if scope_warning := warn_unsupported_user_scope():
        logger.warning(scope_warning)


def _report_bootstrap_manifest(
    *,
    logger: InstallLogger,
    manifest_display: str,
    manifest_targets: list[str],
    dry_run: bool,
    user_scope: bool,
) -> None:
    """Render bootstrap messages with dry-run-safe wording."""
    if dry_run and user_scope:
        logger.progress(f"Dry run: Would create {manifest_display}")
    else:
        logger.success(f"Created {manifest_display}")
    if not manifest_targets:
        return
    target_list = ", ".join(manifest_targets)
    if dry_run and user_scope:
        logger.progress(f"Dry run: Would set targets: {target_list} (in {manifest_display})")
        return
    logger.progress(f"Targets set: {target_list} (persisted to {manifest_display})")
