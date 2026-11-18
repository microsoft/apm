"""Manifest write and dry-run bootstrap helpers for install."""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from apm_cli.constants import APM_YML_FILENAME
from apm_cli.core.command_logger import InstallLogger


def _load_manifest_for_add(apm_yml_path: Path, logger=None) -> tuple[dict, str | None]:
    """Read apm.yml round-trip and return it with its validated ``default_host``.

    OpenAPM req-mf-019: the manifest's ``default_host:`` selects the host that
    shorthand entries resolve against and that canonical writes strip (#3843).
    Exits 1 with an error when the manifest is unreadable or the host invalid.
    """
    from apm_cli.models.manifest_io import load_manifest_roundtrip
    from apm_cli.utils.github_host import validate_manifest_default_host

    try:
        data = load_manifest_roundtrip(apm_yml_path) or {}
    except Exception as e:
        (logger or InstallLogger()).error(f"Failed to read {APM_YML_FILENAME}: {e}")
        sys.exit(1)
    try:
        return data, validate_manifest_default_host(data.get("default_host"))
    except ValueError as e:
        (logger or InstallLogger()).error(str(e))
        sys.exit(1)


def _build_prospective_package(
    data: dict,
    dep_section: str,
    dependencies: list,
    *,
    manifest_path: Path,
    source_root: Path,
    create_config: bool,
):
    """Build the dry-run package as if *dependencies* were written to apm.yml.

    Local paths resolve against *source_root*, the caller's real project (or
    user) directory, even when the preview manifest lives in a temp file.
    """
    from apm_cli.models.apm_package import APMPackage

    data[dep_section]["apm"] = dependencies
    return APMPackage.from_mapping(
        data,
        package_path=source_root,
        source_path=source_root,
        manifest_path=manifest_path,
        create_config=create_config,
        warning_handler=None,  # The disk parse emits the manifest diagnostic.
    )


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
    has_packages: bool,
) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    """Redirect an absent manifest to temporary storage during previews."""
    if not (dry_run and has_packages and not manifest_path.exists()):
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
    from apm_cli.core.scope import get_apm_home

    scope_label = f"{get_apm_home()}/"
    if not dry_run:
        ensure_user_dirs()
        logger.progress(f"Installing to user scope ({scope_label})")
    else:
        logger.progress(f"Previewing user-scope install ({scope_label})")
    if scope_warning := warn_unsupported_user_scope():
        logger.warning(scope_warning)


def _report_bootstrap_manifest(
    *,
    logger: InstallLogger,
    manifest_display: str,
    manifest_targets: list[str],
    dry_run: bool,
) -> None:
    """Render bootstrap messages with dry-run-safe wording."""
    if dry_run:
        logger.progress(f"Dry run: Would create {manifest_display}")
    else:
        logger.success(f"Created {manifest_display}")
    if not manifest_targets:
        return
    target_list = ", ".join(manifest_targets)
    if dry_run:
        logger.progress(f"Dry run: Would set targets: {target_list} (in {manifest_display})")
        return
    logger.progress(f"Targets set: {target_list} (persisted to {manifest_display})")
