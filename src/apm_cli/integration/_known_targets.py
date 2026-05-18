# pylint: disable=duplicate-code
"""Known target profiles and runtime aliases for multi-tool integration.

Extracted from ``target_runtime`` to keep that module under 400 LOC.
``RUNTIME_TO_CANONICAL_TARGET`` and ``KNOWN_TARGETS`` are re-exported from
``target_runtime`` for backward compatibility; new code should import them
from here directly.
"""

from __future__ import annotations

import sys

from .targets import PrimitiveMapping, TargetProfile, _flag_gated

__all__ = [
    "KNOWN_TARGETS",
    "RUNTIME_TO_CANONICAL_TARGET",
]

RUNTIME_TO_CANONICAL_TARGET: dict[str, str] = {
    "vscode": "copilot",
    "agents": "copilot",
}
KNOWN_TARGETS: dict[str, TargetProfile] = {
    # Copilot (GitHub) -- at user scope, Copilot CLI reads ~/.copilot/
    # instead of ~/.github/.  Prompts and instructions are not supported at user scope.
    # Ref: https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/create-custom-agents-for-cli
    "copilot": TargetProfile(
        name="copilot",
        root_dir=".github",
        primitives={
            "instructions": PrimitiveMapping(
                "instructions", ".instructions.md", "github_instructions"
            ),
            "prompts": PrimitiveMapping("prompts", ".prompt.md", "github_prompt"),
            "agents": PrimitiveMapping("agents", ".agent.md", "github_agent"),
            "skills": PrimitiveMapping(
                "skills",
                "/SKILL.md",
                "skill_standard",
                deploy_root=".agents",
            ),
            "hooks": PrimitiveMapping("hooks", ".json", "github_hooks"),
        },
        auto_create=True,
        detect_by_dir=True,
        user_supported="partial",
        user_root_dir=".copilot",
        unsupported_user_primitives=("prompts", "instructions"),
        generated_files=("copilot-instructions.md",),
        compile_family="vscode",
    ),
    # Claude Code -- the user-level config directory is whatever
    # ``CLAUDE_CONFIG_DIR`` points to (default ``~/.claude``).  The env
    # var override is honored by ``for_scope(user_scope=True)``.
    # All primitives are supported at user scope.
    # Ref: https://docs.anthropic.com/en/docs/claude-code/settings
    # Instructions deploy to <root>/rules/*.md with paths: frontmatter.
    # Ref: https://code.claude.com/docs/en/memory#organize-rules-with-claude%2Frules%2F
    "claude": TargetProfile(
        name="claude",
        root_dir=".claude",
        primitives={
            "instructions": PrimitiveMapping("rules", ".md", "claude_rules"),
            "agents": PrimitiveMapping("agents", ".md", "claude_agent"),
            "commands": PrimitiveMapping("commands", ".md", "claude_command"),
            "skills": PrimitiveMapping("skills", "/SKILL.md", "skill_standard"),
            "hooks": PrimitiveMapping("hooks", ".json", "claude_hooks"),
        },
        auto_create=False,
        detect_by_dir=True,
        user_supported=True,
        compile_family="claude",
        hooks_config_display=".claude/settings.json",
    ),
    # Cursor -- at user scope, ~/.cursor/ supports skills, agents, hooks,
    # and MCP.  Rules/instructions are managed via Cursor Settings UI only
    # (not file-based), so "instructions" is excluded from user scope.
    # Ref: https://cursor.com/docs/rules
    "cursor": TargetProfile(
        name="cursor",
        root_dir=".cursor",
        primitives={
            "instructions": PrimitiveMapping("rules", ".mdc", "cursor_rules"),
            "agents": PrimitiveMapping("agents", ".md", "cursor_agent"),
            # TODO(cursor-command-format): track via dedicated issue once
            # filed.  Cursor command deployment reuses the shared command
            # transformer (claude_command), which preserves only the
            # supported common frontmatter subset (description,
            # allowed-tools, model, argument-hint, input).  Switch to a
            # dedicated "cursor_command" format when the integrator
            # implements a Cursor-specific writer that preserves
            # Cursor-specific prompt metadata (author, mcp, parameters,
            # ...) verbatim.  Dropped keys are surfaced via
            # diagnostics.warn() at install time -- see
            # command_integrator.
            "commands": PrimitiveMapping("commands", ".md", "claude_command"),
            "skills": PrimitiveMapping(
                "skills",
                "/SKILL.md",
                "skill_standard",
                deploy_root=".agents",
            ),
            "hooks": PrimitiveMapping("hooks", ".json", "cursor_hooks"),
        },
        auto_create=False,
        detect_by_dir=True,
        user_supported="partial",
        user_root_dir=".cursor",
        unsupported_user_primitives=("instructions",),
        compile_family="agents",
        hooks_config_display=".cursor/hooks.json",
    ),
    # OpenCode -- at user scope, ~/.config/opencode/ supports skills, agents,
    # and commands.  OpenCode has no hooks concept, so "hooks" is excluded.
    "opencode": TargetProfile(
        name="opencode",
        root_dir=".opencode",
        primitives={
            "agents": PrimitiveMapping("agents", ".md", "opencode_agent"),
            "commands": PrimitiveMapping("commands", ".md", "opencode_command"),
            "skills": PrimitiveMapping(
                "skills",
                "/SKILL.md",
                "skill_standard",
                deploy_root=".agents",
            ),
        },
        auto_create=False,
        detect_by_dir=True,
        user_supported="partial",
        user_root_dir=".config/opencode",
        unsupported_user_primitives=("hooks",),
        compile_family="agents",
    ),
    # Gemini CLI -- ~/.gemini/ is the documented user-level config directory.
    # Instructions are compile-only (GEMINI.md) -- Gemini CLI does not read
    # per-file rules from .gemini/rules/.
    # Commands are TOML files under .gemini/commands/.
    # Hooks merge into .gemini/settings.json (same pattern as Claude Code).
    # Ref: https://geminicli.com/docs/cli/gemini-md/
    # Ref: https://geminicli.com/docs/reference/configuration/
    "gemini": TargetProfile(
        name="gemini",
        root_dir=".gemini",
        primitives={
            "commands": PrimitiveMapping("commands", ".toml", "gemini_command"),
            "skills": PrimitiveMapping(
                "skills",
                "/SKILL.md",
                "skill_standard",
                deploy_root=".agents",
            ),
            "hooks": PrimitiveMapping("hooks", ".json", "gemini_hooks"),
        },
        auto_create=False,
        detect_by_dir=True,
        user_supported=True,
        user_root_dir=".gemini",
        compile_family="gemini",
        hooks_config_display=".gemini/settings.json",
    ),
    # Codex CLI: skills use the cross-tool .agents/ dir (agent skills standard),
    # agents are TOML under .codex/agents/, hooks merge into .codex/hooks.json.
    # Instructions are compile-only (AGENTS.md) -- not installed.
    "codex": TargetProfile(
        name="codex",
        root_dir=".codex",
        primitives={
            "agents": PrimitiveMapping("agents", ".toml", "codex_agent"),
            "skills": PrimitiveMapping(
                "skills",
                "/SKILL.md",
                "skill_standard",
                deploy_root=".agents",
            ),
            "hooks": PrimitiveMapping("", "hooks.json", "codex_hooks"),
        },
        auto_create=False,
        detect_by_dir=True,
        user_supported="partial",
        pack_prefixes=(".codex/", ".agents/"),
        compile_family="agents",
        hooks_config_display=".codex/hooks.json",
    ),
    # Windsurf/Cascade -- .windsurf/ is the workspace config directory.
    # Rules are markdown files with trigger/globs frontmatter under .windsurf/rules/.
    # Agents are deployed as skills under .windsurf/skills/<name>/SKILL.md
    # (Cascade auto-invokes them when the description matches the task).
    # Skills use the standard SKILL.md format under .windsurf/skills/.
    # Workflows (~= commands) are markdown files under .windsurf/workflows/.
    # Hooks are configured in .windsurf/hooks.json.
    # At user scope, ~/.codeium/windsurf/ is used.  Global rules use a single
    # file (~/.codeium/windsurf/memories/global_rules.md) with a different
    # format, so "instructions" is excluded from user scope.
    # MCP config: ~/.codeium/windsurf/mcp_config.json (mcpServers JSON format).
    # Ref: https://docs.windsurf.com/windsurf/cascade/memories
    # Ref: https://docs.windsurf.com/windsurf/cascade/mcp
    "windsurf": TargetProfile(
        name="windsurf",
        root_dir=".windsurf",
        primitives={
            "instructions": PrimitiveMapping("rules", ".md", "windsurf_rules"),
            "agents": PrimitiveMapping("skills", "/SKILL.md", "windsurf_agent_skill"),
            "skills": PrimitiveMapping("skills", "/SKILL.md", "skill_standard"),
            "commands": PrimitiveMapping("workflows", ".md", "windsurf_workflow"),
            "hooks": PrimitiveMapping("", "hooks.json", "windsurf_hooks"),
        },
        auto_create=False,
        detect_by_dir=True,
        user_supported="partial",
        user_root_dir=".codeium/windsurf",
        unsupported_user_primitives=("instructions",),
        compile_family="agents",
        hooks_config_display=".windsurf/hooks.json",
    ),
    # Agent-skills: cross-client shared skills directory (.agents/skills/).
    # Skills primitive only -- no agents, hooks, or commands.
    # Not auto-detected (detect_by_dir=False) because .agents/ is shared by
    # multiple tools (Codex, etc.). Explicit --target agent-skills only.
    "agent-skills": TargetProfile(
        name="agent-skills",
        root_dir=".agents",
        primitives={
            "skills": PrimitiveMapping(
                "skills",
                "/SKILL.md",
                "skill_standard",
            ),
        },
        auto_create=True,
        detect_by_dir=False,
        user_supported=True,
        user_root_dir=".agents",
        generated_files=(),
    ),
    # Microsoft 365 Copilot (Cowork) -- experimental, user-scope only.
    # Skills are deployed to <OneDrive>/Documents/Cowork/skills/.
    # The deploy root is resolved dynamically at runtime via
    # copilot_cowork_paths.resolve_copilot_cowork_skills_dir().
    # Non-skill primitives are not supported.
    "copilot-cowork": TargetProfile(
        name="copilot-cowork",
        root_dir="copilot-cowork",  # display grouping placeholder only
        primitives={
            "skills": PrimitiveMapping(
                "skills",
                "/SKILL.md",
                "skill_standard",
            ),
        },
        auto_create=False,
        detect_by_dir=False,
        user_supported=True,
        user_root_resolver=lambda: sys.modules[
            "apm_cli.integration.targets"
        ]._resolve_copilot_cowork_root(),
        requires_flag="copilot_cowork",
    ),
}
