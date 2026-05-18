"""Package-type validators for APM packages.

Provides :func:`validate_apm_package` and the private ``_validate_*``
helpers that implement per-type validation logic.

Public names are re-exported via ``apm_cli.models.validation``.
"""

from __future__ import annotations

from pathlib import Path

from ...constants import APM_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME
from ._bundle_validators import (
    _check_primitive_files,
    _validate_apm_package_with_yml,
    _validate_hybrid_package,
    _validate_marketplace_plugin,
    _validate_single_skill,
    _validate_skill_bundle,
    _validate_skill_frontmatter,
    _validate_version_format,
)
from ._detection import _apm_yml_declares_dependencies, _has_hook_json, detect_package_type
from ._types import PackageContentType, PackageType, ValidationResult


def _check_dir_validity(package_path: Path, result: ValidationResult) -> bool:
    """Check that *package_path* exists and is a directory.

    Appends an error to *result* on failure.  Returns ``True`` when valid.
    """
    if not package_path.exists():
        result.add_error(f"Package directory does not exist: {package_path}")
        return False
    if not package_path.is_dir():
        result.add_error(f"Package path is not a directory: {package_path}")
        return False
    return True


def _validate_apm_yml_package(
    pkg_type: PackageType, package_path: Path, result: ValidationResult
) -> ValidationResult:
    """Dispatch to HYBRID or APM_PACKAGE validator (both use ``apm.yml``)."""
    apm_yml_path = package_path / APM_YML_FILENAME
    if pkg_type == PackageType.HYBRID:
        return _validate_hybrid_package(package_path, apm_yml_path, result)
    return _validate_apm_package_with_yml(package_path, apm_yml_path, result)


def _validate_by_type(
    pkg_type: PackageType,
    package_path: Path,
    plugin_json_path: Path | None,
    result: ValidationResult,
) -> ValidationResult:
    """Dispatch validation to the appropriate per-type helper."""
    if pkg_type == PackageType.INVALID:
        apm_yml_path = package_path / APM_YML_FILENAME
        if apm_yml_path.exists():
            apm_path = package_path / APM_DIR
            if apm_path.exists() and not apm_path.is_dir():
                result.add_error(".apm must be a directory")
            else:
                result.add_error(
                    f"Not a valid APM package: {package_path.name} has apm.yml but "
                    "is missing the required .apm/ directory. "
                    "Add .apm/ with primitives (instructions, skills, etc.), "
                    "declare dependencies in apm.yml (curated aggregator), "
                    "or add skills/<name>/SKILL.md for a skill bundle."
                )
        else:
            result.add_error(
                f"Not a valid APM package: no apm.yml, SKILL.md, hooks, or "
                f"plugin structure found in {package_path.name}. "
                "Ensure the package has SKILL.md (skill bundle), "
                "apm.yml + .apm/ (APM package), or plugin.json (Claude plugin) "
                "at its root."
            )
        return result
    if pkg_type == PackageType.HOOK_PACKAGE:
        return _validate_hook_package(package_path, result)
    if pkg_type == PackageType.CLAUDE_SKILL:
        return _validate_claude_skill(package_path, package_path / SKILL_MD_FILENAME, result)
    if pkg_type == PackageType.MARKETPLACE_PLUGIN:
        return _validate_marketplace_plugin(package_path, plugin_json_path, result)
    if pkg_type == PackageType.SKILL_BUNDLE:
        return _validate_skill_bundle(package_path, result)
    # HYBRID and APM_PACKAGE: both require apm.yml processing
    return _validate_apm_yml_package(pkg_type, package_path, result)


def validate_apm_package(package_path: Path) -> ValidationResult:
    """Validate that a directory contains a valid APM package or Claude Skill.

    Supports six package types:
    - APM_PACKAGE: Has apm.yml (with .apm/ for own primitives, or
      dep-only as a curated dependency aggregator -- #1094)
    - CLAUDE_SKILL: Has SKILL.md but no apm.yml (auto-generates apm.yml)
    - HOOK_PACKAGE: Has hooks/*.json but no apm.yml or SKILL.md
    - MARKETPLACE_PLUGIN: Has plugin.json or .claude-plugin/ (synthesizes apm.yml)
    - HYBRID: Has both apm.yml and root SKILL.md
    - SKILL_BUNDLE: Has skills/<name>/SKILL.md, apm.yml optional

    Args:
        package_path: Path to the directory to validate

    Returns:
        ValidationResult: Validation results with any errors/warnings
    """
    result = ValidationResult()
    if not _check_dir_validity(package_path, result):
        return result
    pkg_type, plugin_json_path = detect_package_type(package_path)
    result.package_type = pkg_type
    return _validate_by_type(pkg_type, package_path, plugin_json_path, result)


def _validate_hook_package(package_path: Path, result: ValidationResult) -> ValidationResult:
    """Validate a hook-only package and create APMPackage from its metadata.

    A hook package has hooks/*.json (or .apm/hooks/*.json) defining hook
    handlers per the Claude Code hooks specification, but no apm.yml or SKILL.md.

    Args:
        package_path: Path to the package directory
        result: ValidationResult to populate

    Returns:
        ValidationResult: Updated validation result
    """
    from ..apm_package import APMPackage

    package_name = package_path.name

    # Create APMPackage from directory name
    package = APMPackage(
        name=package_name,
        version="1.0.0",
        description=f"Hook package: {package_name}",
        package_path=package_path,
        type=PackageContentType.HYBRID,
    )
    result.package = package

    return result


def _validate_claude_skill(
    package_path: Path, skill_md_path: Path, result: ValidationResult
) -> ValidationResult:
    """Validate a Claude Skill and create APMPackage directly from SKILL.md metadata.

    Args:
        package_path: Path to the package directory
        skill_md_path: Path to SKILL.md
        result: ValidationResult to populate

    Returns:
        ValidationResult: Updated validation result
    """
    import frontmatter

    from ..apm_package import APMPackage

    try:
        # Parse SKILL.md to extract metadata
        with open(skill_md_path, encoding="utf-8") as f:
            post = frontmatter.load(f)

        skill_name = post.metadata.get("name", package_path.name)
        skill_description = post.metadata.get("description", f"Claude Skill: {skill_name}")
        skill_license = post.metadata.get("license")

        # Create APMPackage directly from SKILL.md metadata - no file generation needed
        package = APMPackage(
            name=skill_name,
            version="1.0.0",
            description=skill_description,
            license=skill_license,
            package_path=package_path,
            type=PackageContentType.SKILL,
        )
        result.package = package

    except Exception as e:
        result.add_error(f"Failed to process {SKILL_MD_FILENAME}: {e}")
        return result

    return result
