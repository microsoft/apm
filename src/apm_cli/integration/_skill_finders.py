"""File-finder helpers for skill integration.

Extracted from ``skill_deploy.py`` to keep that module under the file-length
gate.  ``skill_deploy`` re-exports these at module level so existing call sites
are unaffected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def find_instruction_files(package_path: Path) -> list[Path]:
    """Find all instruction files in a package."""
    instruction_files: list[Path] = []
    apm_instructions = package_path / ".apm" / "instructions"
    if apm_instructions.exists():
        instruction_files.extend(apm_instructions.glob("*.instructions.md"))
    return instruction_files


def find_agent_files(package_path: Path) -> list[Path]:
    """Find all agent files in a package."""
    agent_files: list[Path] = []
    apm_agents = package_path / ".apm" / "agents"
    if apm_agents.exists():
        agent_files.extend(apm_agents.glob("*.agent.md"))
    return agent_files


def find_prompt_files(package_path: Path) -> list[Path]:
    """Find all prompt files in a package."""
    prompt_files: list[Path] = []
    if package_path.exists():
        prompt_files.extend(package_path.glob("*.prompt.md"))
    apm_prompts = package_path / ".apm" / "prompts"
    if apm_prompts.exists():
        prompt_files.extend(apm_prompts.glob("*.prompt.md"))
    return prompt_files


def find_context_files(package_path: Path) -> list[Path]:
    """Find all context and memory files in a package."""
    context_files: list[Path] = []
    apm_context = package_path / ".apm" / "context"
    if apm_context.exists():
        context_files.extend(apm_context.glob("*.context.md"))
    apm_memory = package_path / ".apm" / "memory"
    if apm_memory.exists():
        context_files.extend(apm_memory.glob("*.memory.md"))
    return context_files


def _emit_unmanaged_skill_skip(
    sub_name: str,
    rel_path: str,
    parent_name: str,
    diagnostics: Any,
    logger: Any,
) -> None:
    """Emit the existing unmanaged-skill skip warning."""
    message = (
        f"Skipping skill '{sub_name}' -- local skill exists (not managed by APM). "
        "Use 'apm install --force' to overwrite."
    )
    if diagnostics is not None:
        diagnostics.skip(rel_path, package=parent_name)
    elif logger:
        logger.warning(message)
    else:
        try:
            from apm_cli.utils.console import _rich_warning

            _rich_warning(message)
        except ImportError:
            pass


def _emit_sub_skill_overwrite(
    sub_name: str,
    rel_path: str,
    parent_name: str,
    diagnostics: Any,
    logger: Any,
) -> None:
    """Emit the existing sub-skill overwrite warning."""
    if diagnostics is not None:
        diagnostics.overwrite(
            path=rel_path,
            package=parent_name,
            detail=f"Skill '{sub_name}' replaced -- previously from another package",
        )
    elif logger:
        logger.warning(
            f"Sub-skill '{sub_name}' from '{parent_name}' overwrites existing skill at {rel_path}"
        )
    else:
        try:
            from apm_cli.utils.console import _rich_warning

            _rich_warning(
                f"Sub-skill '{sub_name}' from '{parent_name}' overwrites existing skill at {rel_path}"
            )
        except ImportError:
            pass
