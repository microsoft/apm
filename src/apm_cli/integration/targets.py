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

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple, Union


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

    user_root_resolver: Optional[Callable[[], Optional["Path"]]] = None
    """Optional callable that resolves the deploy root at runtime.

    When set, ``for_scope(user_scope=True)`` calls this resolver instead of
    using a static ``user_root_dir``.  If the resolver returns ``None``
    the target is unavailable in the current environment (same semantics
    as ``user_supported=False``).

    The callable must be hashable by reference (plain function or
    staticmethod) so ``frozen=True`` is preserved.
    """

    resolved_deploy_root: Optional["Path"] = None
    """Absolute deploy root populated by ``for_scope()`` when
    ``user_root_resolver`` returns a concrete ``Path``.

    Downstream code uses ``deploy_path()`` to route filesystem I/O
    through this root instead of ``project_root / root_dir``.
    """

    requires_flag: Optional[str] = None
    """When set, the target is only returned by ``active_targets`` /
    ``active_targets_user_scope`` / ``resolve_targets`` when the named
    experimental flag is enabled.  The target entry is always visible
    in ``KNOWN_TARGETS`` for tooling introspection.
    """

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

    def deploy_path(self, project_root: "Path", *parts: str) -> "Path":
        """Return the filesystem path for deployment.

        When ``resolved_deploy_root`` is set (dynamic-root targets like
        cowork), the path is rooted there.  Otherwise falls back to the
        standard ``project_root / root_dir`` pattern.

        Args:
            project_root: Workspace or home directory root.
            *parts: Additional path segments (e.g. ``"skills"``, ``"my-skill"``).
        """
        if self.resolved_deploy_root is not None:
            return self.resolved_deploy_root.joinpath(*parts) if parts else self.resolved_deploy_root
        base = project_root / self.root_dir
        return base.joinpath(*parts) if parts else base

    def for_scope(self, user_scope: bool = False) -> "TargetProfile | None":
        """Return a scope-resolved copy of this profile.

        When *user_scope* is ``False``, returns ``self`` unchanged.

        When *user_scope* is ``True``:
        - If ``user_root_resolver`` is set, calls it.  Returns ``None``
          when the resolver returns ``None`` (target unavailable).
          Otherwise returns a copy with ``resolved_deploy_root`` set and
          primitives filtered for user scope.
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

        from dataclasses import replace

        # --- dynamic-root resolver path (cowork) ---
        if self.user_root_resolver is not None:
            resolved_root = self.user_root_resolver()
            if resolved_root is None:
                return None
            if self.unsupported_user_primitives:
                filtered = {
                    k: v for k, v in self.primitives.items()
                    if k not in self.unsupported_user_primitives
                }
            else:
                filtered = self.primitives
            return replace(
                self,
                primitives=filtered,
                resolved_deploy_root=resolved_root,
            )

        if not self.user_supported:
            return None

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
        user_supported="partial",
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
                "skills", "/SKILL.md", "skill_standard",
            ),
        },
        auto_create=False,
        detect_by_dir=False,
        user_supported=True,
        user_root_resolver=lambda: _resolve_copilot_cowork_root(),
        requires_flag="copilot_cowork",
    ),
}


def _resolve_copilot_cowork_root() -> "Path | None":
    """Thin wrapper around ``copilot_cowork_paths.resolve_copilot_cowork_skills_dir()``.

    Used as the ``user_root_resolver`` callable for the cowork target.
    Exceptions propagate to the caller (``for_scope`` / install pipeline).
    """
    from apm_cli.integration.copilot_cowork_paths import resolve_copilot_cowork_skills_dir
    return resolve_copilot_cowork_skills_dir()


def _is_flag_enabled(flag_name: str) -> bool:
    """Check whether an experimental flag is enabled.

    Lazy import to avoid config I/O at module load time.
    """
    from apm_cli.core.experimental import is_enabled
    return is_enabled(flag_name)


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
    explicit_target: "Optional[Union[str, List[str]]]" = None,
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
            canonical = "copilot" if t in ("copilot", "vscode", "agents") else t
            if canonical == "all":
                return [
                    p for p in KNOWN_TARGETS.values()
                    if p.user_supported and _flag_gated(p)
                ]
            profile = KNOWN_TARGETS.get(canonical)
            if profile and profile.user_supported and _flag_gated(profile) and profile.name not in seen:
                seen.add(profile.name)
                profiles.append(profile)
        return profiles

    # --- auto-detect by directory presence at ~/ ---
    # Targets with detect_by_dir=False (cowork) are never auto-detected.
    detected = [
        p for p in KNOWN_TARGETS.values()
        if p.user_supported and p.detect_by_dir
        and _flag_gated(p)
        and (home / p.effective_root(user_scope=True)).is_dir()
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
            canonical = "copilot" if t in ("copilot", "vscode", "agents") else t
            if canonical == "all":
                # Return all targets regardless of flag gating.
                # The project-scope gate in phases/targets.py and
                # for_scope() handle user-observable blocking.
                return list(KNOWN_TARGETS.values())
            profile = KNOWN_TARGETS.get(canonical)
            if profile and _flag_gated(profile) and profile.name not in seen:
                seen.add(profile.name)
                profiles.append(profile)
        return profiles

    # --- auto-detect by directory presence ---
    # Targets with detect_by_dir=False (cowork) are never auto-detected.
    detected = [
        p for p in KNOWN_TARGETS.values()
        if p.detect_by_dir and _flag_gated(p)
        and (root / p.root_dir).is_dir()
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
