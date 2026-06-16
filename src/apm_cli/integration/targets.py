"""Target profiles for multi-tool integration.

Each target tool (Copilot, Claude, Cursor, ...) describes where APM
primitives should land.  Adding a new target means adding an entry to
``KNOWN_TARGETS`` -- no new classes required.

Resolver invariant (#820): both :func:`active_targets` and
:func:`active_targets_user_scope` accept ``Union[str, List[str]]`` for
``explicit_target`` but treat the two shapes identically -- string inputs
are wrapped to a one-element list before the resolution loop.  Validity
is enforced *upstream* by
:func:`apm_cli.core.target_detection.parse_target_field`, which is the
shared gatekeeper for both ``--target`` and ``apm.yml``'s ``target:``
field.  Unknown tokens never reach these functions in normal flow; if
one does, it falls through the loop without matching any profile and
the result is an empty list (no silent ``[copilot]`` fallback).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .target_profile import RULE_FORMATS as RULE_FORMATS
from .target_profile import PrimitiveMapping, TargetProfile

if TYPE_CHECKING:
    from pathlib import Path

# ------------------------------------------------------------------
# Runtime -> canonical target alias map
# ------------------------------------------------------------------
#
# Several runtime identifiers used at the MCP-config layer (e.g. ``vscode``,
# ``agents``) emit configuration that lands inside the ``copilot`` target's
# tree.  The MCP gate (``mcp_integrator._gate_project_scoped_runtimes``) and
# the explicit-target resolution branch in :func:`active_targets` both need
# to map runtime -> canonical-target name in the same way.  Hold the table
# in one place to prevent the two sites drifting -- a silent drift would
# strip a runtime even when its canonical target is active (the same class
# of bug as #1335).
RUNTIME_TO_CANONICAL_TARGET: dict[str, str] = {
    "vscode": "copilot",
    "agents": "copilot",
    "intellij": "copilot",
}


# ------------------------------------------------------------------
# Known targets
# ------------------------------------------------------------------

KNOWN_TARGETS: dict[str, TargetProfile] = {
    # Copilot (GitHub) -- at user scope, Copilot CLI reads ~/.copilot/
    # instead of ~/.github/.  Instructions are concatenated into
    # ~/.copilot/copilot-instructions.md because Copilot CLI reads only
    # that single file at user scope (not individual *.instructions.md).
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
            "canvas": PrimitiveMapping("extensions", "", "copilot_canvas"),
        },
        auto_create=True,
        detect_by_dir=True,
        user_supported="partial",
        user_root_dir=".copilot",
        user_primitive_overrides={
            "instructions": PrimitiveMapping("", ".md", "copilot_user_instructions"),
        },
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
        compile_family="agents",
        hooks_config_display=".cursor/hooks.json",
    ),
    # Kiro IDE -- spec-driven development editor.
    # Steering files use Kiro frontmatter under .kiro/steering/.
    # Skills use the open Agent Skills SKILL.md layout under .kiro/skills/.
    # Hooks are individual JSON files under .kiro/hooks/.
    # MCP config lives at .kiro/settings/mcp.json and ~/.kiro/settings/mcp.json.
    # Kiro CLI config divergence is intentionally out of scope for this v1 target.
    # Ref: https://kiro.dev/docs/steering/
    # Ref: https://kiro.dev/docs/skills/
    # Ref: https://kiro.dev/docs/hooks/
    "kiro": TargetProfile(
        name="kiro",
        root_dir=".kiro",
        primitives={
            "instructions": PrimitiveMapping(
                "steering",
                ".md",
                "kiro_steering",
                output_compare=True,
            ),
            "skills": PrimitiveMapping("skills", "/SKILL.md", "skill_standard"),
            "hooks": PrimitiveMapping("hooks", ".json", "kiro_hooks"),
        },
        auto_create=False,
        detect_by_dir=True,
        user_supported=True,
        user_root_dir=".kiro",
        compile_family="agents",
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
    # Skills use the standard SKILL.md format under .windsurf/skills/.
    # Cascade auto-invokes them when the description frontmatter matches the
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
        name="windsurf",
        root_dir=".windsurf",
        primitives={
            "instructions": PrimitiveMapping(
                "rules",
                ".md",
                "windsurf_rules",
                output_compare=True,
            ),
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
        name="openclaw",
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
        requires_flag="openclaw",
    ),
    # Hermes agent (Nous Research) -- experimental.  Hermes natively reads
    # the agentskills.io SKILL.md format and the AGENTS.md context-file
    # standard, both already emitted by APM, so skills + instructions reuse
    # the existing skill_standard / compile_family="agents" paths.  Skills
    # land in .agents/skills/ at project scope (read by Hermes via
    # skills.external_dirs) and ~/.hermes/skills/ at user scope.  MCP servers
    # are written separately by HermesClientAdapter to ~/.hermes/config.yaml.
    # $HERMES_HOME overrides the user-scope root (handled in for_scope).
    "hermes": TargetProfile(
        name="hermes",
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
        compile_family="agents",
        requires_flag="hermes",
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
        user_root_resolver=lambda: _resolve_copilot_cowork_root(),
        requires_flag="copilot_cowork",
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
        name="copilot-app",
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
        requires_flag="copilot_app",
        scope_invariant_resolver=True,
    ),
}


def apply_legacy_skill_paths(profiles: list[TargetProfile]) -> list[TargetProfile]:
    """Reset ``deploy_root`` on every ``skills`` primitive to ``None``.

    When ``--legacy-skill-paths`` (or ``APM_LEGACY_SKILL_PATHS=1``) is
    active, this restores pre-convergence per-client routing so skills
    land in ``.github/skills/``, ``.cursor/skills/``, etc. instead of
    the default ``.agents/skills/``.

    Returns a NEW list of (possibly replaced) profiles — the global
    ``KNOWN_TARGETS`` dict is never mutated.
    """
    from dataclasses import replace

    result: list[TargetProfile] = []
    for profile in profiles:
        skills_pm = profile.primitives.get("skills")
        if skills_pm and skills_pm.deploy_root is not None:
            new_pm = PrimitiveMapping(
                subdir=skills_pm.subdir,
                extension=skills_pm.extension,
                format_id=skills_pm.format_id,
                deploy_root=None,
            )
            new_primitives = {**profile.primitives, "skills": new_pm}
            profile = replace(profile, primitives=new_primitives)
        result.append(profile)
    return result


def should_use_legacy_skill_paths() -> bool:
    """Return ``True`` when the ``APM_LEGACY_SKILL_PATHS`` env var is set.

    Recognised truthy values: ``1``, ``true``, ``yes`` (case-insensitive).
    """
    import os

    val = os.environ.get("APM_LEGACY_SKILL_PATHS", "").strip().lower()
    return val in ("1", "true", "yes")


def _resolve_copilot_cowork_root() -> Path | None:
    """Thin wrapper around ``copilot_cowork_paths.resolve_copilot_cowork_skills_dir()``.

    Used as the ``user_root_resolver`` callable for the cowork target.
    Exceptions propagate to the caller (``for_scope`` / install pipeline).
    """
    from apm_cli.integration.copilot_cowork_paths import resolve_copilot_cowork_skills_dir

    return resolve_copilot_cowork_skills_dir()


def _resolve_copilot_app_root() -> Path | None:
    """Thin wrapper around ``copilot_app_db.resolve_copilot_app_root()``.

    Used as the ``user_root_resolver`` callable for the ``copilot-app``
    target.  Returns ``~/.copilot/`` only when the app's SQLite DB is
    present, so the target is invisible on machines without the app
    installed.
    """
    from apm_cli.integration.copilot_app_db import resolve_copilot_app_root

    return resolve_copilot_app_root()


def _is_flag_enabled(flag_name: str) -> bool:
    """Check whether an experimental flag is enabled.

    Lazy import to avoid config I/O at module load time.
    """
    from apm_cli.core.experimental import is_enabled

    return is_enabled(flag_name)


def resolve_hermes_root() -> Path:
    """Resolve the Hermes home directory.

    Honors ``$HERMES_HOME`` (default ``~/.hermes``).  Returns an expanded,
    normalized ``Path`` (``..`` segments collapsed via ``resolve``) so traversal
    in ``$HERMES_HOME`` cannot create unintended intermediate directories during
    ``mkdir(parents=True)``; the directory is not required to exist.  Mirrors the
    normalization in ``TargetProfile.for_scope``.  Used both by the user-scope
    skills deploy path and by ``HermesClientAdapter`` to locate ``config.yaml``
    for MCP writes.
    """
    import os
    from pathlib import Path

    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve(strict=False)
    return (Path.home() / ".hermes").resolve(strict=False)


def _flag_gated(profile: TargetProfile) -> bool:
    """Return ``True`` if *profile* passes its flag gate (or has none)."""
    if profile.requires_flag is None:
        return True
    return _is_flag_enabled(profile.requires_flag)


def get_integration_prefixes(targets=None) -> tuple:
    """Return all known target root prefixes as a tuple.

    Used by ``BaseIntegrator.validate_deploy_path`` so the allow-list
    stays in sync with registered targets.

    When *targets* is provided, prefixes are derived from those
    (already scope-resolved) profiles.  Otherwise falls back to
    ``KNOWN_TARGETS`` for backward compatibility.

    Includes prefixes from ``deploy_root`` overrides (e.g. ``.agents/``
    for Codex skills) so cross-root paths pass security validation.
    """
    source = targets if targets is not None else KNOWN_TARGETS.values()
    prefixes: list[str] = []
    seen: set[str] = set()
    for t in source:
        # Dynamic-root targets (cowork) use cowork:// prefix in lockfile.
        # Check the *capability* (user_root_resolver is not None) rather
        # than the *run-time state* (resolved_deploy_root is not None).
        # The static KNOWN_TARGETS registry always has resolved_deploy_root
        # = None (the resolver fires only on per-install copies created by
        # for_scope()), but cleanup code passes targets=None which falls
        # back to the static registry.  Using the capability flag ensures
        # cowork:// entries pass prefix validation during cleanup/uninstall.
        if t.user_root_resolver is not None:
            from apm_cli.integration.copilot_cowork_paths import COWORK_LOCKFILE_PREFIX

            if COWORK_LOCKFILE_PREFIX not in seen:
                seen.add(COWORK_LOCKFILE_PREFIX)
                prefixes.append(COWORK_LOCKFILE_PREFIX)
            continue
        if t.prefix not in seen:
            seen.add(t.prefix)
            prefixes.append(t.prefix)
        for m in t.primitives.values():
            if m.deploy_root is not None:
                dp = f"{m.deploy_root}/"
                if dp not in seen:
                    seen.add(dp)
                    prefixes.append(dp)
    return tuple(prefixes)


def active_targets_user_scope(
    explicit_target: str | list[str] | None = None,
) -> list:
    """Return ``TargetProfile`` instances for user-scope deployment.

    Mirrors ``active_targets()`` but operates against ``~/`` and filters
    out targets that do not support user scope.

    Resolution order:

    1. **Explicit target** (``--target``): returns the matching profile(s)
       that support user scope.  ``"all"`` returns every user-capable
       target.  Validity is enforced upstream by
       :func:`apm_cli.core.target_detection.parse_target_field`; this
       function does not silently fall back when given unknown tokens.
    2. **Directory detection**: profiles whose ``effective_root(user_scope=True)``
       directory exists under ``~/``.
    3. **Fallback**: ``[copilot]`` -- same default as project scope.
    """
    from pathlib import Path

    home = Path.home()

    # --- explicit target ---
    if explicit_target:
        # See module docstring on the parse_target_field gate-keeping contract.
        raw = [explicit_target] if isinstance(explicit_target, str) else list(explicit_target)
        profiles: list = []
        seen: set = set()
        for t in raw:
            canonical = RUNTIME_TO_CANONICAL_TARGET.get(t, t)
            if canonical == "all":
                from apm_cli.core.target_detection import EXPLICIT_ONLY_TARGETS

                return [
                    p
                    for p in KNOWN_TARGETS.values()
                    if p.user_supported and _flag_gated(p) and p.name not in EXPLICIT_ONLY_TARGETS
                ]
            profile = KNOWN_TARGETS.get(canonical)
            if (
                profile
                and profile.user_supported
                and _flag_gated(profile)
                and profile.name not in seen
            ):
                seen.add(profile.name)
                profiles.append(profile)
        return profiles

    # --- auto-detect by directory presence at ~/ ---
    # Targets with detect_by_dir=False (cowork) are never auto-detected.
    detected = [
        p
        for p in KNOWN_TARGETS.values()
        if p.user_supported
        and p.detect_by_dir
        and _flag_gated(p)
        and (home / p.effective_root(user_scope=True)).is_dir()
    ]
    if detected:
        return detected

    # --- fallback: copilot is the universal default ---
    return [KNOWN_TARGETS["copilot"]]


def active_targets(
    project_root,
    explicit_target: str | list[str] | None = None,
) -> list:
    """Return the list of ``TargetProfile`` instances that should be
    deployed into *project_root*.

    Resolution order:

    1. **Explicit target** (``--target`` flag or ``apm.yml target:``):
       returns the matching profile(s).  ``"all"`` returns every known
       target.  Validity is enforced upstream by
       :func:`apm_cli.core.target_detection.parse_target_field`; unknown
       tokens never reach here, so this branch never silently falls back
       to ``[copilot]``.
    2. **Directory detection**: profiles whose ``root_dir`` already
       exists under *project_root*.
    3. **Fallback**: when nothing is detected, returns ``[copilot]``
       so greenfield projects get a default skills root.

    Args:
        project_root: The workspace root ``Path``.
        explicit_target: Canonical target name, list of canonical names,
            or ``"all"``/``None``.  ``None`` means auto-detect.
    """
    from pathlib import Path

    root = Path(project_root)

    # --- explicit target ---
    if explicit_target:
        # See module docstring on the parse_target_field gate-keeping contract.
        raw = [explicit_target] if isinstance(explicit_target, str) else list(explicit_target)
        profiles: list = []
        seen: set = set()
        for t in raw:
            canonical = RUNTIME_TO_CANONICAL_TARGET.get(t, t)
            if canonical == "all":
                # Exclude explicit-only targets (agent-skills) -- they must
                # be requested individually.
                # Exclude experimental targets (copilot-cowork) -- they must
                # be opted into explicitly via `--target copilot-cowork`,
                # matching the documented contract on EXPERIMENTAL_TARGETS in
                # core/target_detection.py. Including cowork in `all` for
                # project scope hits the unconditional project-scope gate in
                # phases/targets.py and aborts the entire install (#1185 b).
                from apm_cli.core.target_detection import (
                    EXPERIMENTAL_TARGETS,
                    EXPLICIT_ONLY_TARGETS,
                )

                return [
                    p
                    for p in KNOWN_TARGETS.values()
                    if p.name not in EXPLICIT_ONLY_TARGETS and p.name not in EXPERIMENTAL_TARGETS
                ]
            profile = KNOWN_TARGETS.get(canonical)
            if profile and _flag_gated(profile) and profile.name not in seen:
                seen.add(profile.name)
                profiles.append(profile)
        return profiles

    # --- auto-detect by directory presence ---
    # Targets with detect_by_dir=False (cowork) are never auto-detected.
    detected = [
        p
        for p in KNOWN_TARGETS.values()
        if p.detect_by_dir and _flag_gated(p) and (root / p.root_dir).is_dir()
    ]
    if detected:
        return detected

    # --- fallback: copilot is the universal default ---
    return [KNOWN_TARGETS["copilot"]]


def resolve_targets(
    project_root,
    user_scope: bool = False,
    explicit_target: str | list[str] | None = None,
) -> list:
    """Return scope-resolved ``TargetProfile`` instances.

    This is the **single entry point** for obtaining deployment targets.
    It combines target detection (or explicit selection), scope resolution
    (``for_scope``), and primitive filtering into one call.

    Callers receive profiles where ``root_dir`` is already correct for
    the requested scope -- no ``effective_root()`` calls needed.

    Args:
        project_root: Workspace root (``Path.cwd()`` or ``Path.home()``).
        user_scope: When ``True``, resolve for user-level deployment.
        explicit_target: Canonical target name, list of canonical names,
            or ``"all"``.  ``None`` means auto-detect.
    """
    if user_scope:
        raw = active_targets_user_scope(explicit_target)
    else:
        raw = active_targets(project_root, explicit_target)

    resolved = []
    for t in raw:
        scoped = t.for_scope(user_scope=user_scope)
        if scoped is not None:
            resolved.append(scoped)
    return resolved
