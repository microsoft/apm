"""Target detection for auto-selecting compilation and integration targets.

This module implements the auto-detection pattern for determining which agent
targets (Copilot, Claude, Cursor, OpenCode, Codex, Gemini) should be used
based on existing project structure and configuration.

Detection priority (highest to lowest):
1. Explicit --target flag (always wins)
2. apm.yml target setting (top-level field)
3. Auto-detect from existing folders:
   - .github/ only -> copilot (internal: "vscode")
   - .claude/ only -> claude
   - .cursor/ only -> cursor
   - .opencode/ only -> opencode
   - .codex/ only -> codex
   - .gemini/ only -> gemini
   - Multiple target folders -> all
   - None exist -> minimal (AGENTS.md only, no folder integration)

"copilot" is the recommended user-facing target name. "vscode" and "agents"
are accepted as aliases and map to the same internal value.
"""

import warnings
from pathlib import Path
from typing import List, Literal, Optional, Tuple, Union  # noqa: F401, UP035

import click


class AgentsTargetDeprecationWarning(DeprecationWarning):
    """Raised when the legacy ``--target agents`` alias is used.

    Scoped subclass so that :mod:`apm_cli.cli` can suppress *only* this
    deprecation (keeping all other ``DeprecationWarning`` s visible).
    """


# Module-level flag: set by :func:`parse_target_field` when the raw input
# contains the ``"agents"`` token, BEFORE alias resolution collapses it.
# Consumed by downstream phases (e.g. ``phases/targets.py``) that need to
# emit a formatted logger warning.  Single-threaded CLI; reset at the top
# of each :func:`parse_target_field` call.
_agents_alias_detected: bool = False


def agents_alias_was_detected() -> bool:
    """Return *True* if the most recent ``parse_target_field()`` saw ``'agents'``."""
    return _agents_alias_detected


# Valid target values (internal canonical form)
TargetType = Literal[
    "vscode",
    "claude",
    "cursor",
    "opencode",
    "codex",
    "gemini",
    "windsurf",
    "agent-skills",
    "all",
    "minimal",
]

# Compiler families used inside a multi-target frozenset. Narrower than
# TargetType because the families are produced by _resolve_compile_target()
# (in the compile CLI) from CLI-validated target names.
#
# Family semantics:
#   "agents"  -> AGENTS.md is generated (any of copilot/vscode/agents/cursor/
#                opencode/codex was requested)
#   "vscode"  -> .github/copilot-instructions.md is generated (only when
#                copilot/vscode/agents was specifically requested -- NOT for
#                cursor/opencode/codex which use their own native config files)
#   "claude"  -> CLAUDE.md is generated
#   "gemini"  -> GEMINI.md is generated
CompileFamily = Literal["agents", "vscode", "claude", "gemini"]

# Compile target: either a single TargetType string or a frozenset of compiler
# families ({"agents", "claude", "gemini"}) for multi-target lists.
CompileTargetType = Union[TargetType, frozenset[CompileFamily]]  # noqa: UP007

# Detection reason returned by detect_target() when no integration folder is
# present. Exported as a constant so consumers can compare with equality
# instead of substring matching.
REASON_NO_TARGET_FOLDER = "no target folder found"

# User-facing target values (includes aliases accepted by CLI)
UserTargetType = Literal[
    "copilot",
    "vscode",
    "agents",
    "claude",
    "cursor",
    "opencode",
    "codex",
    "gemini",
    "windsurf",
    "agent-skills",
    "all",
    "minimal",
]


def detect_target(  # noqa: PLR0911
    project_root: Path,
    explicit_target: str | None = None,
    config_target: str | None = None,
) -> tuple[TargetType, str]:
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
        elif explicit_target == "gemini":
            return "gemini", "explicit --target flag"
        elif explicit_target == "windsurf":
            return "windsurf", "explicit --target flag"
        elif explicit_target == "agent-skills":
            return "agent-skills", "explicit --target flag"
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
        elif config_target == "gemini":
            return "gemini", "apm.yml target"
        elif config_target == "windsurf":
            return "windsurf", "apm.yml target"
        elif config_target == "agent-skills":
            return "agent-skills", "apm.yml target"
        elif config_target == "all":
            return "all", "apm.yml target"

    # Priority 3: Auto-detect from existing folders
    github_exists = (project_root / ".github").exists()
    claude_exists = (project_root / ".claude").exists()
    cursor_exists = (project_root / ".cursor").is_dir()
    opencode_exists = (project_root / ".opencode").is_dir()
    codex_exists = (project_root / ".codex").is_dir()
    gemini_exists = (project_root / ".gemini").is_dir()
    windsurf_exists = (project_root / ".windsurf").is_dir()
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
    if gemini_exists:
        detected.append(".gemini/")
    if windsurf_exists:
        detected.append(".windsurf/")

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
    elif gemini_exists:
        return "gemini", "detected .gemini/ folder"
    elif windsurf_exists:
        return "windsurf", "detected .windsurf/ folder"
    else:
        return "minimal", REASON_NO_TARGET_FOLDER


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
    return target in ("vscode", "opencode", "codex", "gemini", "windsurf", "all", "minimal")


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
        "vscode": "AGENTS.md + .github/copilot-instructions.md + .github/prompts/ + .github/agents/",
        "claude": "CLAUDE.md + .claude/commands/ + .claude/agents/ + .claude/skills/",
        "cursor": ".cursor/agents/ + .cursor/skills/ + .cursor/rules/",
        "opencode": "AGENTS.md + .opencode/agents/ + .opencode/commands/ + .opencode/skills/",
        "codex": "AGENTS.md + .agents/skills/ + .codex/agents/ + .codex/hooks.json",
        "gemini": "GEMINI.md + .gemini/commands/ + .gemini/skills/ + .gemini/settings.json (MCP/hooks)",
        "windsurf": "AGENTS.md + .windsurf/rules/ + .windsurf/skills/ + .windsurf/workflows/ + .windsurf/hooks.json",
        "agent-skills": ".agents/skills/ only (cross-client shared skills -- no agents, hooks, or commands)",
        "all": "AGENTS.md + CLAUDE.md + GEMINI.md + .github/copilot-instructions.md + .github/ + .claude/ + .cursor/ + .opencode/ + .codex/ + .gemini/ + .windsurf/ + .agents/",
        "minimal": "AGENTS.md only (create .github/, .claude/, or .gemini/ for full integration)",
    }
    return descriptions.get(normalized, "unknown target")


# ---------------------------------------------------------------------------
# Multi-target helpers (used by active_targets() in the integration layer)
# ---------------------------------------------------------------------------

#: The complete set of real (non-pseudo) canonical targets.
#: "minimal" is intentionally excluded -- it is a fallback pseudo-target.
ALL_CANONICAL_TARGETS = frozenset(
    {"vscode", "claude", "cursor", "opencode", "codex", "gemini", "windsurf"}
)

#: Targets that the parser must accept but that are gated at runtime by
#: ``is_enabled()`` in ``core/experimental.py`` and ``_flag_gated()`` in
#: ``integration/targets.py``.  They are NOT included in the
#: ``parse_target_arg("all")`` expansion -- explicit opt-in only.
EXPERIMENTAL_TARGETS: frozenset[str] = frozenset({"copilot-cowork"})

#: Stable targets excluded from "all" expansion (cross-client deploy
#: locations). Unlike EXPERIMENTAL_TARGETS, these are GA -- they just do
#: not represent a single client tool.
EXPLICIT_ONLY_TARGETS: frozenset[str] = frozenset({"agent-skills"})

#: Alias mapping: user-facing name -> canonical internal name.
TARGET_ALIASES: dict[str, str] = {
    "copilot": "vscode",
    "agents": "vscode",
    "vscode": "vscode",
}


def normalize_target_list(
    value: str | list[str] | None,
) -> list[str] | None:
    """Normalize a user-provided target value to a list of canonical names.

    Handles:
    - ``None`` -> ``None`` (auto-detect)
    - ``"claude"`` -> ``["claude"]``
    - ``"copilot"`` -> ``["vscode"]``  (alias resolution)
    - ``"all"`` -> ``["claude", "codex", "cursor", "gemini", "opencode", "vscode"]``
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

    raw: list[str] = [value] if isinstance(value, str) else list(value)

    # "all" anywhere in the input means "every target" -- expand to the
    # full sorted list of canonical targets.
    if "all" in raw:
        return sorted(ALL_CANONICAL_TARGETS)

    seen: set[str] = set()
    result: list[str] = []
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
    ALL_CANONICAL_TARGETS
    | EXPERIMENTAL_TARGETS
    | EXPLICIT_ONLY_TARGETS
    | frozenset(TARGET_ALIASES)
    | frozenset({"all"})
)


def parse_target_field(
    value: str | list[str] | None,
    *,
    source_path: Path | None = None,
) -> str | list[str] | None:
    """Parse, validate, and normalize a target value from any entry point.

    Single source of truth for the ``target`` field, shared by the
    ``--target`` CLI flag (via :class:`TargetParamType`) and ``apm.yml``'s
    top-level ``target:`` (via :func:`APMPackage.from_apm_yml`).  The
    output may differ from the input in case (lowercased), order
    (preserved but deduplicated), and shape (single-element multi-token
    inputs collapse to ``str``).  Aliases are resolved for multi-token
    input only; see the *Returns* section below for the exact rules.

    Accepted input shapes:

    * ``None`` -> ``None`` (auto-detect at consumption time -- this is the
      "field absent" path; an apm.yml without ``target:`` lands here).
    * Single token (``"claude"``) -> the same lowercased token as ``str``.
      Aliases are NOT resolved for solo input -- ``"copilot"`` returns
      ``"copilot"`` (not the canonical ``"vscode"``) to match the
      long-standing CLI contract; downstream consumers handle the alias
      set explicitly.
    * CSV string (``"claude,copilot"``) -> deduplicated ``List[str]`` with
      aliases resolved to canonical names. Collapses to a bare ``str`` if
      after dedup only one canonical token remains.
    * List input (``["claude", "copilot"]``) goes through the same path as
      the CSV form -- single-element lists collapse to ``str``.
    * Literal ``"all"`` -> ``"all"`` (exclusive; cannot be combined).

    Args:
        value: The raw value -- ``str``, ``List[str]``, or ``None``.
        source_path: Optional path to the apm.yml that produced ``value``.
            When supplied, ValueError messages name the file so users can
            jump to it directly.

    Returns:
        ``None`` for unset, a ``str`` for a single token (or ``"all"``),
        or a deduplicated ``List[str]`` for multi-target input.

    Raises:
        ValueError: When the value is an empty / whitespace-only / commas-only
            string, an empty list, a non-string non-list type, contains a
            token that is not in :data:`VALID_TARGET_VALUES`, or mixes
            ``"all"`` with other targets.  An empty *string* is treated as
            user error (the "field absent" path is ``None``, supplied by
            the YAML loader for a missing key).
    """
    if value is None:
        return None

    global _agents_alias_detected
    _agents_alias_detected = False

    # ---- collect raw tokens ----
    if isinstance(value, str):
        # Empty / whitespace-only / comma-only strings are user error -- a
        # missing field comes through as ``None`` from the YAML loader, so
        # an empty *string* means the user typed something invalid.
        raw_parts = [v.strip().lower() for v in value.split(",") if v.strip()]
    elif isinstance(value, list):
        raw_parts = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(
                    _target_error(
                        f"each entry must be a string, got {type(item).__name__}",
                        source_path,
                    )
                )
            if item.strip():
                raw_parts.append(item.strip().lower())
    else:
        raise ValueError(
            _target_error(
                f"expected string or list of strings, got {type(value).__name__}",
                source_path,
            )
        )

    if not raw_parts:
        raise ValueError(_target_error("target value must not be empty", source_path))

    # ---- validate every token ----
    for p in raw_parts:
        if p not in VALID_TARGET_VALUES:
            raise ValueError(
                _target_error(
                    f"'{p}' is not a valid target. "
                    f"Choose from: {', '.join(sorted(VALID_TARGET_VALUES))}",
                    source_path,
                )
            )

    # ---- deprecation warning for legacy "agents" alias (once per call) ----
    if "agents" in raw_parts:
        _agents_alias_detected = True
        warnings.warn(
            "'--target agents' is deprecated -- it maps to 'copilot' (.github/), "
            "not '.agents/'. Use '--target copilot' or '--target agent-skills' "
            "(.agents/skills/). Removal in v1.0.",
            AgentsTargetDeprecationWarning,
            stacklevel=2,
        )

    # ---- "all" handling ----
    if "all" in raw_parts:
        non_all_tokens = {t for t in raw_parts if t != "all"}
        if non_all_tokens - EXPLICIT_ONLY_TARGETS:
            raise ValueError(
                _target_error(
                    "'all' cannot be combined with other targets",
                    source_path,
                )
            )
        if not non_all_tokens:
            return "all"
        # "all" + explicit-only tokens (e.g. "all,agent-skills"):
        # expand "all" to canonical targets and append the explicit-only ones.
        expanded = sorted(ALL_CANONICAL_TARGETS) + sorted(non_all_tokens)
        return expanded

    # Single-token input is returned as-is (no alias resolution).  This
    # preserves the long-standing CLI contract where ``--target copilot``
    # yields ``"copilot"`` rather than the canonical ``"vscode"``; every
    # downstream consumer (active_targets, agents_compiler,
    # _CROSS_TARGET_MAPS, _get_target_prefixes) already accepts both alias
    # spellings, so resolving here would be a visible behaviour change
    # with zero functional benefit and would break the CLI test suite
    # (~10 ``test_single_*`` cases).  This is the one asymmetry #820's
    # "shared normalization" intentionally leaves in place; collapsing it
    # is an independent decision tracked separately from this fix.
    if len(raw_parts) == 1:
        return raw_parts[0]

    # Multi-token: resolve aliases + dedupe, preserving input order.
    seen: set[str] = set()
    result: list[str] = []
    for p in raw_parts:
        canonical = TARGET_ALIASES.get(p, p)
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)

    if len(result) == 1:
        return result[0]
    return result


def _target_error(message: str, source_path: Path | None) -> str:
    """Format a target validation error, naming the source file when known."""
    if source_path is not None:
        return f"Invalid 'target' in {source_path}: {message}"
    return f"Invalid target: {message}"


class TargetParamType(click.ParamType):
    """Click parameter type accepting comma-separated target values.

    Delegates to :func:`parse_target_field`, which is the shared validator
    used by ``apm.yml``'s ``target:`` field as well -- so ``--target X`` and
    ``target: X`` always resolve identically and reject the same inputs.

    Examples::

        -t claude             -> "claude"
        -t claude,copilot     -> ["claude", "vscode"]
        -t all                -> "all"
        -t copilot,vscode     -> ["vscode"]  (deduped aliases)
    """

    name = "target"

    def convert(
        self,
        value: str | list[str] | None,
        param: click.Parameter | None,
        ctx: click.Context | None,
    ) -> str | list[str] | None:
        try:
            return parse_target_field(value)
        except ValueError as e:
            # Use the v2 three-section error renderer for unknown targets
            # so that CLI, apm.yml, and auto-detect all share the same
            # error format (#1154).
            from apm_cli.core.apm_yml import CANONICAL_TARGETS
            from apm_cli.core.errors import UnknownTargetError, render_unknown_target_error

            err_msg = str(e)
            if "is not a valid target" in err_msg:
                target_name = value if isinstance(value, str) else ",".join(value or [])
                rendered = render_unknown_target_error(target_name, sorted(CANONICAL_TARGETS))
                raise UnknownTargetError(rendered) from None
            # Click idiom: route validation errors through self.fail so the
            # user sees a clean "Invalid value for '--target': ..." message
            # rather than a Python traceback.
            self.fail(err_msg, param, ctx)


# ---------------------------------------------------------------------------
# v2 Resolution algorithm (#1154)
# ---------------------------------------------------------------------------

from dataclasses import dataclass  # noqa: E402


@dataclass(frozen=True)
class Signal:
    """A filesystem marker that indicates a harness is present."""

    target: str  # canonical target name: 'claude', 'copilot', etc.
    source: str  # human-readable: 'CLAUDE.md', '.github/copilot-instructions.md'


@dataclass(frozen=True)
class ResolvedTargets:
    """Result of target resolution -- the single source of truth."""

    targets: list[str]  # sorted canonical target names
    source: str  # '--target flag' | 'apm.yml' | 'auto-detect from <csv>'
    auto_create: bool  # always True after resolution (three-guard collapse)


# Detection signal whitelist.
# (target, check_type, path)
# check_type: 'dir' = is_dir(), 'file' = is_file()
SIGNAL_WHITELIST: list[tuple[str, str, str]] = [
    ("claude", "dir", ".claude"),
    ("claude", "file", "CLAUDE.md"),
    ("cursor", "dir", ".cursor"),
    ("cursor", "file", ".cursorrules"),  # legacy; .cursor/ is canonical
    ("copilot", "file", ".github/copilot-instructions.md"),
    ("codex", "dir", ".codex"),
    ("gemini", "dir", ".gemini"),
    ("gemini", "file", "GEMINI.md"),
    ("opencode", "dir", ".opencode"),
    ("windsurf", "dir", ".windsurf"),
]

# Ordered list of targets for display (excludes agent-skills meta-target).
CANONICAL_TARGETS_ORDERED: list[str] = [
    "claude",
    "copilot",
    "cursor",
    "codex",
    "gemini",
    "opencode",
    "windsurf",
]

# Canonical deploy directories for each target.
CANONICAL_DEPLOY_DIRS: dict[str, str] = {
    "claude": ".claude/",
    "copilot": ".github/",
    "cursor": ".cursor/",
    "codex": ".codex/",
    "gemini": ".gemini/",
    "opencode": ".opencode/",
    "windsurf": ".windsurf/",
}

# The primary (lowest-friction) signal for each target, used in
# "needs <path>" display for inactive targets.
CANONICAL_SIGNAL: dict[str, str] = {
    "claude": "CLAUDE.md",
    "copilot": ".github/copilot-instructions.md",
    "cursor": ".cursor/",
    "codex": ".codex/",
    "gemini": "GEMINI.md",
    "opencode": ".opencode/",
    "windsurf": ".windsurf/",
}


def detect_signals(project_root: Path) -> list[Signal]:
    """Scan project_root for harness markers per SIGNAL_WHITELIST."""
    found: list[Signal] = []
    for target, check_type, rel_path in SIGNAL_WHITELIST:
        full = project_root / rel_path
        if check_type == "dir" and full.is_dir():
            found.append(Signal(target=target, source=rel_path + "/"))
        elif check_type == "file" and full.is_file():
            found.append(Signal(target=target, source=rel_path))
    return found


def _validate_canonical_v2(tokens: list[str]) -> None:
    """Validate every token is a known canonical target."""
    from apm_cli.core.apm_yml import CANONICAL_TARGETS
    from apm_cli.core.errors import UnknownTargetError, render_unknown_target_error

    for token in tokens:
        if token not in CANONICAL_TARGETS:
            raise UnknownTargetError(render_unknown_target_error(token, sorted(CANONICAL_TARGETS)))


def resolve_targets(
    project_root: Path,
    *,
    flag: str | list[str] | None = None,
    yaml_targets: list[str] | None = None,
) -> ResolvedTargets:
    """Resolve effective targets. Raises on error.

    Priority: flag > yaml_targets > auto-detect signals.
    """
    from apm_cli.core.errors import (
        AmbiguousHarnessError,
        NoHarnessError,
        render_ambiguous_error,
        render_no_harness_error,
    )

    # Priority 1: --target flag
    if flag is not None:
        tokens = [flag] if isinstance(flag, str) else list(flag)
        _validate_canonical_v2(tokens)
        return ResolvedTargets(
            targets=sorted(tokens),
            source="--target flag",
            auto_create=True,
        )

    # Priority 2: apm.yml targets (already validated by parse_targets_field)
    if yaml_targets is not None and len(yaml_targets) > 0:
        return ResolvedTargets(
            targets=sorted(yaml_targets),
            source="apm.yml",
            auto_create=True,
        )

    # Priority 3: auto-detect from signals
    signals = detect_signals(project_root)

    # Dedupe by target (e.g. .claude/ + CLAUDE.md both -> 'claude')
    target_set = sorted({s.target for s in signals})
    signal_sources = sorted({s.source for s in signals})

    if len(target_set) == 0:
        raise NoHarnessError(render_no_harness_error(project_root))

    if len(target_set) >= 2:
        raise AmbiguousHarnessError(render_ambiguous_error(project_root, target_set))

    # Exactly 1 target detected
    return ResolvedTargets(
        targets=target_set,
        source=f"auto-detect from {', '.join(signal_sources)}",
        auto_create=True,
    )


def expand_all_targets(
    project_root: Path,
    *,
    yaml_targets: list[str] | None = None,
) -> list[str]:
    """Expand 'all' to (signals union yaml_targets). Raises NoHarnessError if empty."""
    from apm_cli.core.errors import NoHarnessError, render_no_harness_error

    signals = detect_signals(project_root)
    signal_set = {s.target for s in signals}

    yaml_set = set(yaml_targets) if yaml_targets else set()

    combined = sorted(signal_set | yaml_set)

    if not combined:
        raise NoHarnessError(render_no_harness_error(project_root))

    return combined


def format_provenance(resolved: ResolvedTargets) -> str:
    """Format provenance line for CLI output.

    Returns the message portion (without the [i] prefix, since
    _rich_info adds it).

    # Double-space between target list and metadata is intentional and
    # canonical. Test assertions match this exact spacing. Do not collapse.
    """
    targets_csv = ", ".join(resolved.targets)
    return f"Targets: {targets_csv}  (source: {resolved.source})"
