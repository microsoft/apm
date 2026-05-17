"""Skill integration functionality for APM packages (Claude Code & Cursor support)."""

import filecmp
import hashlib  # noqa: F401
import re
import shutil
from dataclasses import dataclass
from datetime import datetime  # noqa: F401
from pathlib import Path

import frontmatter  # noqa: F401

from apm_cli.integration.base_integrator import BaseIntegrator

# DEPRECATED -- use IntegrationResult directly for new code.
# Kept for backward compatibility. The fields map as follows:
# skill_created -> IntegrationResult.skill_created
# sub_skills_promoted -> IntegrationResult.sub_skills_promoted
# skill_path, references_copied -> not mapped (skill-internal)
from .naming import normalize_skill_name, validate_skill_name
from .typing_helpers import should_install_skill


def copy_skill_to_target(
    package_info,
    source_path: Path,
    target_base: Path,
    targets=None,
) -> list[Path]:
    """Copy skill directory to all active target skills/ directories.

    This is a standalone function for direct skill copy operations.
    It handles:
    - Package type routing via should_install_skill()
    - Skill name validation/normalization
    - Directory structure preservation
    - Deployment to every active target that supports skills

    When *targets* is provided, only those targets are used.
    Otherwise falls back to ``active_targets()``.

    Source SKILL.md is copied verbatim -- no metadata injection.

    Copies:
    - SKILL.md (required)
    - scripts/ (optional)
    - references/ (optional)
    - assets/ (optional)
    - Any other subdirectories the package contains

    Args:
        package_info: PackageInfo object with package metadata
        source_path: Path to skill in apm_modules/
        target_base: Usually project root
        targets: Optional explicit list of TargetProfile objects.

    Returns:
        List of all deployed skill directory paths (empty if skipped).
    """
    # Check if package type allows skill installation (T4 routing)
    if not should_install_skill(package_info):
        return []

    # Check for SKILL.md existence
    source_skill_md = source_path / "SKILL.md"
    if not source_skill_md.exists():
        # No SKILL.md means this package is handled by compilation, not skill copy
        return []

    # Get and validate skill name from folder
    raw_skill_name = source_path.name

    is_valid, _ = validate_skill_name(raw_skill_name)
    if is_valid:  # noqa: SIM108
        skill_name = raw_skill_name
    else:
        skill_name = normalize_skill_name(raw_skill_name)

    deployed: list[Path] = []
    seen_skill_dirs: set[Path] = set()

    # Deploy to all active targets that support skills.
    # When no targets are provided, fall back to project-scope detection.
    # Callers responsible for user-scope should pass resolved targets
    # from resolve_targets().
    if targets is None:
        from apm_cli.integration.targets import active_targets

        targets = active_targets(target_base)
    for target in targets:
        if not target.supports("skills"):
            continue
        skills_mapping = target.primitives["skills"]
        effective_root = skills_mapping.deploy_root or target.root_dir

        # Skip if target dir does not exist and auto_create is disabled
        target_root_dir = target_base / target.root_dir
        if not target.auto_create and not target_root_dir.is_dir():
            continue

        skill_dir = target_base / effective_root / "skills" / skill_name

        # Security: reject traversal in skill name and validate containment.
        # The containment check resolves the *base* (which may sit behind a
        # symlink) but verifies the *unresolved* caller-controlled segment
        # (skill_name) has no traversal parts.  This prevents a symlink at
        # target_base / effective_root from silently redirecting writes
        # outside the project root.
        from apm_cli.utils.path_security import (
            PathTraversalError,
            ensure_path_within,
            validate_path_segments,
        )

        validate_path_segments(skill_name, context="skill name")
        if skill_dir.is_symlink():
            raise PathTraversalError(
                f"Skill destination {skill_dir} is a symlink -- refusing to deploy"
            )

        # Verify the resolved skill directory is within the project root.
        # This catches the case where an ancestor directory (e.g.
        # effective_root) is a symlink pointing outside the project.
        resolved_project = target_base.resolve()
        resolved_skill_dir = skill_dir.resolve()
        if not resolved_skill_dir.is_relative_to(resolved_project):
            raise PathTraversalError(
                f"Skill directory '{skill_dir}' resolves to '{resolved_skill_dir}' "
                f"which is outside the project root '{resolved_project}'"
            )
        ensure_path_within(skill_dir, target_base / effective_root / "skills")

        # Dedup: skip if same resolved path already deployed.
        resolved = skill_dir.resolve()
        if resolved in seen_skill_dirs:
            import logging

            logging.getLogger(__name__).debug(
                "%s -- already deployed, skipping for %s", skill_dir, target.name
            )
            continue
        seen_skill_dirs.add(resolved)

        skill_dir.parent.mkdir(parents=True, exist_ok=True)
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        from apm_cli.security.gate import ignore_non_content

        shutil.copytree(source_path, skill_dir, ignore=ignore_non_content)
        deployed.append(skill_dir)

    return deployed
