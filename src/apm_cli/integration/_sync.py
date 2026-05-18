"""File-removal sync helpers for BaseIntegrator.

Extracted from :mod:`apm_cli.integration.base_integrator` to keep
that module under the 500-line ceiling while preserving all behaviour.

``BaseIntegrator`` re-exports these as thin ``@staticmethod`` wrappers
so all call-sites remain unchanged.
"""

from __future__ import annotations

from pathlib import Path

from apm_cli.utils.console import _rich_warning

from ._opts import SyncRemoveOpts


def cleanup_empty_parents(
    deleted_paths: list[Path],
    stop_at: Path,
) -> None:
    """Remove empty parent directories in a single bottom-up pass.

    Collects all parent directories of *deleted_paths*, sorts by
    depth descending, and removes each if empty -- O(H+D) syscalls
    instead of the per-file O(HxD) approach.

    Args:
        deleted_paths: Paths that were deleted (files or dirs).
        stop_at: Do not remove this directory or any ancestor.
    """
    if not deleted_paths:
        return
    stop_resolved = stop_at.resolve()
    # Collect unique parents (skip stop_at itself)
    candidates: set = set()
    for p in deleted_paths:
        parent = p.parent
        while parent != stop_at and parent.resolve() != stop_resolved:
            candidates.add(parent)
            parent = parent.parent
    # Sort deepest-first for safe bottom-up removal
    for d in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
        try:
            if d.exists() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


def _warn_cowork_orphans(count: int, logger, warn_fn) -> None:
    """Emit the cowork orphan warning once."""
    orphan_msg = (
        f"Cowork: skipping {count} orphaned lockfile "
        f"{'entry' if count == 1 else 'entries'}"
        " -- OneDrive path not detected.\n"
        "Run: apm config set copilot-cowork-skills-dir <path>  "
        "(or set APM_COPILOT_COWORK_SKILLS_DIR)\n"
        "to clean up these entries on the next install/uninstall."
    )
    if logger:
        logger.warning(orphan_msg, symbol="warning")
    else:
        warn_fn(orphan_msg, symbol="warning")


def _resolve_managed_target(
    rel_path: str,
    project_root: Path,
    resolved_opts: SyncRemoveOpts,
    cowork_root_state: dict[str, object],
) -> tuple[Path | None, bool]:
    """Resolve a managed-file entry to a filesystem path."""
    from apm_cli.integration.base_integrator import BaseIntegrator
    from apm_cli.integration.copilot_cowork_paths import COWORK_URI_SCHEME

    if not BaseIntegrator.validate_deploy_path(
        rel_path, project_root, targets=resolved_opts.targets
    ):
        return None, False
    if not rel_path.startswith(COWORK_URI_SCHEME):
        return project_root / rel_path, False
    try:
        if not cowork_root_state["resolved"]:
            from apm_cli.integration.copilot_cowork_paths import resolve_copilot_cowork_skills_dir

            cowork_root_state["root"] = resolve_copilot_cowork_skills_dir()
            cowork_root_state["resolved"] = True
        if cowork_root_state["root"] is None:
            return None, True
        from apm_cli.integration.copilot_cowork_paths import from_lockfile_path

        return from_lockfile_path(rel_path, cowork_root_state["root"]), False
    except Exception:
        return None, False


def _delete_existing_target(target: Path, stats: dict[str, int]) -> None:
    """Delete *target* if it exists and update stats."""
    if not target.exists():
        return
    try:
        target.unlink()
        stats["files_removed"] += 1
    except Exception:
        stats["errors"] += 1


def sync_remove_files(
    project_root: Path,
    managed_files: set[str] | None,
    prefix: str,
    opts: SyncRemoveOpts | None = None,
) -> dict[str, int]:
    """Remove APM-managed files matching *prefix* from *managed_files*.

    Falls back to a legacy glob when *managed_files* is ``None``.

    Args:
        project_root: Workspace root.
        managed_files: Set of workspace-relative paths.
        prefix: Only process paths that start with this prefix
                (e.g. ``".github/prompts/"``).
        legacy_glob_dir: Directory to glob inside for the legacy fallback.
        legacy_glob_pattern: Glob pattern for legacy fallback
                             (e.g. ``"*-apm.prompt.md"``).
        targets: Optional target profiles for path validation.
                 Passed through to ``validate_deploy_path()`` so
                 user-scope prefixes are recognised.
        logger: Optional logger for diagnostic messages.
        _warn_fn: Optional callable used instead of ``_rich_warning`` when
                  ``logger`` is ``None``.  Injected by
                  ``BaseIntegrator.sync_remove_files`` so that test patches
                  on ``apm_cli.integration.base_integrator._rich_warning``
                  propagate correctly into this helper.

    Returns:
        ``{"files_removed": int, "errors": int}``
    """
    resolved_opts = opts or SyncRemoveOpts()
    warn_fn = resolved_opts.warn_fn or _rich_warning
    logger = resolved_opts.logger

    stats: dict[str, int] = {"files_removed": 0, "errors": 0}

    if managed_files is not None:
        cowork_root_state: dict[str, object] = {"resolved": False, "root": None}
        cowork_orphans_skipped = 0

        for rel_path in managed_files:
            if not rel_path.startswith(prefix):
                continue
            target, skipped_orphan = _resolve_managed_target(
                rel_path,
                project_root,
                resolved_opts,
                cowork_root_state,
            )
            if skipped_orphan:
                cowork_orphans_skipped += 1
                continue
            if target is None:
                continue
            _delete_existing_target(target, stats)

        if cowork_orphans_skipped > 0:
            _warn_cowork_orphans(cowork_orphans_skipped, logger, warn_fn)
    elif (
        resolved_opts.legacy_glob_dir
        and resolved_opts.legacy_glob_pattern
        and resolved_opts.legacy_glob_dir.exists()
    ):
        for f in resolved_opts.legacy_glob_dir.glob(resolved_opts.legacy_glob_pattern):
            try:
                f.unlink()
                stats["files_removed"] += 1
            except Exception:
                stats["errors"] += 1

    return stats
