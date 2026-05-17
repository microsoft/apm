"""Skill integration functionality for APM packages (Claude Code & Cursor support)."""

import filecmp
import hashlib  # noqa: F401
import re
import shutil
import sys
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
from .naming import normalize_skill_name, to_hyphen_case, validate_skill_name


def get_effective_type(package_info) -> "PackageContentType":
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

    effective_type = sys.modules[__package__].get_effective_type(package_info)

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

    effective_type = sys.modules[__package__].get_effective_type(package_info)

    # INSTRUCTIONS and HYBRID should compile to AGENTS.md
    # SKILL and PROMPTS should NOT compile to AGENTS.md
    return effective_type in (PackageContentType.INSTRUCTIONS, PackageContentType.HYBRID)
