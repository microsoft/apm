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
