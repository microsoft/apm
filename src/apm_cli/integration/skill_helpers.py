"""Shared skill integration helpers."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.models.apm_package import PackageContentType


# DEPRECATED -- use IntegrationResult directly for new code.
# Kept for backward compatibility. The fields map as follows:
# skill_created -> IntegrationResult.skill_created
# sub_skills_promoted -> IntegrationResult.sub_skills_promoted
# skill_path, references_copied -> not mapped (skill-internal)
@dataclass
class SkillIntegrationResult:
    """Result of skill integration operation."""

    skill_created: bool
    skill_updated: bool
    skill_skipped: bool
    skill_path: Path | None
    references_copied: int  # Now tracks total files copied to subdirectories
    links_resolved: int = 0  # Kept for backwards compatibility
    sub_skills_promoted: int = 0  # Number of sub-skills promoted to top-level
    target_paths: list[Path] = None  # All deployed directories (for deployed_files manifest)

    def __post_init__(self):
        if self.target_paths is None:
            self.target_paths = []


def to_hyphen_case(name: str) -> str:
    """Convert a package name to hyphen-case for Claude Skills spec.

    Args:
        name: Package name (e.g., "owner/repo" or "MyPackage")

    Returns:
        str: Hyphen-case name, max 64 chars (e.g., "owner-repo" or "my-package")
    """
    # Extract just the repo name if it's owner/repo format
    if "/" in name:
        name = name.split("/")[-1]

    # Replace underscores and spaces with hyphens
    result = name.replace("_", "-").replace(" ", "-")

    # Insert hyphens before uppercase letters (camelCase to hyphen-case)
    result = re.sub(r"([a-z])([A-Z])", r"\1-\2", result)

    # Convert to lowercase and remove any invalid characters
    result = re.sub(r"[^a-z0-9-]", "", result.lower())

    # Remove consecutive hyphens
    result = re.sub(r"-+", "-", result)

    # Remove leading/trailing hyphens
    result = result.strip("-")

    # Truncate to 64 chars (Claude Skills spec limit)
    return result[:64]


def validate_skill_name(name: str) -> tuple[bool, str]:
    """Validate skill name per agentskills.io spec.

    Skill names must:
    - Be 1-64 characters long
    - Contain only lowercase alphanumeric characters and hyphens (a-z, 0-9, -)
    - Not contain consecutive hyphens (--)
    - Not start or end with a hyphen

    Args:
        name: Skill name to validate

    Returns:
        tuple[bool, str]: (is_valid, error_message)
            - is_valid: True if name is valid, False otherwise
            - error_message: Empty string if valid, descriptive error otherwise
    """
    # Check length
    if len(name) < 1:
        return (False, "Skill name cannot be empty")

    if len(name) > 64:
        return (False, f"Skill name must be 1-64 characters (got {len(name)})")

    # Check for consecutive hyphens
    if "--" in name:
        return (False, "Skill name cannot contain consecutive hyphens (--)")

    # Check for leading/trailing hyphens
    if name.startswith("-"):
        return (False, "Skill name cannot start with a hyphen")

    if name.endswith("-"):
        return (False, "Skill name cannot end with a hyphen")

    # Check for valid characters (lowercase alphanumeric + hyphens only)
    # Pattern: must start and end with alphanumeric, with alphanumeric or hyphens in between
    pattern = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"
    if not re.match(pattern, name):
        # Determine specific error
        if any(c.isupper() for c in name):
            return (False, "Skill name must be lowercase (no uppercase letters)")

        if "_" in name:
            return (False, "Skill name cannot contain underscores (use hyphens instead)")

        if " " in name:
            return (False, "Skill name cannot contain spaces (use hyphens instead)")

        # Check for other invalid characters
        invalid_chars = set(re.findall(r"[^a-z0-9-]", name))
        if invalid_chars:
            return (
                False,
                f"Skill name contains invalid characters: {', '.join(sorted(invalid_chars))}",
            )

        return (False, "Skill name must be lowercase alphanumeric with hyphens only")

    return (True, "")


def normalize_skill_name(name: str) -> str:
    """Convert any package name to a valid skill name per agentskills.io spec.

    Normalization steps:
    1. Extract repo name if owner/repo format
    2. Convert to lowercase
    3. Replace underscores and spaces with hyphens
    4. Convert camelCase to hyphen-case
    5. Remove invalid characters
    6. Remove consecutive hyphens
    7. Strip leading/trailing hyphens
    8. Truncate to 64 characters

    Args:
        name: Package name to normalize (e.g., "owner/MyRepo_Name")

    Returns:
        str: Valid skill name (e.g., "my-repo-name")
    """
    # Use to_hyphen_case which already handles most normalization
    return to_hyphen_case(name)


# =============================================================================
# Package Type Routing Functions (T4)
# =============================================================================
# These functions determine behavior based on:
# 1. Explicit `type` field in apm.yml (highest priority)
# 2. Presence of SKILL.md at package root (makes it a skill)
# 3. Default to INSTRUCTIONS for instruction-only packages
#
# Per skill-strategy.md Decision 2: "Skills are explicit, not implicit"
# - Packages with SKILL.md OR explicit type: skill/hybrid -> become skills
# - Packages with only instructions -> compile to AGENTS.md, NOT skills


def get_effective_type(package_info) -> PackageContentType:
    """Get effective package content type based on package structure.

    Determines type by:
    1. Package has SKILL.md (PackageType.CLAUDE_SKILL or HYBRID) -> SKILL
    2. Package is a SKILL_BUNDLE or MARKETPLACE_PLUGIN (has skills/) -> SKILL
    3. Otherwise -> INSTRUCTIONS (compile to AGENTS.md only)

    Args:
        package_info: PackageInfo object containing package metadata

    Returns:
        PackageContentType: The effective type
    """
    from apm_cli.models.apm_package import PackageContentType, PackageType

    # Check if package has SKILL.md (via package_type field)
    # PackageType.CLAUDE_SKILL = has root SKILL.md only
    # PackageType.HYBRID = has both apm.yml AND root SKILL.md
    # PackageType.SKILL_BUNDLE = has skills/<name>/SKILL.md (nested bundle)
    # PackageType.MARKETPLACE_PLUGIN = has plugin manifest (plugin.json or
    #   .claude-plugin/); may or may not include skills/. The integrator
    #   path gates on actual skills/ presence, so plugins without skills
    #   are inert in the SKILL branch.
    if package_info.package_type in (
        PackageType.CLAUDE_SKILL,
        PackageType.HYBRID,
        PackageType.SKILL_BUNDLE,
        PackageType.MARKETPLACE_PLUGIN,
    ):
        return PackageContentType.SKILL

    # Default to INSTRUCTIONS for packages without SKILL.md
    return PackageContentType.INSTRUCTIONS


def should_install_skill(package_info) -> bool:
    """Determine if package should be installed as a native skill.

    This controls whether a package gets installed to .github/skills/ (or .claude/skills/).

    Per skill-strategy.md Decision 2 - "Skills are explicit, not implicit":

    Returns True for:
        - SKILL: Package has SKILL.md or declares type: skill
        - HYBRID: Package declares type: hybrid in apm.yml

    Returns False for:
        - INSTRUCTIONS: Compile to AGENTS.md only, no skill created
        - PROMPTS: Commands/prompts only, no skill created
        - Packages without SKILL.md and no explicit type field

    Args:
        package_info: PackageInfo object containing package metadata

    Returns:
        bool: True if package should be installed as a native skill
    """
    from apm_cli.models.apm_package import PackageContentType

    effective_type = get_effective_type(package_info)

    # SKILL and HYBRID should install as skills
    # INSTRUCTIONS and PROMPTS should NOT install as skills
    return effective_type in (PackageContentType.SKILL, PackageContentType.HYBRID)


def should_compile_instructions(package_info) -> bool:
    """Determine if package should compile to AGENTS.md/CLAUDE.md.

    This controls whether a package's instructions are included in compiled output.

    Per skill-strategy.md Decision 2:

    Returns True for:
        - INSTRUCTIONS: Compile to AGENTS.md only (default for packages without SKILL.md)
        - HYBRID: Package declares type: hybrid in apm.yml

    Returns False for:
        - SKILL: Install as native skill only, no AGENTS.md compilation
        - PROMPTS: Commands/prompts only, no instructions compiled

    Args:
        package_info: PackageInfo object containing package metadata

    Returns:
        bool: True if package's instructions should be compiled to AGENTS.md/CLAUDE.md
    """
    from apm_cli.models.apm_package import PackageContentType

    effective_type = get_effective_type(package_info)

    # INSTRUCTIONS and HYBRID should compile to AGENTS.md
    # SKILL and PROMPTS should NOT compile to AGENTS.md
    return effective_type in (PackageContentType.INSTRUCTIONS, PackageContentType.HYBRID)


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
