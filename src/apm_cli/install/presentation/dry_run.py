"""Dry-run presentation for ``apm install --dry-run``.

Extracted from ``commands/install.py`` (P2.S5) -- faithful copy of the
original block that lived at lines 525-581.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from apm_cli.install.dry_run_plan import ProspectiveInstallPlan

if TYPE_CHECKING:
    from pathlib import Path

    from apm_cli.commands.install import InstallLogger


def render_and_exit(
    *,
    logger: InstallLogger,
    plan: ProspectiveInstallPlan | None = None,
    should_install_apm: bool | None = None,
    apm_deps: Sequence[Any] = (),
    mcp_deps: Sequence[Any] = (),
    dev_apm_deps: Sequence[Any] = (),
    should_install_mcp: bool | None = None,
    update: bool,
    only_packages: Sequence[str] | None = None,
    apm_dir: Path,
) -> None:
    """Render the dry-run preview to the user.

    The caller is responsible for ``return``-ing after this function
    completes -- this function does NOT exit or return early on its own.
    """
    if plan is None:
        plan = ProspectiveInstallPlan(
            apm_dependencies=tuple(apm_deps),
            dev_apm_dependencies=tuple(dev_apm_deps),
            mcp_dependencies=tuple(mcp_deps),
            should_install_apm=bool(should_install_apm),
            should_install_mcp=bool(should_install_mcp),
            only_packages=tuple(only_packages) if only_packages is not None else None,
        )

    from apm_cli.deps.lockfile import LockFile, get_lockfile_path
    from apm_cli.drift import detect_orphans

    logger.progress("Dry run mode - showing what would be installed:")

    if plan.should_install_apm and plan.apm_dependencies:
        logger.progress(f"APM dependencies ({plan.apm_dependency_count}):")
        for dep in plan.apm_dependencies:
            action = "update" if update else "install"
            logger.progress(f"  - {dep.repo_url}#{dep.reference or 'main'} -> {action}")

    if plan.should_install_mcp and plan.mcp_dependencies:
        logger.progress(f"MCP dependencies ({plan.mcp_dependency_count}):")
        for dep in plan.mcp_dependencies:
            logger.progress(f"  - {dep}")

    if not plan.all_apm_dependencies and not plan.mcp_dependencies:
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

    if plan.all_apm_dependencies:
        logger.dry_run_notice(
            "Per-package stale-file cleanup (renames within a package) is "
            "not previewed -- it requires running integration. Run without "
            "--dry-run to apply."
        )

    logger.success("Dry run complete - no changes made")
