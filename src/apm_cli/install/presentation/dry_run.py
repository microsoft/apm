"""Dry-run presentation for ``apm install --dry-run``.

Extracted from ``commands/install.py`` (P2.S5) -- faithful copy of the
original block that lived at lines 525-581.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apm_cli.install.dry_run_plan import ProspectiveInstallPlan

if TYPE_CHECKING:
    from pathlib import Path

    from apm_cli.commands.install import InstallLogger


def render_and_exit(
    *,
    logger: InstallLogger,
    plan: ProspectiveInstallPlan,
    update: bool,
    apm_dir: Path,
) -> None:
    """Render the dry-run preview to the user.

    The caller is responsible for ``return``-ing after this function
    completes -- this function does NOT exit or return early on its own.
    """
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path
    from apm_cli.drift import detect_orphans

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

    # Orphan preview: lockfile + manifest difference -- no integration
    # required, accurate to compute.
    try:
        _dryrun_lock = LockFile.read(get_lockfile_path(apm_dir))
    except Exception:
        _dryrun_lock = None
    if _dryrun_lock:
        _orphan_preview = detect_orphans(
            _dryrun_lock,
            plan.intended_dependency_keys,
            only_packages=list(plan.only_packages) if plan.only_packages is not None else None,
        )
        if _orphan_preview:
            logger.progress(
                f"Files that would be removed (packages no longer in apm.yml): "
                f"{len(_orphan_preview)}"
            )
            for _orphan in sorted(_orphan_preview)[:10]:
                logger.progress(f"  - {_orphan}")
            if len(_orphan_preview) > 10:
                logger.progress(f"  ... and {len(_orphan_preview) - 10} more")

    if selected_apm_dependencies:
        logger.dry_run_notice(
            "Per-package stale-file cleanup (renames within a package) is "
            "not previewed -- it requires running integration. Run without "
            "--dry-run to apply."
        )
