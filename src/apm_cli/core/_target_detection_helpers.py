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

    AGENTS.md is generated for vscode, cursor, codex, gemini, all, and minimal
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
        "cursor",
        "opencode",
        "codex",
        "gemini",
        "antigravity",
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

    Returns True only when the target that reads AGENTS.md also reads its
    respective deployed rules directory (``.github/instructions/`` for Copilot
    or ``.agents/rules/`` for Antigravity) -- meaning instructions can safely
    be omitted from AGENTS.md without losing context for any consumer.

    Today Copilot (vscode) and Antigravity support this native rules reading.
    Codex, OpenCode, Windsurf, and Gemini rely on AGENTS.md as their sole
    instruction source and must always receive instruction content (issue #1678).

    Args:
        target: The detected or configured target.  May be a string or a
            frozenset of compiler families for multi-target lists.

    Returns:
        bool: True if instructions can be omitted from AGENTS.md.
    """
    from apm_cli.core.target_detection import get_dedup_rules_dir

    return get_dedup_rules_dir(target) is not None


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


def get_dedup_rules_dir(target: CompileTargetType) -> tuple[str, str] | None:
    """Get the deployed-instruction directory and target key for dedup.

    Args:
        target: The detected or configured target. May be a string or a
            frozenset of compiler families for multi-target lists.

    Returns:
        tuple[str, str] | None: Relative path (e.g., '.agents/rules' or
        '.github/instructions') and canonical target key for expected filename
        mapping, or None when the target does not support instruction
        deduplication.
    """
    from apm_cli.core.target_catalog import normalize_target_name

    if isinstance(target, frozenset):
        if target == frozenset({"vscode"}):
            return ".github/instructions", "copilot"
        return None
    if isinstance(target, str):
        try:
            target = normalize_target_name(target)
        except KeyError:
            return None
    if target in ("vscode", "copilot"):
        return ".github/instructions", "copilot"
    if target == "antigravity":
        return ".agents/rules", "antigravity"
    return None


def manifest_targets_from_target_option(target: str | list[str] | None) -> list[str] | None:
    """Return manifest-safe targets for an install-time ``--target`` value."""
    if target is None:
        return None

    from apm_cli.core.apm_yml import CANONICAL_TARGETS
    from apm_cli.core.target_catalog import expand_all, get_target_capability

    raw_targets = [target] if isinstance(target, str) else list(target)
    seen: set[str] = set()
    manifest_targets: list[str] = []
    for raw_target in raw_targets:
        expanded = expand_all("install") if raw_target == "all" else [str(raw_target)]
        for item in expanded:
            capability = get_target_capability(item)
            canonical = capability.primitive_profile if item in capability.runtimes else item
            if canonical in CANONICAL_TARGETS and canonical not in seen:
                seen.add(canonical)
                manifest_targets.append(canonical)
    return sorted(manifest_targets) if manifest_targets else None


def normalize_policy_targets(value: str | list[str] | None) -> str | list[str] | None:
    """Normalize MCP-only selectors for compilation-target policy checks.

    The return shape matches the input shape so scalar callers remain
    backward-compatible while plural target sets are evaluated together.
    """
    if value is None:
        return None

    from apm_cli.core.target_catalog import get_target_capability
    from apm_cli.core.target_detection import MCP_ONLY_TARGETS

    values = [value] if isinstance(value, str) else list(value)
    normalized: list[str] = []
    for target in values:
        if target == "all":
            if target not in normalized:
                normalized.append(target)
            continue
        if target in MCP_ONLY_TARGETS:
            try:
                canonical = get_target_capability(target).primitive_profile
            except KeyError:
                canonical = None
            if canonical is None:
                raise RuntimeError(f"MCP-only target '{target}' has no canonical policy mapping")
            target = canonical
        if target not in normalized:
            normalized.append(target)

    return normalized[0] if isinstance(value, str) else normalized
