"""Dry-run presentation for ``apm install --dry-run``.

Extracted from ``commands/install.py`` (P2.S5) -- faithful copy of the
original block that lived at lines 525-581.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    scope: object = None,
    fallback_allow_executables: dict[str, dict[str, bool]] | None = None,
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
            "Orphan removals are not previewed. Run 'apm install' without --dry-run "
            "to resolve dependencies."
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

    if (
        _dryrun_lock
        and plan.should_install_apm
        and not plan.dependency_preview_errors
        and scope is not None
    ):
        _preview_gate_retirements(_dryrun_lock, scope, fallback_allow_executables, logger)

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
        logger.verbose_detail(f"Kept {path}: still deployed by a remaining package")
    return {path for files in decision.removed_files.values() for path in files}


_GATE_REASONS = {"denied": "denied", "gated_pending_approval": "not approved in executables.allow"}


def _preview_gate_retirements(
    lockfile: LockFile,
    scope: object,
    fallback_allow_executables: dict[str, dict[str, bool]] | None,
    logger,
) -> None:
    """Name the executables the real install's gate would retire now.

    Reads the same per-row decision ``apm update`` uses to re-run the gate
    (:func:`~apm_cli.install.exec_gate.scope_stale_exec_rows`), so a deny,
    a revoked approval or an org policy change is previewed, not only applied.
    """
    from apm_cli.core.scope import get_modules_dir
    from apm_cli.install.exec_gate import scope_stale_exec_rows

    modules_dir = get_modules_dir(scope)
    rows = scope_stale_exec_rows(
        lockfile, modules_dir, scope, fallback_allow_executables=fallback_allow_executables
    )
    for row in rows:
        if row.unknown:
            logger.verbose_detail(
                "Executables gate not previewed: org policy or a package could not be read."
            )
            continue
        if not row.retired_types:
            continue
        reason = _GATE_REASONS.get(row.status or "", row.status)
        label = row.dependency.to_dependency_ref().to_display_reference()
        logger.progress(f"Executables that would be retired ({reason}): {label}")
        for exec_type in row.retired_types:
            items = _gated_items(lockfile, row.dependency, exec_type, modules_dir)
            logger.progress(f"  - {exec_type}: {', '.join(items)}" if items else f"  - {exec_type}")


def _gated_items(lockfile: LockFile, dependency, exec_type: str, modules_dir: Path) -> list[str]:
    """The deployed items of *exec_type* the lock records for *dependency*.

    MCP and LSP servers are matched through the provenance token their
    integrators record for the declaring package.
    """
    if exec_type == "mcp":
        from apm_cli.integration.mcp_config_view import mcp_package_owner

        install_path = dependency.to_dependency_ref().get_install_path(modules_dir)
        owner = mcp_package_owner(dependency.name, package_dir=install_path)
        return _provenance_names(lockfile.mcp_config_provenance, owner)
    if exec_type == "lsp":
        from apm_cli.install.lsp.integration import package_lsp_owner

        owner = package_lsp_owner(dependency.get_unique_key())
        return _provenance_names(lockfile.lsp_config_provenance, owner)
    if exec_type not in ("hooks", "bin"):
        return []
    return sorted(path for path in dependency.deployed_files if exec_type in path.split("/")[:-1])


def _provenance_names(provenance: dict, owner: str) -> list[str]:
    return sorted(
        name
        for name, owners in provenance.items()
        if owner in ([owners] if isinstance(owners, str) else owners)
    )
