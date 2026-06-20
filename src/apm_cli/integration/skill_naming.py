"""Skill naming helpers for APM skill integration."""

import logging
import re
from typing import Any

_log = logging.getLogger("apm_cli.integration.skill_integrator")


def to_hyphen_case(name: str) -> str:
    """Convert a package name to hyphen-case for Claude Skills spec."""
    if "/" in name:
        name = name.split("/")[-1]

    result = name.replace("_", "-").replace(" ", "-")
    result = re.sub(r"([a-z])([A-Z])", r"\1-\2", result)
    result = re.sub(r"[^a-z0-9-]", "", result.lower())
    result = re.sub(r"-+", "-", result)
    result = result.strip("-")
    return result[:64]


def _skill_name_char_error(name: str) -> str:
    """Return the precise skill-name character validation error for *name*."""
    if any(c.isupper() for c in name):
        return "Skill name must be lowercase (no uppercase letters)"
    if "_" in name:
        return "Skill name cannot contain underscores (use hyphens instead)"
    if " " in name:
        return "Skill name cannot contain spaces (use hyphens instead)"
    invalid_chars = set(re.findall(r"[^a-z0-9-]", name))
    if invalid_chars:
        return f"Skill name contains invalid characters: {', '.join(sorted(invalid_chars))}"
    return "Skill name must be lowercase alphanumeric with hyphens only"


def normalize_skill_name(name: str) -> str:
    """Convert any package name to a valid skill name per agentskills.io spec."""
    return to_hyphen_case(name)


def should_compile_instructions(package_info: Any) -> bool:
    """Determine if package should compile to AGENTS.md/CLAUDE.md."""
    from apm_cli.models.apm_package import PackageContentType

    from .skill_integrator import get_effective_type

    effective_type = get_effective_type(package_info)
    return effective_type in (PackageContentType.INSTRUCTIONS, PackageContentType.HYBRID)
