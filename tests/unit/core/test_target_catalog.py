"""Characterization tests for target capability metadata."""

from dataclasses import astuple

from apm_cli.core.target_detection import (
    ALL_CANONICAL_TARGETS,
    EXPERIMENTAL_TARGETS,
    EXPLICIT_ONLY_TARGETS,
    MCP_ONLY_TARGETS,
    TARGET_ALIASES,
    VALID_TARGET_VALUES,
)
from apm_cli.integration.targets import KNOWN_TARGETS, RUNTIME_TO_CANONICAL_TARGET


def test_current_target_sets_and_aliases_are_characterized() -> None:
    """Lock the accepted target contract before moving its owner."""
    assert (
        frozenset({"claude", "codex", "cursor", "gemini", "kiro", "opencode", "vscode", "windsurf"})
        == ALL_CANONICAL_TARGETS
    )
    assert (
        frozenset({"copilot-app", "copilot-cowork", "hermes", "openclaw"}) == EXPERIMENTAL_TARGETS
    )
    assert frozenset({"agent-skills", "antigravity"}) == EXPLICIT_ONLY_TARGETS
    assert frozenset({"intellij"}) == MCP_ONLY_TARGETS
    assert TARGET_ALIASES == {
        "agy": "antigravity",
        "agents": "vscode",
        "copilot": "vscode",
        "vscode": "vscode",
    }
    assert (
        frozenset(
            {
                "agent-skills",
                "agents",
                "agy",
                "all",
                "antigravity",
                "claude",
                "codex",
                "copilot",
                "copilot-app",
                "copilot-cowork",
                "cursor",
                "gemini",
                "hermes",
                "intellij",
                "kiro",
                "openclaw",
                "opencode",
                "vscode",
                "windsurf",
            }
        )
        == VALID_TARGET_VALUES
    )


def test_current_runtime_mapping_is_characterized() -> None:
    """Lock runtime-to-native-profile routing before moving its owner."""
    assert RUNTIME_TO_CANONICAL_TARGET == {
        "agents": "copilot",
        "intellij": "copilot",
        "vscode": "copilot",
    }


def test_current_native_profiles_are_characterized() -> None:
    """Lock native roots, primitive mappings, flags, and compile families."""
    actual = {
        name: (
            profile.root_dir,
            {primitive: astuple(mapping) for primitive, mapping in profile.primitives.items()},
            profile.compile_family,
            profile.requires_flag,
        )
        for name, profile in KNOWN_TARGETS.items()
    }
    assert actual == {
        "copilot": (
            ".github",
            {
                "instructions": (
                    "instructions",
                    ".instructions.md",
                    "github_instructions",
                    None,
                    False,
                ),
                "prompts": ("prompts", ".prompt.md", "github_prompt", None, False),
                "agents": ("agents", ".agent.md", "github_agent", None, False),
                "skills": ("skills", "/SKILL.md", "skill_standard", ".agents", False),
                "hooks": ("hooks", ".json", "github_hooks", None, False),
                "canvas": ("extensions", "", "copilot_canvas", None, False),
            },
            "vscode",
            None,
        ),
        "claude": (
            ".claude",
            {
                "instructions": ("rules", ".md", "claude_rules", None, True),
                "agents": ("agents", ".md", "claude_agent", None, False),
                "commands": ("commands", ".md", "claude_command", None, False),
                "skills": ("skills", "/SKILL.md", "skill_standard", None, False),
                "hooks": ("hooks", ".json", "claude_hooks", None, False),
            },
            "claude",
            None,
        ),
        "cursor": (
            ".cursor",
            {
                "instructions": ("rules", ".mdc", "cursor_rules", None, True),
                "agents": ("agents", ".md", "cursor_agent", None, False),
                "commands": ("commands", ".md", "claude_command", None, False),
                "skills": ("skills", "/SKILL.md", "skill_standard", ".agents", False),
                "hooks": ("hooks", ".json", "cursor_hooks", None, False),
            },
            "agents",
            None,
        ),
        "kiro": (
            ".kiro",
            {
                "instructions": ("steering", ".md", "kiro_steering", None, True),
                "skills": ("skills", "/SKILL.md", "skill_standard", None, False),
                "hooks": ("hooks", ".json", "kiro_hooks", None, False),
            },
            "agents",
            None,
        ),
        "opencode": (
            ".opencode",
            {
                "agents": ("agents", ".md", "opencode_agent", None, False),
                "commands": ("commands", ".md", "opencode_command", None, False),
                "skills": ("skills", "/SKILL.md", "skill_standard", ".agents", False),
            },
            "agents",
            None,
        ),
        "gemini": (
            ".gemini",
            {
                "commands": ("commands", ".toml", "gemini_command", None, False),
                "skills": ("skills", "/SKILL.md", "skill_standard", ".agents", False),
                "hooks": ("hooks", ".json", "gemini_hooks", None, False),
            },
            "gemini",
            None,
        ),
        "antigravity": (
            ".agents",
            {
                "instructions": ("rules", ".md", "antigravity_rules", None, True),
                "skills": ("skills", "/SKILL.md", "skill_standard", None, False),
                "hooks": ("", "hooks.json", "antigravity_hooks", None, False),
            },
            "agents",
            None,
        ),
        "codex": (
            ".codex",
            {
                "agents": ("agents", ".toml", "codex_agent", None, False),
                "skills": ("skills", "/SKILL.md", "skill_standard", ".agents", False),
                "hooks": ("", "hooks.json", "codex_hooks", None, False),
            },
            "agents",
            None,
        ),
        "windsurf": (
            ".windsurf",
            {
                "instructions": ("rules", ".md", "windsurf_rules", None, True),
                "skills": ("skills", "/SKILL.md", "skill_standard", ".agents", False),
                "commands": ("workflows", ".md", "windsurf_workflow", None, False),
                "hooks": ("", "hooks.json", "windsurf_hooks", None, False),
            },
            "agents",
            None,
        ),
        "agent-skills": (
            ".agents",
            {"skills": ("skills", "/SKILL.md", "skill_standard", None, False)},
            None,
            None,
        ),
        "openclaw": (
            ".agents",
            {"skills": ("skills", "/SKILL.md", "skill_standard", None, False)},
            None,
            "openclaw",
        ),
        "hermes": (
            ".agents",
            {"skills": ("skills", "/SKILL.md", "skill_standard", None, False)},
            "agents",
            "hermes",
        ),
        "copilot-cowork": (
            "copilot-cowork",
            {"skills": ("skills", "/SKILL.md", "skill_standard", None, False)},
            None,
            "copilot_cowork",
        ),
        "copilot-app": (
            "copilot-app",
            {"prompts": ("workflows", ".prompt.md", "prompt_standard", None, False)},
            None,
            "copilot_app",
        ),
    }
