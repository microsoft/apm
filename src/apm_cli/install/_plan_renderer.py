"""Plan rendering helpers for human-readable install plan output.

Extracted from install/plan to keep that module under 400 lines.
Defines action constant strings, symbol mapping, and text renderers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apm_cli.utils.console import STATUS_SYMBOLS

if TYPE_CHECKING:
    from apm_cli.install.plan import PlanEntry, UpdatePlan

_ACTION_UPDATE = "update"
_ACTION_ADD = "add"
_ACTION_REMOVE = "remove"
_ACTION_UNCHANGED = "unchanged"

_ACTION_SYMBOLS = {
    _ACTION_UPDATE: STATUS_SYMBOLS["update"],
    _ACTION_ADD: STATUS_SYMBOLS["check"],
    _ACTION_REMOVE: STATUS_SYMBOLS["remove"],
    _ACTION_UNCHANGED: STATUS_SYMBOLS["equal"],
}


def _format_ref_change(entry: PlanEntry) -> str:
    """Render the ref/commit transition for a single :class:`PlanEntry`.

    Examples (ASCII only):
        ``main (abc1234 -> def5678)``
        ``v1.0.0 (new)``
        ``main (abc1234 removed)``
    """
    if entry.action == _ACTION_ADD:
        ref = entry.new_resolved_ref or "-"
        commit = entry.short_new_commit
        return f"{ref} ({commit}, new)"
    if entry.action == _ACTION_REMOVE:
        ref = entry.old_resolved_ref or "-"
        commit = entry.short_old_commit
        return f"{ref} ({commit}, removed)"
    old_ref = entry.old_resolved_ref or "-"
    new_ref = entry.new_resolved_ref or old_ref
    ref_part = old_ref if old_ref == new_ref else f"{old_ref} -> {new_ref}"
    return f"{ref_part} ({entry.short_old_commit} -> {entry.short_new_commit})"


def _render_summary_legend(counts: dict, verbose: bool) -> list:
    """Return 0, 1, or 2 lines summarising action counts and legend symbols."""
    _LABELED = [(_ACTION_UPDATE, "updated"), (_ACTION_ADD, "added"), (_ACTION_REMOVE, "removed")]
    _active = [(act, label) for act, label in _LABELED if counts.get(act)] + (
        [(_ACTION_UNCHANGED, "unchanged")] if verbose and counts.get(_ACTION_UNCHANGED) else []
    )
    if not _active:
        return []
    return [
        "  " + ", ".join(f"{counts[act]} {label}" for act, label in _active),
        "  " + "  ".join(f"{_ACTION_SYMBOLS[act]} {label}" for act, label in _active),
    ]


def render_plan_text(plan: UpdatePlan, *, verbose: bool = False) -> str:
    """Render an :class:`UpdatePlan` as ASCII terminal output.

    Empty string when ``plan.has_changes`` is False (callers display
    a higher-level "already up to date" message instead).

    Bracket-status symbols:
        ``[~]`` updated
        ``[+]`` added
        ``[-]`` removed
        ``[=]`` unchanged (verbose only)

    The output never includes a trailing newline; callers append one if
    needed.
    """
    if not plan.has_changes and not verbose:
        return ""

    lines: list[str] = ["[i] Update plan for apm.yml", ""]
    for entry in plan.entries:
        if entry.action == _ACTION_UNCHANGED and not verbose:
            continue
        symbol = _ACTION_SYMBOLS.get(entry.action, "[?]")
        lines.append(f"  {symbol} {entry.display_name}")
        lines.append(f"      ref: {_format_ref_change(entry)}")
        if entry.deployed_files:
            preview = ", ".join(entry.deployed_files[:3])
            if len(entry.deployed_files) > 3:
                preview += f", +{len(entry.deployed_files) - 3} more"
            lines.append(f"      files: {preview}")
        lines.append("")

    lines.extend(_render_summary_legend(plan.summary_counts, verbose))
    return "\n".join(lines).rstrip()
