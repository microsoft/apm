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

Implementation note
-------------------
The bulk of this module lives in three private siblings:

* :mod:`apm_cli.core._target_types`   – type aliases and constants
* :mod:`apm_cli.core._target_compile` – compile predicates and legacy detect
* :mod:`apm_cli.core._target_resolve` – v2 resolution algorithm (#1154)

All public symbols remain importable from *this* module so that existing
``from apm_cli.core.target_detection import …`` statements need no changes.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import click

# ---------------------------------------------------------------------------
# Re-exports from private sibling modules
# ---------------------------------------------------------------------------
from apm_cli.core._target_compile import (
    detect_target,
    get_target_description,
    should_compile_agents_md,
    should_compile_claude_md,
    should_compile_copilot_instructions_md,
    should_compile_gemini_md,
)
from apm_cli.core._target_resolve import (
    SIGNAL_WHITELIST,
    ResolvedTargets,
    Signal,
    detect_signals,
    expand_all_targets,
    format_provenance,
    resolve_targets,
)
from apm_cli.core._target_types import (
    ALL_CANONICAL_TARGETS,
    CANONICAL_DEPLOY_DIRS,
    CANONICAL_SIGNAL,
    CANONICAL_TARGETS_ORDERED,
    EXPERIMENTAL_TARGETS,
    EXPLICIT_ONLY_TARGETS,
    REASON_NO_TARGET_FOLDER,
    TARGET_ALIASES,
    VALID_TARGET_VALUES,
    CompileFamily,
    CompileTargetType,
    TargetType,
    UserTargetType,
    normalize_target_list,
)

# ---------------------------------------------------------------------------
# Deprecation state (kept here: mutated by parse_target_field below)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# parse_target_field — shared validator for --target CLI flag and apm.yml
# ---------------------------------------------------------------------------


def _collect_raw_tokens(value: str | list, source_path) -> list[str]:
    """Parse value into a list of raw lowercased tokens, raising ValueError on bad input."""
    if isinstance(value, str):
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
    return raw_parts


def _dedupe_targets_preserving_order(raw_parts: list[str]) -> list[str] | str:
    """Resolve aliases and deduplicate, preserving order. Returns str if single result."""
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
    raw_parts = _collect_raw_tokens(value, source_path)

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
    return _dedupe_targets_preserving_order(raw_parts)


def _target_error(message: str, source_path: Path | None) -> str:
    """Format a target validation error, naming the source file when known."""
    if source_path is not None:
        return f"Invalid 'target' in {source_path}: {message}"
    return f"Invalid target: {message}"


# ---------------------------------------------------------------------------
# Click parameter type for --target (comma-separated multi-target support)
# ---------------------------------------------------------------------------


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
