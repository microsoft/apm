"""Target detection for auto-selecting compilation and integration targets.

This module implements the auto-detection pattern for determining which agent
targets (Copilot, Claude, Cursor, OpenCode, Codex) should be used based on
existing project structure and configuration.

Detection priority (highest to lowest):
1. Explicit --target flag (always wins)
2. apm.yml target setting (top-level field)
3. Auto-detect from existing folders:
   - .github/ only -> copilot (internal: "vscode")
   - .claude/ only -> claude
   - .cursor/ only -> cursor
   - .opencode/ only -> opencode
   - .codex/ only -> codex
   - Multiple target folders -> all
   - None exist -> minimal (AGENTS.md only, no folder integration)

"copilot" is the recommended user-facing target name. "vscode" and "agents"
are accepted as aliases and map to the same internal value.
"""

from pathlib import Path
from typing import List, Literal, Optional, Tuple, Union

import click

# Valid target values (internal canonical form)
TargetType = Literal["vscode", "claude", "cursor", "opencode", "codex", "all", "minimal"]

# User-facing target values (includes aliases accepted by CLI)
UserTargetType = Literal["copilot", "vscode", "agents", "claude", "cursor", "opencode", "codex", "all", "minimal"]


def detect_target(
    project_root: Path,
    explicit_target: Optional[str] = None,
    config_target: Optional[str] = None,
) -> Tuple[TargetType, str]:
    """Detect the appropriate target for compilation and integration.
    
    Args:
        project_root: Root directory of the project
        explicit_target: Explicitly provided --target flag value
        config_target: Target from apm.yml top-level 'target' field
        
    Returns:
        Tuple of (target, reason) where:
        - target: The detected target type
        - reason: Human-readable explanation for the choice
    """
    # Priority 1: Explicit --target flag
    if explicit_target:
        if explicit_target in ("copilot", "vscode", "agents"):
            return "vscode", "explicit --target flag"
        elif explicit_target == "claude":
            return "claude", "explicit --target flag"
        elif explicit_target == "cursor":
            return "cursor", "explicit --target flag"
        elif explicit_target == "opencode":
            return "opencode", "explicit --target flag"
        elif explicit_target == "codex":
            return "codex", "explicit --target flag"
        elif explicit_target == "all":
            return "all", "explicit --target flag"
    
    # Priority 2: apm.yml target setting
    if config_target:
        if config_target in ("copilot", "vscode", "agents"):
            return "vscode", "apm.yml target"
        elif config_target == "claude":
            return "claude", "apm.yml target"
        elif config_target == "cursor":
            return "cursor", "apm.yml target"
        elif config_target == "opencode":
            return "opencode", "apm.yml target"
        elif config_target == "codex":
            return "codex", "apm.yml target"
        elif config_target == "all":
            return "all", "apm.yml target"
    
    # Priority 3: Auto-detect from existing folders
    github_exists = (project_root / ".github").exists()
    claude_exists = (project_root / ".claude").exists()
    cursor_exists = (project_root / ".cursor").is_dir()
    opencode_exists = (project_root / ".opencode").is_dir()
    codex_exists = (project_root / ".codex").is_dir()
    detected = []
    if github_exists:
        detected.append(".github/")
    if claude_exists:
        detected.append(".claude/")
    if cursor_exists:
        detected.append(".cursor/")
    if opencode_exists:
        detected.append(".opencode/")
    if codex_exists:
        detected.append(".codex/")

    if len(detected) >= 2:
        return "all", f"detected {' and '.join(detected)} folders"
    elif github_exists:
        return "vscode", "detected .github/ folder"
    elif claude_exists:
        return "claude", "detected .claude/ folder"
    elif cursor_exists:
        return "cursor", "detected .cursor/ folder"
    elif opencode_exists:
        return "opencode", "detected .opencode/ folder"
    elif codex_exists:
        return "codex", "detected .codex/ folder"
    else:
        # No known target folders exist - minimal output
        return "minimal", "no .github/, .claude/, .cursor/, .opencode/, or .codex/ folder found"


def should_integrate_vscode(target: TargetType) -> bool:
    """Check if VSCode integration should be performed.
    
    Args:
        target: The detected or configured target
        
    Returns:
        bool: True if VSCode integration (prompts, agents) should run
    """
    return target in ("vscode", "all")


def should_integrate_claude(target: TargetType) -> bool:
    """Check if Claude integration should be performed.
    
    Args:
        target: The detected or configured target
        
    Returns:
        bool: True if Claude integration (commands, skills) should run
    """
    return target in ("claude", "all")


def should_integrate_opencode(target: TargetType) -> bool:
    """Check if OpenCode integration should be performed.

    Args:
        target: The detected or configured target

    Returns:
        bool: True if OpenCode integration (agents, commands, skills) should run
    """
    return target in ("opencode", "all")


def should_integrate_cursor(target: TargetType) -> bool:
    """Check if Cursor integration should be performed.

    Args:
        target: The detected or configured target

    Returns:
        bool: True if Cursor integration (agents, skills, rules) should run
    """
    return target in ("cursor", "all")


def should_integrate_codex(target: TargetType) -> bool:
    """Check if Codex CLI integration should be performed.

    Args:
        target: The detected or configured target

    Returns:
        bool: True if Codex integration (agents, skills, hooks) should run
    """
    return target in ("codex", "all")


def should_compile_agents_md(target: TargetType) -> bool:
    """Check if AGENTS.md should be compiled.
    
    AGENTS.md is generated for vscode, codex, all, and minimal targets.
    It's the universal format that works everywhere.
    
    Args:
        target: The detected or configured target
        
    Returns:
        bool: True if AGENTS.md should be generated
    """
    return target in ("vscode", "opencode", "codex", "all", "minimal")


def should_compile_claude_md(target: TargetType) -> bool:
    """Check if CLAUDE.md should be compiled.
    
    Args:
        target: The detected or configured target
        
    Returns:
        bool: True if CLAUDE.md should be generated
    """
    return target in ("claude", "all")


def get_target_description(target: UserTargetType) -> str:
    """Get a human-readable description of what will be generated for a target.
    
    Accepts both internal target types and user-facing aliases.
    
    Args:
        target: The target type (internal or user-facing alias)
        
    Returns:
        str: Description of output files
    """
    # Normalize aliases to internal value for lookup
    normalized = "vscode" if target in ("copilot", "agents") else target
    descriptions = {
        "vscode": "AGENTS.md + .github/prompts/ + .github/agents/",
        "claude": "CLAUDE.md + .claude/commands/ + .claude/agents/ + .claude/skills/",
        "cursor": ".cursor/agents/ + .cursor/skills/ + .cursor/rules/",
        "opencode": "AGENTS.md + .opencode/agents/ + .opencode/commands/ + .opencode/skills/",
        "codex": "AGENTS.md + .agents/skills/ + .codex/agents/ + .codex/hooks.json",
        "all": "AGENTS.md + CLAUDE.md + .github/ + .claude/ + .cursor/ + .opencode/ + .codex/ + .agents/",
        "minimal": "AGENTS.md only (create .github/ or .claude/ for full integration)",
    }
    return descriptions.get(normalized, "unknown target")


# ---------------------------------------------------------------------------
# Multi-target helpers (used by active_targets() in the integration layer)
# ---------------------------------------------------------------------------

#: The complete set of real (non-pseudo) canonical targets.
#: "minimal" is intentionally excluded -- it is a fallback pseudo-target.
ALL_CANONICAL_TARGETS = frozenset({"vscode", "claude", "cursor", "opencode", "codex"})

#: Alias mapping: user-facing name -> canonical internal name.
TARGET_ALIASES: dict[str, str] = {
    "copilot": "vscode",
    "agents": "vscode",
    "vscode": "vscode",
}


def normalize_target_list(
    value: Union[str, List[str], None],
) -> Optional[List[str]]:
    """Normalize a user-provided target value to a list of canonical names.

    Handles:
    - ``None`` -> ``None`` (auto-detect)
    - ``"claude"`` -> ``["claude"]``
    - ``"copilot"`` -> ``["vscode"]``  (alias resolution)
    - ``"all"`` -> ``["claude", "codex", "copilot", "cursor", "opencode"]``
    - ``["claude", "copilot"]`` -> ``["claude", "vscode"]``
    - Deduplicates while preserving first-seen order.

    Args:
        value: A single target string, a list of target strings, or ``None``.

    Returns:
        A deduplicated list of canonical target names, or ``None`` if the
        input was ``None`` (meaning "auto-detect").
    """
    if value is None:
        return None

    raw: List[str] = [value] if isinstance(value, str) else list(value)

    # "all" anywhere in the input means "every target" -- expand to the
    # full sorted list of canonical targets.
    if "all" in raw:
        return sorted(ALL_CANONICAL_TARGETS)

    seen: set[str] = set()
    result: List[str] = []
    for item in raw:
        canonical = TARGET_ALIASES.get(item, item)
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


# ---------------------------------------------------------------------------
# Click parameter type for --target (comma-separated multi-target support)
# ---------------------------------------------------------------------------

#: All values accepted by the ``--target`` CLI option.
#: Derived from canonical targets, alias keys, and the ``"all"`` keyword.
VALID_TARGET_VALUES: frozenset[str] = (
    ALL_CANONICAL_TARGETS | frozenset(TARGET_ALIASES) | frozenset({"all"})
)


class TargetParamType(click.ParamType):
    """Click parameter type accepting comma-separated target values.

    Single values and ``"all"`` are returned as plain strings for backward
    compatibility with existing command handlers.  Multiple comma-separated
    targets are returned as a deduplicated ``list[str]`` of canonical names.

    Examples::

        -t claude             -> "claude"
        -t claude,copilot     -> ["claude", "vscode"]
        -t all                -> "all"
        -t copilot,vscode     -> ["vscode"]  (deduped aliases)
    """

    name = "target"

    def convert(
        self,
        value: Union[str, List[str], None],
        param: Optional[click.Parameter],
        ctx: Optional[click.Context],
    ) -> Union[str, List[str], None]:
        if value is None:
            return None
        # If already converted (e.g. from a default), pass through.
        if isinstance(value, list):
            return value

        # Split on comma, normalize whitespace & case, drop empty parts.
        parts = [v.strip().lower() for v in value.split(",") if v.strip()]
        if not parts:
            self.fail("target value must not be empty", param, ctx)

        # Validate every token.
        for p in parts:
            if p not in VALID_TARGET_VALUES:
                self.fail(
                    f"'{p}' is not a valid target. "
                    f"Choose from: {', '.join(sorted(VALID_TARGET_VALUES))}",
                    param,
                    ctx,
                )

        # "all" is exclusive -- reject combinations like "all,claude".
        if "all" in parts:
            if len(parts) > 1:
                self.fail(
                    "'all' cannot be combined with other targets",
                    param,
                    ctx,
                )
            return "all"

        # Single target -> plain string (backward compat).
        if len(parts) == 1:
            return parts[0]

        # Multi-target: resolve aliases and deduplicate.
        seen: set[str] = set()
        result: List[str] = []
        for p in parts:
            canonical = TARGET_ALIASES.get(p, p)
            if canonical not in seen:
                seen.add(canonical)
                result.append(canonical)
        # If aliases collapsed everything to one target, return a string.
        if len(result) == 1:
            return result[0]
        return result
