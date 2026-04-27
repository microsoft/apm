"""Skill transformer for converting SKILL.md to platform-native formats."""

import re
from pathlib import Path
from typing import Optional  # noqa: F401

from ..primitives.models import Skill


def to_hyphen_case(name: str) -> str:
    """Convert a name to hyphen-case for file naming.

    Args:
        name: Name to convert (e.g., "Brand Guidelines" or "brand_guidelines")

    Returns:
        str: Hyphen-case name (e.g., "brand-guidelines")
    """
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

    return result


class SkillTransformer:
    """Transforms SKILL.md to platform-native formats.

    For VSCode: SKILL.md -> .github/agents/{name}.agent.md
    For Claude: SKILL.md stays as-is (native format)
    """

    def transform_to_agent(
        self, skill: Skill, output_dir: Path, dry_run: bool = False
    ) -> Path | None:
        """Transform SKILL.md -> .github/agents/{name}.agent.md for VSCode.

        Note: Only creates the .agent.md file. Bundled resources stay in apm_modules/.

        Args:
            skill: Skill primitive to transform
            output_dir: Project root directory
            dry_run: If True, don't write files

        Returns:
            Path: Path to the generated agent.md file, or None if dry_run
        """
        # Generate agent content with frontmatter
        agent_content = self._generate_agent_content(skill)

        # Determine output path
        agent_name = to_hyphen_case(skill.name)
        agent_path = output_dir / ".github" / "agents" / f"{agent_name}.agent.md"

        if dry_run:
            return agent_path

        # Create directory and write file
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        agent_path.write_text(agent_content, encoding="utf-8")

        return agent_path

    def _generate_agent_content(self, skill: Skill) -> str:
        """Generate agent.md content from a skill.

        Args:
            skill: Skill primitive to convert

        Returns:
            str: Agent.md file content with frontmatter
        """
        # Build frontmatter
        lines = [
            "---",
            f"name: {skill.name}",
            f"description: {skill.description}",
        ]

        lines.append("---")
        lines.append("")

        # Add source attribution if from dependency
        if skill.source and skill.source != "local":
            lines.append(f"<!-- Source: {skill.source} -->")
            lines.append("")

        # Add body content
        lines.append(skill.content)

        return "\n".join(lines)

    def get_agent_name(self, skill: Skill) -> str:
        """Get the hyphen-case agent name for a skill.

        Args:
            skill: Skill primitive

        Returns:
            str: Hyphen-case name suitable for filename
        """
        return to_hyphen_case(skill.name)
