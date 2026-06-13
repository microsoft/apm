"""Target-detection helpers extracted to keep target_detection.py under 800 lines.

Re-exported from ``target_detection`` so all existing import paths keep working.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .target_detection import CompileTargetType, ResolvedTargets


def should_compile_agents_md(target: CompileTargetType) -> bool:
    """Check if AGENTS.md should be compiled.

    AGENTS.md is generated for vscode, codex, gemini, all, and minimal
    targets.  Gemini needs it because GEMINI.md imports AGENTS.md.

    Args:
        target: The detected or configured target. May be a string or a
            frozenset of compiler families for multi-target lists.

    Returns:
        bool: True if AGENTS.md should be generated
    """
    if isinstance(target, frozenset):
        return "agents" in target or "gemini" in target
    return target in (
        "vscode",
        "opencode",
        "codex",
        "gemini",
        "windsurf",
        "kiro",
        "hermes",
        "all",
        "minimal",
    )


def should_compile_claude_md(target: CompileTargetType) -> bool:
    """Check if CLAUDE.md should be compiled.

    Args:
        target: The detected or configured target. May be a string or a
            frozenset of compiler families for multi-target lists.

    Returns:
        bool: True if CLAUDE.md should be generated
    """
    if isinstance(target, frozenset):
        return "claude" in target
    return target in ("claude", "all")


def should_compile_gemini_md(target: CompileTargetType) -> bool:
    """Check if GEMINI.md should be compiled.

    Args:
        target: The detected or configured target. May be a string or a
            frozenset of compiler families for multi-target lists.

    Returns:
        bool: True if GEMINI.md should be generated
    """
    if isinstance(target, frozenset):
        return "gemini" in target
    return target in ("gemini", "all")


def should_compile_copilot_instructions_md(target: CompileTargetType) -> bool:
    """Check if .github/copilot-instructions.md should be compiled.

    Only the Copilot-native targets (copilot/vscode/agents alias) and "all"
    trigger generation.  cursor, opencode, and codex use their own native
    configuration files and must NOT receive copilot-instructions.md, even
    when combined in a multi-target list.

    Args:
        target: The detected or configured target. May be a string or a
            frozenset of compiler families for multi-target lists.

    Returns:
        bool: True if Copilot root instructions should be generated
    """
    if isinstance(target, frozenset):
        # "vscode" family is added to the frozenset by _resolve_compile_target()
        # ONLY when copilot/vscode/agents was in the original list. Checking
        # "agents" would over-fire because cursor/opencode/codex also map to
        # the "agents" family for AGENTS.md generation.
        return "vscode" in target
    return target in ("vscode", "all")


def can_dedup_agents_md_instructions(target: CompileTargetType) -> bool:
    """Check if instruction dedup is safe for AGENTS.md.

    Returns True only when every target that reads AGENTS.md also reads
    ``.github/instructions/`` -- meaning instructions can safely be omitted
    from AGENTS.md without losing context for any consumer.

    Today only Copilot (vscode) reads both locations.  Codex, OpenCode,
    Windsurf, and Gemini rely on AGENTS.md as their sole instruction source
    and must always receive instruction content (issue #1678).

    Args:
        target: The detected or configured target.  May be a string or a
            frozenset of compiler families for multi-target lists.

    Returns:
        bool: True if instructions can be omitted from AGENTS.md.
    """
    if isinstance(target, frozenset):
        # Conservative policy: only dedup when the target set is exactly
        # {"vscode"} (Copilot alone).  Any additional family -- including
        # "agents" -- means at least one consumer that does not read
        # .github/instructions/ may be present, so we keep instructions
        # in AGENTS.md to be safe.
        return target == frozenset({"vscode"})
    # Single-string targets: only "vscode" reads .github/instructions/.
    return target == "vscode"


def _target_error(message: str, source_path: Path | None) -> str:
    """Format a target validation error, naming the source file when known."""
    if source_path is not None:
        return f"Invalid 'target' in {source_path}: {message}"
    return f"Invalid target: {message}"


def format_provenance(resolved: ResolvedTargets) -> str:
    """Format provenance line for CLI output.

    Returns the message portion (without the [i] prefix, since
    _rich_info adds it).

    # Double-space between target list and metadata is intentional and
    # canonical. Test assertions match this exact spacing. Do not collapse.
    """
    targets_csv = ", ".join(resolved.targets)
    return f"Targets: {targets_csv}  (source: {resolved.source})"
