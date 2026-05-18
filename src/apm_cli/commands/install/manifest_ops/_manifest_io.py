"""Install manifest I/O helpers for apm.yml mutation.

Extracted from manifest_ops/__init__ to keep that module under 400 lines.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from apm_cli.constants import APM_YML_FILENAME
from apm_cli.utils.console import _rich_error


@dataclass(slots=True)
class _MergeYmlContext:
    """Context bundle for _merge_packages_into_yml."""

    apm_yml_entries: dict
    current_deps: list
    data: dict
    dep_section: str
    apm_yml_path: object  # Path or str
    dev: bool = False
    logger: object | None = None


@dataclass(frozen=True, slots=True)
class _ValidationAddRequest:
    """Options for _validate_and_add_packages_to_apm_yml."""

    dry_run: bool = False
    dev: bool = False
    logger: object | None = None
    manifest_path: object | None = None  # Path | None
    auth_resolver: object | None = None
    scope: object | None = None
    allow_insecure: bool = False


def _merge_packages_into_yml(validated_packages, context: _MergeYmlContext):
    """Append *validated_packages* to the dependency list and write apm.yml.

    Mutates *current_deps* in place and persists the updated manifest to
    *apm_yml_path*.
    """
    dep_label = "devDependencies" if context.dev else "apm.yml"
    for package in validated_packages:
        context.current_deps.append(context.apm_yml_entries.get(package, package))
        if context.logger:
            context.logger.verbose_detail(f"Added {package} to {dep_label}")

    context.data[context.dep_section]["apm"] = context.current_deps

    # Write back to apm.yml
    try:
        from apm_cli.utils.yaml_io import dump_yaml

        dump_yaml(context.data, context.apm_yml_path)
        if context.logger:
            context.logger.success(
                f"Updated {APM_YML_FILENAME} with {len(validated_packages)} new package(s)"
            )
    except Exception as e:
        if context.logger:
            context.logger.error(f"Failed to write {APM_YML_FILENAME}: {e}")
        else:
            _rich_error(f"Failed to write {APM_YML_FILENAME}: {e}")
        sys.exit(1)


def _load_apm_yml_data(apm_yml_path, logger) -> dict:
    """Read and return apm.yml as a dict; exits on failure."""
    try:
        from apm_cli.utils.yaml_io import load_yaml

        return load_yaml(apm_yml_path) or {}
    except Exception as e:
        if logger:
            logger.error(f"Failed to read {APM_YML_FILENAME}: {e}")
        else:
            _rich_error(f"Failed to read {APM_YML_FILENAME}: {e}")
        sys.exit(1)


def _log_dry_run_additions(validated_packages: list, logger) -> None:
    """Emit dry-run progress lines for each package that would be added."""
    if not logger:
        return
    logger.progress(f"Dry run: Would add {len(validated_packages)} package(s) to apm.yml")
    for pkg in validated_packages:
        logger.verbose_detail(f"  + {pkg}")
