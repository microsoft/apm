"""Original APM target definitions exposed as built-in harness adapters."""

from __future__ import annotations

from collections.abc import Mapping
from importlib import import_module
from types import MappingProxyType

from apm_cli.harnesses.types import (
    HarnessAdapter,
    PrimitiveMapping,
    TargetCapability,
    TargetProfile,
)

_TARGET_COMMANDS = frozenset({"compile", "install", "update"})


def _capability(
    name: str,
    description: str,
    *,
    aliases: tuple[str, ...] = (),
    in_all: bool = False,
    explicit_only: bool = False,
    experimental_flag: str | None = None,
    mcp_only: bool = False,
    primitive_profile: str | None = None,
    compile_family: str | None = None,
    runtimes: tuple[str, ...] = (),
    meta_target: bool = False,
    accepts_agent_skills_packages: bool = False,
) -> TargetCapability:
    return TargetCapability(
        name=name,
        aliases=aliases,
        description=description,
        in_all=in_all,
        explicit_only=explicit_only,
        experimental_flag=experimental_flag,
        mcp_only=mcp_only,
        primitive_profile=primitive_profile,
        compile_family=compile_family,
        runtimes=runtimes,
        commands=_TARGET_COMMANDS,
        meta_target=meta_target,
        accepts_agent_skills_packages=accepts_agent_skills_packages,
    )


def _build_target_catalog(capabilities):
    catalog = {}
    value_owners = {}
    runtime_owners = {}
    for capability in capabilities:
        if not capability.name:
            raise ValueError("Target capability name must not be empty")
        if not capability.description:
            raise ValueError(f"Target '{capability.name}' must have a description")
        if not capability.commands:
            raise ValueError(f"Target '{capability.name}' must declare at least one command")
        if capability.name in value_owners:
            raise ValueError(f"Duplicate target value '{capability.name}'")
        if capability.mcp_only and capability.primitive_profile is None:
            raise ValueError(f"MCP-only target '{capability.name}' must declare primitive_profile")
        if capability.in_all and (
            capability.experimental_flag is not None or capability.explicit_only
        ):
            raise ValueError(
                f"Target '{capability.name}' cannot set in_all with "
                "experimental_flag or explicit_only"
            )
        catalog[capability.name] = capability
        value_owners[capability.name] = capability.name
        for alias in capability.aliases:
            if alias in value_owners:
                raise ValueError(f"Duplicate target value '{alias}'")
            value_owners[alias] = capability.name
        for runtime in capability.runtimes:
            owner = runtime_owners.get(runtime)
            if owner is not None:
                raise ValueError(
                    f"Runtime '{runtime}' maps to multiple targets: "
                    f"'{owner}' and '{capability.name}'"
                )
            runtime_owners[runtime] = capability.name
    return MappingProxyType(catalog)


def _resolve_copilot_cowork_root():
    from apm_cli.integration.targets import _resolve_copilot_cowork_root as resolve

    return resolve()


def _resolve_copilot_app_root():
    from apm_cli.integration.targets import _resolve_copilot_app_root as resolve

    return resolve()


def _encode_cowork_locator(path, deploy_root):
    from apm_cli.integration.targets import _encode_cowork_locator as encode

    return encode(path, deploy_root)


def _decode_cowork_locator(locator, deploy_root):
    from apm_cli.integration.targets import _decode_cowork_locator as decode

    return decode(locator, deploy_root)


def _encode_copilot_app_locator(path):
    from apm_cli.integration.targets import _encode_copilot_app_locator as encode

    return encode(path)


def _client_constructor(module_name: str, class_name: str):
    def construct(project_root=None, user_scope=False):
        adapter_type = getattr(import_module(module_name), class_name)
        return adapter_type(project_root=project_root, user_scope=user_scope)

    return construct


TARGET_CAPABILITIES: Mapping[str, TargetCapability] = _build_target_catalog(
    (
        _capability(
            "agent-skills",
            "Cross-client native .agents skills configuration",
            explicit_only=True,
            primitive_profile="agent-skills",
            meta_target=True,
        ),
        _capability(
            "antigravity",
            "Antigravity native .agents configuration",
            aliases=("agy",),
            explicit_only=True,
            primitive_profile="antigravity",
            compile_family="agents",
        ),
        _capability(
            "claude",
            "Claude Code native .claude configuration",
            in_all=True,
            primitive_profile="claude",
            compile_family="claude",
        ),
        _capability(
            "codex",
            "Codex native .codex and .agents configuration",
            in_all=True,
            primitive_profile="codex",
            compile_family="agents",
        ),
        _capability(
            "copilot",
            "GitHub Copilot native .github configuration",
            aliases=("vscode", "agents"),
            in_all=True,
            primitive_profile="copilot",
            compile_family="vscode",
            runtimes=("copilot", "vscode", "agents"),
        ),
        _capability(
            "copilot-app",
            "GitHub Copilot desktop app native workflow configuration",
            experimental_flag="copilot_app",
            primitive_profile="copilot-app",
        ),
        _capability(
            "copilot-cowork",
            "Microsoft 365 Copilot Cowork native skills configuration",
            experimental_flag="copilot_cowork",
            primitive_profile="copilot-cowork",
        ),
        _capability(
            "cursor",
            "Cursor native .cursor configuration",
            in_all=True,
            primitive_profile="cursor",
            compile_family="agents",
        ),
        _capability(
            "gemini",
            "Gemini CLI native .gemini configuration",
            in_all=True,
            primitive_profile="gemini",
            compile_family="gemini",
        ),
        _capability(
            "grok-bot",
            "Grok Bot native agent-data/workflows skills configuration",
            explicit_only=True,
            primitive_profile="grok-bot",
            accepts_agent_skills_packages=True,
        ),
        _capability(
            "grok-build",
            "xAI Grok Build native .grok configuration",
            in_all=True,
            primitive_profile="grok-build",
            compile_family="agents",
        ),
        _capability(
            "grok-cloud",
            "xAI Grok Cloud native .grok/skills configuration",
            explicit_only=True,
            experimental_flag="grok_cloud",
            primitive_profile="grok-cloud",
        ),
        _capability(
            "hermes",
            "Hermes native skills and MCP configuration",
            explicit_only=True,
            primitive_profile="hermes",
            compile_family="agents",
        ),
        _capability(
            "intellij",
            "IntelliJ MCP integration using the Copilot primitive profile",
            mcp_only=True,
            primitive_profile="copilot",
            compile_family="agents",
            runtimes=("intellij",),
        ),
        _capability(
            "kiro",
            "Kiro native .kiro configuration",
            in_all=True,
            primitive_profile="kiro",
            compile_family="agents",
        ),
        _capability(
            "openclaw",
            "OpenClaw native skills configuration",
            experimental_flag="openclaw",
            primitive_profile="openclaw",
        ),
        _capability(
            "opencode",
            "OpenCode native .opencode configuration",
            in_all=True,
            primitive_profile="opencode",
            compile_family="agents",
        ),
        _capability(
            "windsurf",
            "Windsurf native .windsurf and .agents configuration",
            in_all=True,
            primitive_profile="windsurf",
            compile_family="agents",
        ),
    )
)
KNOWN_TARGETS: dict[str, TargetProfile] = {
    # Agent-skills: cross-client shared skills directory (.agents/skills/).
    # Skills primitive only -- no agents, hooks, or commands.
    # Not auto-detected (detect_by_dir=False) because .agents/ is shared by
    # multiple tools (Codex, etc.). Explicit --target agent-skills only.
    "agent-skills": TargetProfile(
        capability=TARGET_CAPABILITIES["agent-skills"],
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
    # Antigravity CLI (agy) -- Google's Gemini-derived agentic CLI.
    # Workspace config lives under the cross-tool .agents/ root (the same
    # shared root used for agent skills); Antigravity has no unique
    # workspace directory of its own, so this target is EXPLICIT-ONLY --
    # never auto-detected and not part of `--target all` -- modelled on
    # the agent-skills target.
    # Rules are native markdown under .agents/rules/ with trigger/globs
    # frontmatter mapped from instruction applyTo patterns.
    # Skills use the cross-tool .agents/skills/ standard.
    # Hooks merge into a single .agents/hooks.json file in Antigravity's
    # OWN native schema (PreToolUse/PostToolUse/PreInvocation/
    # PostInvocation/Stop), NOT the Gemini settings.json hook schema.
    # MCP servers live in a dedicated .agents/mcp_config.json (written by
    # AntigravityClientAdapter), NOT settings.json.
    # Antigravity has no TOML command surface (legacy Gemini commands
    # convert to skills upstream), so there is no commands primitive.
    # User scope: skills -> ~/.gemini/antigravity-cli/skills/; MCP ->
    # ~/.gemini/config/mcp_config.json (handled by the adapter).
    # Instructions/hooks are not offered at user scope because Antigravity
    # spreads them across heterogeneous ~/.gemini/ subdirs.
    # Compile family is "agents" (emits AGENTS.md, not GEMINI.md).
    # Ref: https://antigravity.google/docs/cli-using
    # Ref: https://antigravity.google/docs/skills
    # Ref: https://antigravity.google/docs/hooks
    # Ref: https://antigravity.google/docs/mcp
    "antigravity": TargetProfile(
        capability=TARGET_CAPABILITIES["antigravity"],
        root_dir=".agents",
        primitives={
            "instructions": PrimitiveMapping(
                "rules", ".md", "antigravity_rules", output_compare=True
            ),
            "skills": PrimitiveMapping(
                "skills",
                "/SKILL.md",
                "skill_standard",
            ),
            "hooks": PrimitiveMapping("", "hooks.json", "antigravity_hooks"),
        },
        auto_create=True,
        detect_by_dir=False,
        user_supported="partial",
        user_root_dir=".gemini/antigravity-cli",
        unsupported_user_primitives=("instructions", "hooks"),
        hooks_config_display=".agents/hooks.json",
    ),
    # Claude Code -- the user-level config directory is whatever
    # ``CLAUDE_CONFIG_DIR`` points to (default ``~/.claude``).  The env
    # var override is honored by ``for_scope(user_scope=True)``.
    # All primitives are supported at user scope.
    # Ref: https://docs.anthropic.com/en/docs/claude-code/settings
    # Instructions deploy to <root>/rules/*.md with paths: frontmatter.
    # Ref: https://code.claude.com/docs/en/memory#organize-rules-with-claude%2Frules%2F
    "claude": TargetProfile(
        capability=TARGET_CAPABILITIES["claude"],
        root_dir=".claude",
        primitives={
            "instructions": PrimitiveMapping(
                "rules",
                ".md",
                "claude_rules",
                output_compare=True,
            ),
            "agents": PrimitiveMapping("agents", ".md", "claude_agent"),
            "commands": PrimitiveMapping("commands", ".md", "claude_command"),
            "skills": PrimitiveMapping("skills", "/SKILL.md", "skill_standard"),
            "hooks": PrimitiveMapping("hooks", ".json", "claude_hooks"),
        },
        auto_create=False,
        detect_by_dir=True,
        user_supported=True,
        hooks_config_display=".claude/settings.json",
    ),
    # Codex CLI: skills use the cross-tool .agents/ dir (agent skills standard),
    # agents are TOML under .codex/agents/, hooks merge into .codex/hooks.json.
    # Instructions are compile-only (AGENTS.md) -- not installed.
    "codex": TargetProfile(
        capability=TARGET_CAPABILITIES["codex"],
        root_dir=".codex",
        primitives={
            "agents": PrimitiveMapping(
                "agents", ".toml", "codex_agent", prompt_fields=("developer_instructions",)
            ),
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
        hooks_config_display=".codex/hooks.json",
    ),
    # Copilot (GitHub) -- at user scope, Copilot CLI reads ~/.copilot/
    # instead of ~/.github/. Copilot CLI supports modular, path-scoped
    # instructions at user scope via ~/.copilot/instructions/**/*.instructions.md,
    # mirroring the project-scope .github/instructions/ layout.
    # Ref: https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions
    "copilot": TargetProfile(
        capability=TARGET_CAPABILITIES["copilot"],
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
            "canvas": PrimitiveMapping("extensions", "", "copilot_canvas"),
        },
        auto_create=True,
        detect_by_dir=True,
        # "partial": instructions deploy modularly under
        # ~/.copilot/instructions/ (restored above), but prompts/agents/
        # skills/hooks/canvas have no documented Copilot CLI user-scope
        # surface yet, so the target is not "True" (fully) supported.
        user_supported="partial",
        user_root_dir=".copilot",
        generated_files=("copilot-instructions.md",),
    ),
    # GitHub Copilot desktop App -- experimental, user-scope only.
    # Prompts whose frontmatter carries workflow-shape keys (``interval``,
    # ``schedule_hour``, ``schedule_day``) are installed as rows in the
    # app's ``workflows`` table at ``~/.copilot/data.db``.  ``mode`` /
    # ``model`` / ``reasoning_effort`` are optional fields on a workflow
    # but do NOT mark a plain prompt as a workflow (they overload with
    # plain VSCode / Copilot slash-command prompts).  No files are
    # written under the deploy root; the synthetic root is only used so
    # the existing target machinery can address rows via the
    # ``copilot-app-db://workflows/<id>`` lockfile URI scheme.
    "copilot-app": TargetProfile(
        capability=TARGET_CAPABILITIES["copilot-app"],
        root_dir="copilot-app",  # display grouping placeholder only
        primitives={
            "prompts": PrimitiveMapping(
                "workflows",
                ".prompt.md",
                "prompt_standard",
            ),
        },
        auto_create=False,
        detect_by_dir=False,
        user_supported=True,
        user_root_resolver=lambda: _resolve_copilot_app_root(),
        scope_invariant_resolver=True,
        external_locator_encoder=lambda path, _deploy_root: _encode_copilot_app_locator(path),
        lockfile_uri_schemes=("copilot-app-db://",),
    ),
    # Microsoft 365 Copilot (Cowork) -- experimental, user-scope only.
    # Skills are deployed to <OneDrive>/Documents/Cowork/skills/.
    # The deploy root is resolved dynamically at runtime via
    # copilot_cowork_paths.resolve_copilot_cowork_skills_dir().
    # Non-skill primitives are not supported.
    "copilot-cowork": TargetProfile(
        capability=TARGET_CAPABILITIES["copilot-cowork"],
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
        user_root_resolver=lambda: _resolve_copilot_cowork_root(),
        external_locator_encoder=lambda path, deploy_root: _encode_cowork_locator(
            path, deploy_root
        ),
        external_locator_decoder=lambda locator, deploy_root: _decode_cowork_locator(
            locator, deploy_root
        ),
        lockfile_uri_schemes=("cowork://",),
        warn_unsupported_primitives=True,
    ),
    # Cursor -- at user scope, ~/.cursor/ supports skills, agents, hooks,
    # and MCP.  Rules/instructions are managed via Cursor Settings UI only
    # (not file-based), so "instructions" is excluded from user scope.
    # Ref: https://cursor.com/docs/rules
    "cursor": TargetProfile(
        capability=TARGET_CAPABILITIES["cursor"],
        root_dir=".cursor",
        primitives={
            "instructions": PrimitiveMapping(
                "rules",
                ".mdc",
                "cursor_rules",
                output_compare=True,
            ),
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
        hooks_config_display=".cursor/hooks.json",
    ),
    # Gemini CLI -- ~/.gemini/ is the documented user-level config directory.
    # Instructions are compile-only (GEMINI.md) -- Gemini CLI does not read
    # per-file rules from .gemini/rules/.
    # Commands are TOML files under .gemini/commands/.
    # Hooks merge into .gemini/settings.json (same pattern as Claude Code).
    # Ref: https://geminicli.com/docs/cli/gemini-md/
    # Ref: https://geminicli.com/docs/reference/configuration/
    "gemini": TargetProfile(
        capability=TARGET_CAPABILITIES["gemini"],
        root_dir=".gemini",
        primitives={
            "commands": PrimitiveMapping(
                "commands", ".toml", "gemini_command", prompt_fields=("prompt",)
            ),
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
        hooks_config_display=".gemini/settings.json",
    ),
    # Grok Bot -- stable, explicit-only, skills-only target for the Grok Bot
    # agent runtime.  Grok Bot reads SKILL.md directories from
    # agent-data/workflows/<skill-name>/ at project scope and
    # ~/agent-data/workflows/<skill-name>/ at user scope.  Modelled on the
    # hermes/agent-skills targets: skills primitive only, no agents, hooks,
    # or commands.  Not auto-detected (detect_by_dir=False) since
    # agent-data/ is not shared with any other target's root_dir.
    "grok-bot": TargetProfile(
        capability=TARGET_CAPABILITIES["grok-bot"],
        root_dir="agent-data",
        primitives={
            "skills": PrimitiveMapping(
                "workflows",
                "/SKILL.md",
                "skill_standard",
            ),
        },
        auto_create=True,
        detect_by_dir=False,
        user_supported=True,
        user_root_dir="agent-data",
    ),
    # Grok Build -- project and user configuration live under .grok/.
    # Grok reads AGENTS.md for compiled project context and supports native
    # rules, agents, legacy command markdown, and Agent Skills.
    # Ref: https://github.com/xai-org/grok-build/tree/main/crates/codegen/xai-grok-pager/docs/user-guide
    "grok-build": TargetProfile(
        capability=TARGET_CAPABILITIES["grok-build"],
        root_dir=".grok",
        primitives={
            "instructions": PrimitiveMapping("rules", ".md", "grok_rules"),
            "agents": PrimitiveMapping("agents", ".md", "grok_agent"),
            "commands": PrimitiveMapping("commands", ".md", "claude_command"),
            "skills": PrimitiveMapping("skills", "/SKILL.md", "skill_standard"),
        },
        auto_create=False,
        detect_by_dir=True,
        user_supported=True,
        user_root_dir=".grok",
    ),
    # Grok Cloud -- xAI docs verify project ``./.grok/skills/`` and user
    # ``~/.grok/skills/``.  Skills are the only deployed primitive.
    "grok-cloud": TargetProfile(
        capability=TARGET_CAPABILITIES["grok-cloud"],
        root_dir=".grok",
        primitives={
            "skills": PrimitiveMapping("skills", "/SKILL.md", "skill_standard"),
        },
        auto_create=True,
        detect_by_dir=False,
        user_supported=True,
        user_root_dir=".grok",
    ),
    # Hermes agent (Nous Research) -- stable explicit-only. Hermes natively reads
    # the agentskills.io SKILL.md format and the AGENTS.md context-file
    # standard, both already emitted by APM, so skills + instructions reuse
    # the existing skill_standard / compile_family="agents" paths.  Skills
    # land in .agents/skills/ at project scope (read by Hermes via
    # skills.external_dirs) and ~/.hermes/skills/ at user scope.  MCP servers
    # are written separately by HermesClientAdapter to ~/.hermes/config.yaml.
    # $HERMES_HOME overrides the user-scope root (handled in for_scope).
    "hermes": TargetProfile(
        capability=TARGET_CAPABILITIES["hermes"],
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
        user_root_dir=".hermes",
    ),
    # Kiro IDE/CLI v3 -- spec-driven development editor.
    # Agents are Markdown files under .kiro/agents/; identity derives from
    # the relative path (no redundant 'name' frontmatter field).
    # Steering files use Kiro frontmatter under .kiro/steering/.
    # Skills use the open Agent Skills SKILL.md layout under .kiro/skills/.
    # Hooks are individual JSON files under .kiro/hooks/.
    # CLI V3 prompts are body-only .md files; the filename stem is their name.
    # MCP config lives at .kiro/settings/mcp.json and ~/.kiro/settings/mcp.json.
    # Ref: https://kiro.dev/docs/custom-agents/ (accessed 2026-08-03)
    # Ref: https://kiro.dev/docs/steering/
    # Ref: https://kiro.dev/docs/skills/
    # Ref: https://kiro.dev/docs/hooks/
    # Ref: https://kiro.dev/docs/cli/chat/manage-prompts/
    "kiro": TargetProfile(
        capability=TARGET_CAPABILITIES["kiro"],
        root_dir=".kiro",
        primitives={
            "agents": PrimitiveMapping("agents", ".md", "kiro_agent"),
            "instructions": PrimitiveMapping(
                "steering",
                ".md",
                "kiro_steering",
                output_compare=True,
            ),
            "prompts": PrimitiveMapping("prompts", ".md", "kiro_prompt"),
            "skills": PrimitiveMapping("skills", "/SKILL.md", "skill_standard"),
            "hooks": PrimitiveMapping("hooks", ".json", "kiro_hooks"),
        },
        auto_create=False,
        detect_by_dir=True,
        user_supported=True,
        user_root_dir=".kiro",
    ),
    # OpenClaw -- experimental, skills-only target for the OpenClaw agent
    # runtime (github.com/openclaw/openclaw).  OpenClaw reads SKILL.md
    # directories from several locations; APM deploys to:
    #   project scope: <workspace>/.agents/skills/ (agentskills.io standard,
    #                  OpenClaw priority-2 load path)
    #   user scope:    ~/.openclaw/skills/ (OpenClaw managed dir, priority-4)
    # At project scope the output is identical to the agent-skills target;
    # the --global user path is the distinguishing capability.
    # Ref: https://docs.openclaw.ai/tools/skills
    "openclaw": TargetProfile(
        capability=TARGET_CAPABILITIES["openclaw"],
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
        user_root_dir=".openclaw",
    ),
    # OpenCode -- at user scope, ~/.config/opencode/ supports skills, agents,
    # and commands.  OpenCode has no hooks concept, so "hooks" is excluded.
    "opencode": TargetProfile(
        capability=TARGET_CAPABILITIES["opencode"],
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
        user_primitive_overrides={
            "skills": PrimitiveMapping("skills", "/SKILL.md", "skill_standard"),
        },
        include_scoped_in_user_root_context=True,
    ),
    # Windsurf/Cascade (now Devin Desktop) -- .windsurf/ is the workspace
    # config directory.
    # Rules are markdown files with trigger/globs frontmatter under .windsurf/rules/.
    # Skills converge onto the cross-tool .agents/skills/<name>/SKILL.md path
    # (deploy_root=".agents"), matching copilot/cursor/codex/gemini/opencode;
    # Devin's own docs also use .agents/skills/.  Rules, workflows, and hooks
    # stay under .windsurf/.
    # Cascade auto-invokes skills when the description frontmatter matches the
    # task -- this is the universal invocation mechanism, so windsurf does
    # NOT expose a separate ``agents`` primitive.  Package authors who want
    # their content to deploy to windsurf must declare it under
    # ``.apm/skills/<name>/SKILL.md`` (not under ``.apm/agents/``).
    # Workflows (~= commands) are markdown files under .windsurf/workflows/.
    # Hooks are configured in .windsurf/hooks.json.
    # At user scope, ~/.codeium/windsurf/ is used.  Global rules use a single
    # file (~/.codeium/windsurf/memories/global_rules.md) with a different
    # format, so "instructions" is excluded from user scope.
    # MCP config: ~/.codeium/windsurf/mcp_config.json (mcpServers JSON format).
    # Ref: https://docs.windsurf.com/windsurf/cascade/memories
    # Ref: https://docs.windsurf.com/windsurf/cascade/mcp
    "windsurf": TargetProfile(
        capability=TARGET_CAPABILITIES["windsurf"],
        root_dir=".windsurf",
        primitives={
            "instructions": PrimitiveMapping(
                "rules",
                ".md",
                "windsurf_rules",
                output_compare=True,
            ),
            "skills": PrimitiveMapping(
                "skills",
                "/SKILL.md",
                "skill_standard",
                deploy_root=".agents",
            ),
            "commands": PrimitiveMapping("workflows", ".md", "windsurf_workflow"),
            "hooks": PrimitiveMapping("", "hooks.json", "windsurf_hooks"),
        },
        auto_create=False,
        detect_by_dir=True,
        user_supported="partial",
        user_root_dir=".codeium/windsurf",
        unsupported_user_primitives=("instructions",),
        pack_prefixes=(".windsurf/", ".agents/"),
        hooks_config_display=".windsurf/hooks.json",
    ),
}


_CLIENT_TYPES = {
    "antigravity": ("apm_cli.adapters.client.antigravity", "AntigravityClientAdapter"),
    "copilot": ("apm_cli.adapters.client.copilot", "CopilotClientAdapter"),
    "vscode": ("apm_cli.adapters.client.vscode", "VSCodeClientAdapter"),
    "codex": ("apm_cli.adapters.client.codex", "CodexClientAdapter"),
    "cursor": ("apm_cli.adapters.client.cursor", "CursorClientAdapter"),
    "gemini": ("apm_cli.adapters.client.gemini", "GeminiClientAdapter"),
    "intellij": ("apm_cli.adapters.client.intellij", "IntelliJClientAdapter"),
    "kiro": ("apm_cli.adapters.client.kiro", "KiroClientAdapter"),
    "opencode": ("apm_cli.adapters.client.opencode", "OpenCodeClientAdapter"),
    "windsurf": ("apm_cli.adapters.client.windsurf", "WindsurfClientAdapter"),
    "claude": ("apm_cli.adapters.client.claude", "ClaudeClientAdapter"),
    "hermes": ("apm_cli.adapters.client.hermes", "HermesClientAdapter"),
}

_SIGNALS = (
    ("claude", "dir", ".claude"),
    ("claude", "file", "CLAUDE.md"),
    ("cursor", "dir", ".cursor"),
    ("cursor", "file", ".cursorrules"),
    ("copilot", "file", ".github/copilot-instructions.md"),
    ("copilot", "file", ".github/mcp.json"),
    ("copilot", "dir", ".github/instructions"),
    ("copilot", "dir", ".github/agents"),
    ("copilot", "dir", ".github/prompts"),
    ("copilot", "dir", ".github/hooks"),
    ("codex", "dir", ".codex"),
    ("gemini", "dir", ".gemini"),
    ("gemini", "file", "GEMINI.md"),
    ("grok-build", "dir", ".grok"),
    ("opencode", "dir", ".opencode"),
    ("windsurf", "dir", ".windsurf"),
    ("kiro", "dir", ".kiro"),
)

_DISPLAY_SIGNALS = {
    "claude": "CLAUDE.md",
    "copilot": ".github/copilot-instructions.md",
    "cursor": ".cursor/",
    "codex": ".codex/",
    "gemini": "GEMINI.md",
    "grok-build": ".grok/",
    "opencode": ".opencode/",
    "windsurf": ".windsurf/",
    "kiro": ".kiro/",
}


class _LazyIntegrators(Mapping):
    def __init__(self, primitive_names):
        self._primitive_names = tuple(primitive_names)

    def __getitem__(self, primitive):
        if primitive not in self._primitive_names:
            raise KeyError(primitive)
        from apm_cli.integration.dispatch import get_dispatch_table

        return get_dispatch_table()[primitive].integrator_class

    def __iter__(self):
        return iter(self._primitive_names)

    def __len__(self):
        return len(self._primitive_names)


def get_builtin_adapters() -> tuple[HarnessAdapter, ...]:
    """Return built-ins without importing client or integrator implementations."""
    clients = {name: _client_constructor(*details) for name, details in _CLIENT_TYPES.items()}
    adapters = []
    for name, capability in TARGET_CAPABILITIES.items():
        profile = KNOWN_TARGETS.get(name)
        runtimes = {
            runtime: clients[runtime] for runtime in capability.runtimes if runtime in clients
        }
        if name in clients:
            runtimes.setdefault(name, clients[name])
        signals = tuple(signal[1:] for signal in _SIGNALS if signal[0] == name)
        integrations = _LazyIntegrators(profile.primitives) if profile is not None else {}
        adapters.append(
            HarnessAdapter(
                capability=capability,
                profile=profile,
                mcp_clients=runtimes,
                signals=signals,
                display_signal=_DISPLAY_SIGNALS.get(name),
                integrators=integrations,
            )
        )
    return tuple(adapters)
