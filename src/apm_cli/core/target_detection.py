"""Target detection for auto-selecting compilation and integration targets.

This module implements the auto-detection pattern for determining which agent
targets (VSCode/Copilot, Claude, OpenCode) should be used based on existing
project structure and configuration.

Detection priority (highest to lowest):
1. Explicit --target flag (always wins)
2. apm.yml target setting (top-level field)
3. Auto-detect from existing folders:
   - .github/ exists only → vscode
   - .claude/ exists only → claude
   - .opencode/ exists only → opencode
   - Multiple integration folders exist → all
   - None exist → minimal (AGENTS.md only, no folder integration)
"""

from pathlib import Path
from typing import Literal, Optional, Tuple

# Valid target values
TargetType = Literal["vscode", "claude", "opencode", "all", "minimal"]


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
        if explicit_target in ("vscode", "agents"):
            return "vscode", "explicit --target flag"
        elif explicit_target == "claude":
            return "claude", "explicit --target flag"
        elif explicit_target == "opencode":
            return "opencode", "explicit --target flag"
        elif explicit_target == "all":
            return "all", "explicit --target flag"

    # Priority 2: apm.yml target setting
    if config_target:
        if config_target in ("vscode", "agents"):
            return "vscode", "apm.yml target"
        elif config_target == "claude":
            return "claude", "apm.yml target"
        elif config_target == "opencode":
            return "opencode", "apm.yml target"
        elif config_target == "all":
            return "all", "apm.yml target"

    # Priority 3: Auto-detect from existing folders
    github_exists = (project_root / ".github").exists()
    claude_exists = (project_root / ".claude").exists()
    opencode_exists = (project_root / ".opencode").exists()

    enabled_targets = []
    if github_exists:
        enabled_targets.append("vscode")
    if claude_exists:
        enabled_targets.append("claude")
    if opencode_exists:
        enabled_targets.append("opencode")

    if enabled_targets == ["vscode"]:
        return "vscode", "detected .github/ folder"
    elif enabled_targets == ["claude"]:
        return "claude", "detected .claude/ folder"
    elif enabled_targets == ["opencode"]:
        return "opencode", "detected .opencode/ folder"
    elif len(enabled_targets) > 1:
        labels = {
            "vscode": ".github/",
            "claude": ".claude/",
            "opencode": ".opencode/",
        }
        joined = ", ".join(labels[target] for target in enabled_targets)
        return "all", f"detected multiple integration folders ({joined})"
    else:
        # Neither folder exists - minimal output
        return "minimal", "no .github/, .claude/, or .opencode/ folder found"


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
        bool: True if OpenCode integration should run
    """
    return target in ("opencode", "all")


def should_compile_agents_md(target: TargetType) -> bool:
    """Check if AGENTS.md should be compiled.

    AGENTS.md is generated for vscode, opencode, all, and minimal targets.
    It's the universal format that works everywhere.

    Args:
        target: The detected or configured target

    Returns:
        bool: True if AGENTS.md should be generated
    """
    return target in ("vscode", "opencode", "all", "minimal")


def should_compile_claude_md(target: TargetType) -> bool:
    """Check if CLAUDE.md should be compiled.

    Args:
        target: The detected or configured target

    Returns:
        bool: True if CLAUDE.md should be generated
    """
    return target in ("claude", "all")


def get_target_description(target: TargetType) -> str:
    """Get a human-readable description of what will be generated for a target.

    Args:
        target: The target type

    Returns:
        str: Description of output files
    """
    descriptions = {
        "vscode": "AGENTS.md + .github/prompts/ + .github/agents/",
        "claude": "CLAUDE.md + .claude/commands/ + .claude/agents/ + .claude/skills/",
        "opencode": "AGENTS.md + .opencode/commands/ + .opencode/skills/",
        "all": "AGENTS.md + CLAUDE.md + .github/ + .claude/ + .opencode/",
        "minimal": "AGENTS.md only (create .github/ or .claude/ for full integration)",
    }
    return descriptions.get(target, "unknown target")
