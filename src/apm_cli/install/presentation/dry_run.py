"""Dry-run presentation for ``apm install --dry-run``.

Extracted from ``commands/install.py`` (P2.S5) -- faithful copy of the
original block that lived at lines 525-581.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apm_cli.core.remediation import DRY_RUN_INCOMPLETE, kept_note
from apm_cli.install.dry_run_plan import ProspectiveInstallPlan

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.deps.lockfile import LockFile


def render_and_exit(
    *,
    logger: InstallLogger,
    plan: ProspectiveInstallPlan,
    update: bool,
    apm_dir: Path,
    local_cleanup_pending: Callable[[LockFile], bool] | None = None,
    project_root: Path | None = None,
    no_policy: bool = False,
    scope: object = None,
) -> None:
    """Render the dry-run preview to the user.

    The caller is responsible for ``return``-ing after this function
    completes -- this function does NOT exit or return early on its own.
    """
    from apm_cli.deps.lockfile import LockFile, LockfileConflictError, get_lockfile_path

    logger.progress("Dry run mode - showing what would change:")

    selected_apm_dependencies = plan.selected_apm_dependencies if plan.should_install_apm else ()

    if selected_apm_dependencies:
        logger.progress(f"APM dependencies ({plan.apm_dependency_count}):")
        for dep in selected_apm_dependencies:
            action = (
                "update"
                if update or dep.get_identity() in plan.updated_apm_identities
                else "install"
            )
            logger.progress(f"  - {dep.to_display_reference()} -> {action}")

    if plan.selected_mcp_dependencies:
        logger.progress(f"MCP dependencies ({plan.mcp_dependency_count}):")
        for dep in plan.selected_mcp_dependencies:
            logger.progress(f"  - {dep}")

    if plan.selected_lsp_dependencies:
        logger.progress(f"LSP servers to configure ({plan.lsp_dependency_count}):")
        for dep in plan.selected_lsp_dependencies:
            logger.progress(f"  - {dep}")

    if (
        not selected_apm_dependencies
        and not plan.selected_mcp_dependencies
        and not plan.selected_lsp_dependencies
    ):
        if (
            plan.should_install_apm
            and not plan.should_install_mcp
            and (plan.mcp_dependencies or plan.lsp_dependencies)
        ):
            logger.progress(
                "No APM dependencies selected by --only=apm. "
                "Drop --only to preview MCP/LSP dependencies."
            )
        elif not plan.should_install_apm and plan.all_apm_dependencies:
            logger.progress(
                "No MCP/LSP dependencies selected by --only=mcp. "
                "Drop --only to preview APM dependencies."
            )
        else:
            logger.progress("No dependencies found in apm.yml")

    for error in plan.dependency_declaration_errors:
        # Shown without --verbose: install refuses these whatever is cached.
        logger.warning(f"apm install would refuse: {error}")
    if plan.dependency_preview_errors:
        logger.dry_run_notice(
            "Dependency preview incomplete; showing only locally known dependencies. "
            f"Orphan removals are not previewed. {DRY_RUN_INCOMPLETE.sentence()}"
        )
        for error in plan.dependency_preview_errors:
            if error not in plan.dependency_declaration_errors:
                logger.verbose_detail(error)

    try:
        _dryrun_lock = LockFile.read(get_lockfile_path(apm_dir))
    except LockfileConflictError as exc:
        logger.warning(str(exc))
        _dryrun_lock = None
    except Exception:
        _dryrun_lock = None
    if _dryrun_lock and plan.should_install_apm and not plan.dependency_preview_errors:
        _orphan_preview = _preview_orphan_files(_dryrun_lock, plan, logger)
        if _orphan_preview:
            logger.progress(
                f"Files that would be removed (packages no longer in apm.yml): "
                f"{len(_orphan_preview)}"
            )
            for _orphan in sorted(_orphan_preview)[:10]:
                logger.progress(f"  - {_orphan}")
            if len(_orphan_preview) > 10:
                logger.progress(f"  ... and {len(_orphan_preview) - 10} more")

    if _dryrun_lock and plan.should_install_apm:
        from apm_cli.install.frozen_retirement import preview_executable_retirements

        # Deny wins (#3944, #4043): install would remove these packages' deployed
        # executables; the preview removes nothing.
        retirements = preview_executable_retirements(
            _dryrun_lock, apm_dir, no_policy=no_policy, scope=scope
        )
        if retirements:
            logger.progress(
                f"Executables that would be retired (decision changed since apm.lock.yaml): "
                f"{len(retirements)}"
            )
            for source, surfaces in retirements:
                logger.progress(f"  - {source}")
                for surface in surfaces:
                    logger.progress(f"      {surface}")

    if _dryrun_lock and plan.should_install_mcp and project_root is not None:
        from apm_cli.install.mcp.retired_destinations import preview_retired_mcp_moves

        retired_preview = preview_retired_mcp_moves(
            _dryrun_lock,
            project_root,
            declared=plan.selected_mcp_dependencies,
            target_runtimes=plan.mcp_target_runtimes,
        )
        for retired, names in retired_preview.moves.items():
            logger.progress(f"MCP servers that would move from {retired} to .mcp.json:")
            for name in names:
                logger.progress(f"  - {name}")
        for retired, names in retired_preview.removals.items():
            logger.progress(f"MCP servers that would be removed from {retired}:")
            for name in names:
                logger.progress(f"  - {name}")

    cleanup_pending = False
    if not selected_apm_dependencies and local_cleanup_pending and _dryrun_lock:
        try:
            cleanup_pending = local_cleanup_pending(_dryrun_lock)
        except (KeyError, TypeError, ValueError) as exc:  # malformed ledger state
            logger.verbose_detail(f"Local cleanup preview skipped: {exc}")
    if selected_apm_dependencies or cleanup_pending:
        logger.dry_run_notice(
            "Stale-file cleanup (renames within a package or in the project's "
            ".apm/ content) is not previewed -- it requires running "
            "integration. Run without --dry-run to apply."
        )


def _preview_orphan_files(lockfile: LockFile, plan: ProspectiveInstallPlan, logger) -> set[str]:
    """Return the deployed files the real install's orphan decision would remove.

    The same owner decision the real install acts on, with the retained
    packages' recorded claims standing in for fresh integration output. A
    targeted install never retires unrelated packages, so it previews none.
    """
    from apm_cli.install.orphans import DeploymentClaims, OrphanOperation, decide_orphan_removal

    if plan.only_packages:
        return set()
    decision = decide_orphan_removal(
        operation=OrphanOperation.INSTALL,
        roots=(),
        candidate_universe=lockfile,
        installed_paths=None,
        resolved_keys=None,
        intended=plan.intended_dependency_keys,
        materialized=None,
        reachability=None,
        deployment_claims=DeploymentClaims(live_files=None, retained={}),
    )
    for path in sorted({path for files in decision.claimed_files.values() for path in files}):
        logger.verbose_detail(kept_note(path, "still deployed by a remaining package"))
    return {path for files in decision.removed_files.values() for path in files}
