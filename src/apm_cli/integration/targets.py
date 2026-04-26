"""Target profiles for multi-tool integration.

Each target tool (Copilot, Claude, Cursor, ...) describes where APM
primitives should land.  Adding a new target means adding an entry to
``KNOWN_TARGETS`` -- no new classes required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union


@dataclass(frozen=True)
class PrimitiveMapping:
    """Where a single primitive type is deployed in a target tool."""

    subdir: str
    """Subdirectory under the target root (e.g. ``"rules"``, ``"agents"``)."""

    extension: str
    """File extension or suffix for deployed files
    (e.g. ``".mdc"``, ``".agent.md"``)."""

    format_id: str
    """Opaque tag used by integrators to select the right
    content transformer (e.g. ``"cursor_rules"``)."""

    deploy_root: Optional[str] = None
    """Override *root_dir* for this primitive only.

    When set, integrators use ``deploy_root`` instead of
    ``target.root_dir`` to compute the deploy directory.
    For example, Codex skills deploy to ``.agents/`` (cross-tool
    directory) rather than ``.codex/``.  Default ``None`` preserves
    existing behavior for all other targets.
    """


@dataclass(frozen=True)
class TargetProfile:
    """Capabilities and layout of a single target tool."""

    name: str
    """Short unique identifier (``"copilot"``, ``"claude"``, ``"cursor"``)."""

    root_dir: str
    """Top-level directory in the workspace (e.g. ``".github"``)."""

    primitives: Dict[str, PrimitiveMapping]
    """Mapping from APM primitive name -> deployment spec.

    Only primitives listed here are deployed to this target.
    """

    auto_create: bool = True
    """Create *root_dir* if it does not exist (used during fallback or
    explicit ``--target`` selection)."""

    detect_by_dir: bool = True
    """If ``True``, only deploy when *root_dir* already exists."""

    # -- user-scope metadata --------------------------------------------------

    user_supported: Union[bool, str] = False
    """Whether this target supports user-scope (``~/``) deployment.

    * ``True``  -- fully supported (all primitives work at user scope).
    * ``"partial"`` -- some primitives work, others do not.
    * ``False`` -- not supported at user scope.
    """

    user_root_dir: Optional[str] = None
    """Override for *root_dir* at user scope.

    When ``None`` the normal *root_dir* is used at both project and user
    scope.  Set this when the tool reads from a different directory at
    user level (e.g. Copilot CLI uses ``~/.copilot/`` instead of
    ``~/.github/``).
    """

    unsupported_user_primitives: Tuple[str, ...] = ()
    """Primitives that are **not** available at user scope even when the
    target itself is partially supported (e.g. Copilot CLI cannot deploy
    prompts at user scope)."""

    @property
    def prefix(self) -> str:
        """Return the path prefix for this target (e.g. ``".github/"``).

        Used by ``validate_deploy_path`` and ``partition_managed_files``.
        """
        return f"{self.root_dir}/"

    def supports(self, primitive: str) -> bool:
        """Return ``True`` if this target accepts *primitive*."""
        return primitive in self.primitives

    def effective_root(self, user_scope: bool = False) -> str:
        """Return the root directory for the given scope.

        At user scope, returns *user_root_dir* when set, otherwise
        falls back to the standard *root_dir*.
        """
        if user_scope and self.user_root_dir:
            return self.user_root_dir
        return self.root_dir

    def supports_at_user_scope(self, primitive: str) -> bool:
        """Return ``True`` if *primitive* can be deployed at user scope."""
        if not self.user_supported:
            return False
        if primitive in self.unsupported_user_primitives:
            return False
        return primitive in self.primitives

    def for_scope(self, user_scope: bool = False) -> "TargetProfile | None":
        """Return a scope-resolved copy of this profile.

        When *user_scope* is ``False``, returns ``self`` unchanged.

        When *user_scope* is ``True``:
        - Returns ``None`` if this target does not support user scope.
        - Otherwise returns a frozen copy with ``root_dir`` set to
          ``user_root_dir`` (or left unchanged when ``user_root_dir``
          is ``None``) and ``primitives`` filtered to exclude entries
          listed in ``unsupported_user_primitives``.

        This is the **single place** where scope resolution happens.
        All downstream code reads ``target.root_dir`` directly.
        """
        if not user_scope:
            return self
        if not self.user_supported:
            return None

        from dataclasses import replace

        new_root = self.user_root_dir or self.root_dir
        if self.unsupported_user_primitives:
            filtered = {
                k: v for k, v in self.primitives.items()
                if k not in self.unsupported_user_primitives
            }
        else:
            filtered = self.primitives

        return replace(self, root_dir=new_root, primitives=filtered)


# ------------------------------------------------------------------
# Known targets
# ------------------------------------------------------------------

KNOWN_TARGETS: Dict[str, TargetProfile] = {
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
            "prompts": PrimitiveMapping(
                "prompts", ".prompt.md", "github_prompt"
            ),
            "agents": PrimitiveMapping(
                "agents", ".agent.md", "github_agent"
            ),
            "skills": PrimitiveMapping(
                "skills", "/SKILL.md", "skill_standard"
            ),
            "hooks": PrimitiveMapping(
                "hooks", ".json", "github_hooks"
            ),
        },
        auto_create=True,
        detect_by_dir=True,
        user_supported="partial",
        user_root_dir=".copilot",
        unsupported_user_primitives=("prompts", "instructions"),
    ),
    # Claude Code -- ~/.claude/ is the documented user-level config directory.
    # All primitives are supported at user scope.
    # Ref: https://docs.anthropic.com/en/docs/claude-code/settings
    # Instructions deploy to .claude/rules/*.md with paths: frontmatter.
    # Ref: https://code.claude.com/docs/en/memory#organize-rules-with-claude%2Frules%2F
    "claude": TargetProfile(
        name="claude",
        root_dir=".claude",
        primitives={
            "instructions": PrimitiveMapping(
                "rules", ".md", "claude_rules"
            ),
            "agents": PrimitiveMapping(
                "agents", ".md", "claude_agent"
            ),
            "commands": PrimitiveMapping(
                "commands", ".md", "claude_command"
            ),
            "skills": PrimitiveMapping(
                "skills", "/SKILL.md", "skill_standard"
            ),
            "hooks": PrimitiveMapping(
                "hooks", ".json", "claude_hooks"
            ),
        },
        auto_create=False,
        detect_by_dir=True,
        user_supported=True,
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
                "rules", ".mdc", "cursor_rules"
            ),
            "agents": PrimitiveMapping(
                "agents", ".md", "cursor_agent"
            ),
            "skills": PrimitiveMapping(
                "skills", "/SKILL.md", "skill_standard"
            ),
            "hooks": PrimitiveMapping(
                "hooks", ".json", "cursor_hooks"
            ),
        },
        auto_create=False,
        detect_by_dir=True,
        user_supported="partial",
        user_root_dir=".cursor",
        unsupported_user_primitives=("instructions",),
    ),
    # OpenCode -- at user scope, ~/.config/opencode/ supports skills, agents,
    # and commands.  OpenCode has no hooks concept, so "hooks" is excluded.
    "opencode": TargetProfile(
        name="opencode",
        root_dir=".opencode",
        primitives={
            "agents": PrimitiveMapping(
                "agents", ".md", "opencode_agent"
            ),
            "commands": PrimitiveMapping(
                "commands", ".md", "opencode_command"
            ),
            "skills": PrimitiveMapping(
                "skills", "/SKILL.md", "skill_standard"
            ),
        },
        auto_create=False,
        detect_by_dir=True,
        user_supported="partial",
        user_root_dir=".config/opencode",
        unsupported_user_primitives=("hooks",),
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
            "commands": PrimitiveMapping(
                "commands", ".toml", "gemini_command"
            ),
            "skills": PrimitiveMapping(
                "skills", "/SKILL.md", "skill_standard"
            ),
            "hooks": PrimitiveMapping(
                "hooks", ".json", "gemini_hooks"
            ),
        },
        auto_create=False,
        detect_by_dir=True,
        user_supported=True,
        user_root_dir=".gemini",
    ),
    # Codex CLI: skills use the cross-tool .agents/ dir (agent skills standard),
    # agents are TOML under .codex/agents/, hooks merge into .codex/hooks.json.
    # Instructions are compile-only (AGENTS.md) -- not installed.
    "codex": TargetProfile(
        name="codex",
        root_dir=".codex",
        primitives={
            "agents": PrimitiveMapping(
                "agents", ".toml", "codex_agent"
            ),
            "skills": PrimitiveMapping(
                "skills", "/SKILL.md", "skill_standard",
                deploy_root=".agents",
            ),
            "hooks": PrimitiveMapping(
                "", "hooks.json", "codex_hooks"
            ),
        },
        auto_create=False,
        detect_by_dir=True,
    ),
}


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
    explicit_target: "Optional[Union[str, List[str]]]" = None,
) -> list:
    """Return ``TargetProfile`` instances for user-scope deployment.

    Mirrors ``active_targets()`` but operates against ``~/`` and filters
    out targets that do not support user scope.

    Resolution order:

    1. **Explicit target** (``--target``): returns the matching profile
       if it supports user scope.  ``"all"`` returns every user-capable
       target.  A list of names returns all matching user-capable profiles.
    2. **Directory detection**: profiles whose ``effective_root(user_scope=True)``
       directory exists under ``~/``.
    3. **Fallback**: ``[copilot]`` -- same default as project scope.
    """
    from pathlib import Path

    home = Path.home()

    # --- explicit target ---
    if explicit_target:
        if isinstance(explicit_target, list):
            profiles: list = []
            seen: set = set()
            for t in explicit_target:
                canonical = t
                if canonical in ("copilot", "vscode", "agents"):
                    canonical = "copilot"
                if canonical == "all":
                    return [
                        p for p in KNOWN_TARGETS.values()
                        if p.user_supported
                    ]
                profile = KNOWN_TARGETS.get(canonical)
                if profile and profile.user_supported and profile.name not in seen:
                    seen.add(profile.name)
                    profiles.append(profile)
            return profiles if profiles else []

        # single string (existing behavior)
        canonical = explicit_target
        if canonical in ("copilot", "vscode", "agents"):
            canonical = "copilot"
        if canonical == "all":
            return [
                p for p in KNOWN_TARGETS.values()
                if p.user_supported
            ]
        profile = KNOWN_TARGETS.get(canonical)
        if profile and profile.user_supported:
            return [profile]
        return []

    # --- auto-detect by directory presence at ~/ ---
    detected = [
        p for p in KNOWN_TARGETS.values()
        if p.user_supported and (home / p.effective_root(user_scope=True)).is_dir()
    ]
    if detected:
        return detected

    # --- fallback: copilot is the universal default ---
    return [KNOWN_TARGETS["copilot"]]


def active_targets(
    project_root,
    explicit_target: "Optional[Union[str, List[str]]]" = None,
) -> list:
    """Return the list of ``TargetProfile`` instances that should be
    deployed into *project_root*.

    Resolution order:

    1. **Explicit target** (``--target`` flag or ``apm.yml target:``):
       returns only the matching profile(s).  ``"all"`` returns every
       known target.  A list of names returns all matching profiles.
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
        if isinstance(explicit_target, list):
            profiles: list = []
            seen: set = set()
            for t in explicit_target:
                canonical = t
                if canonical in ("copilot", "vscode", "agents"):
                    canonical = "copilot"
                if canonical == "all":
                    return list(KNOWN_TARGETS.values())
                profile = KNOWN_TARGETS.get(canonical)
                if profile and profile.name not in seen:
                    seen.add(profile.name)
                    profiles.append(profile)
            return profiles if profiles else [KNOWN_TARGETS["copilot"]]

        # single string (existing behavior)
        canonical = explicit_target
        if canonical in ("copilot", "vscode", "agents"):
            canonical = "copilot"
        if canonical == "all":
            return list(KNOWN_TARGETS.values())
        profile = KNOWN_TARGETS.get(canonical)
        return [profile] if profile else []

    # --- auto-detect by directory presence ---
    detected = [
        p for p in KNOWN_TARGETS.values()
        if (root / p.root_dir).is_dir()
    ]
    if detected:
        return detected

    # --- fallback: copilot is the universal default ---
    return [KNOWN_TARGETS["copilot"]]


def resolve_targets(
    project_root,
    user_scope: bool = False,
    explicit_target: "Optional[Union[str, List[str]]]" = None,
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
