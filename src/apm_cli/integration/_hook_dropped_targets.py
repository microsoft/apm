"""Private implementation for HookIntegrator.reconcile_dropped_targets.

Split out of ``hook_integrator.py`` purely to respect that file's line-count
budget (CI file-length guardrail) -- this module is NOT a second
target-cleanup authority. ``HookIntegrator.reconcile_dropped_targets`` is
still the sole public entry point every caller uses; everything here is a
private implementation detail invoked only from that one method, and all
JSON mutation still runs through ``HookIntegrator._clean_apm_entries_from_json``,
the unchanged, single canonical primitive for merge-hook JSON writes.

## Why this exists

``HookIntegrator.sync_integration``/``reconcile_after_removal`` are
intentionally scoped (#2250) to the SAME resolved ``targets`` the rebuild
loop uses -- correct for that bug, but it permanently walls prune/uninstall
off from a target DROPPED from ``targets:`` entirely, since the rebuild loop
never touches it again. ``reconcile_dropped_targets`` is the canonical owner
of that gap: called once from ``manifest_reconcile.
reconcile_dropped_merge_hook_targets`` with the complement of the caller's
active/declared target union (see that module for the "allowed = active |
declared" rule).

Names not registered in ``_MERGE_HOOK_TARGETS`` (e.g. ``copilot``, which uses
per-file, not merged, hook deployment already tracked/cleaned via
``deployed_files``) are silently skipped -- only true merge-hook targets are
acted on here.

## Fail-closed partial-state handling

This is stricter than ``_clean_apm_entries_from_json``'s best-effort posture
for its existing prune/uninstall callers (unchanged): a sidecar-only orphan
(native JSON absent, ownership sidecar remains -- a case that primitive's own
early-return never reaches) is validated as parseable before being unlinked,
and any malformed native/sidecar JSON is left byte-identical with an
actionable warning logged, never silently swallowed.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import hook_integrator as _hi


def reconcile_dropped_targets(
    project_root: Path,
    dropped_target_names: list[str] | set[str],
    *,
    user_scope: bool = False,
) -> dict[str, int]:
    """Clean merge-hook JSON entries/sidecars for targets no longer declared."""
    from .targets import KNOWN_TARGETS

    stats: dict[str, int] = {"files_removed": 0, "errors": 0}
    for name in dropped_target_names:
        config = _hi._MERGE_HOOK_TARGETS.get(name)
        if config is None:
            continue
        profile = KNOWN_TARGETS.get(name)
        if profile is None:
            continue
        scoped = profile.for_scope(user_scope=user_scope)
        if scoped is None:
            continue
        target_dir = scoped.deploy_path(project_root)
        json_path = target_dir / config.config_filename
        sidecar_path = target_dir / _hi._APM_HOOKS_SIDECAR

        if not json_path.exists():
            if sidecar_path.exists():
                _reconcile_sidecar_only_orphan(sidecar_path, stats)
            continue

        errors_before = stats["errors"]
        _hi.HookIntegrator._clean_apm_entries_from_json(
            json_path,
            stats,
            container=config.event_container_key,
            sidecar_path=sidecar_path,
        )
        if stats["errors"] > errors_before:
            _hi._log.warning(
                "Dropped-target hook config %s is unreadable/malformed; "
                "left unmodified for manual review.",
                json_path,
            )
    return stats


def _reconcile_sidecar_only_orphan(sidecar_path: Path, stats: dict[str, int]) -> None:
    """Remove an ownership sidecar whose native hook JSON is already gone.

    ``_clean_apm_entries_from_json`` never reaches this state (it returns
    immediately when the native file is absent), so a sidecar left behind
    lingers forever in every existing caller. Validates the sidecar parses
    before deleting it -- malformed JSON fails closed (left in place,
    counted as an error, warned).
    """
    try:
        with open(sidecar_path, encoding="utf-8") as f:
            json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        stats["errors"] += 1
        _hi._log.warning(
            "Orphaned hook ownership sidecar %s is unreadable/malformed (%s); "
            "leaving it in place for manual review.",
            sidecar_path,
            exc,
        )
        return
    try:
        sidecar_path.unlink()
        stats["files_removed"] += 1
    except OSError as exc:
        stats["errors"] += 1
        _hi._log.warning("Failed to remove orphaned hook sidecar %s: %s", sidecar_path, exc)
