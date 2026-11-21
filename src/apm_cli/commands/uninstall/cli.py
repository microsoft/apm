"""APM uninstall command CLI."""

import builtins
import contextlib
import sys
import traceback
from dataclasses import dataclass
from typing import Any

import click

from apm_cli.core.remediation import (
    CLAUDE_PLUGIN_REGISTRATION_STALE,
    COPILOT_PLUGIN_REGISTRATION_STALE,
    INTEGRATIONS_UNSYNCED,
    INTEGRATIONS_UNSYNCED_AFTER,
    LOCKFILE_STATE_UNRECONCILED,
    LOCKFILE_UNMANAGED_PATHS,
    MCP_ENTRY_STALE,
    PROJECT_MANIFEST_MISSING,
)

from ...codex_plugins.client import CodexNativeError
from ...constants import APM_YML_FILENAME
from ...core.command_logger import CommandLogger
from ...deps.lockfile_generator import require_current_cli
from ...deps.lockfile_write_authority import (
    LockfileWriter,
    flush_lockfile_writes,
    lockfile_writer,
    remove_lockfile,
    restore_lockfile_bytes,
)
from ...hook_contract import HookContractError
from ...install.errors import PolicyViolationError
from ...install.locking import serialized_lifecycle
from ...install.phases.finalize import hint_compile_after_removal, snapshot_compile_inputs
from ...models.apm_package import APMPackage
from ...models.dependency.selection import manifest_declarations_of
from ...utils.path_security import PathTraversalError
from .engine import (
    IntegrationCleanupOutcome,
    LocalSurvivorRefreshError,
    MCPUninstallCleanupError,
    _cleanup_staged_local_refreshes,
    _cleanup_stale_mcp,
    _cleanup_transitive_orphans,
    _count_noun,
    _dependency_lifecycle_label,
    _dependency_public_label,
    _dry_run_uninstall,
    _preflight_uninstall_survivors,
    _progress_once,
    _project_transitive_orphans,
    _remove_packages_from_disk,
    _removed_lock_key,
    _shrink_surviving_selections,
    _stage_shared_local_survivors,
    _sync_integrations_after_uninstall,
    _validate_uninstall_packages,
    gate_retired_hook_survivors,
    manifest_dependency_entries,
)


def _uninstall_summary(removed_count: int, retained_count: int, orphan_count: int) -> str:
    """Return the final summary, counting each package once."""
    summary = f"removed {_count_noun(removed_count, 'package')}"
    if orphan_count:
        orphans = _count_noun(
            orphan_count,
            "orphaned transitive dependency",
            "orphaned transitive dependencies",
        )
        summary += f" and {orphans}"
    if retained_count:
        retained = _count_noun(retained_count, "package")
        summary += f"; removed {retained} from the selected profile only"
    return summary


def _report_uninstall_outcome(
    logger: CommandLogger,
    summary: str,
    integration_cleanup: IntegrationCleanupOutcome,
    integration_cleanup_error: Exception | None,
    mcp_cleanup_error: Exception | None,
    lsp_cleanup_error: Exception | None,
    mcp_cleanup_fatal: bool,
) -> bool:
    """Render the final summary and return whether cleanup was incomplete."""
    # Cleanup evidence warnings (#3701) name kept files before the outcome.
    logger.render_summary()
    integration_incomplete = not integration_cleanup.complete
    if not mcp_cleanup_fatal and lsp_cleanup_error is None and not integration_incomplete:
        if integration_cleanup_error is None:
            logger.success(f"Uninstall complete: {summary}")
        else:
            logger.warning("Package removal finished, but integration cleanup is incomplete.")
    elif integration_incomplete:
        logger.warning("Package removal finished, but managed hook cleanup is incomplete.")
    return (
        mcp_cleanup_fatal
        or lsp_cleanup_error is not None
        or integration_incomplete
        or integration_cleanup_error is not None
    )


def _snapshot_compile_inputs(deploy_root, manifest_path, modules_dir, scope):
    """Snapshot compile-input instructions for the project-scope compile hint."""
    from ...core.scope import InstallScope
    from ...models.apm_package import package_target_selection

    if scope is not InstallScope.PROJECT:
        return None
    manifest_target = None
    with contextlib.suppress(Exception):
        manifest_target = package_target_selection(APMPackage.from_apm_yml(manifest_path))
    return snapshot_compile_inputs(deploy_root, manifest_target, modules_dir)


def _collect_deployed_cleanup_state(
    lockfile,
    dependency_keys: set[str],
) -> tuple[set[str], dict[str, str]]:
    """Snapshot selected deployment paths and hashes before lockfile mutation."""
    from ...core.deployment_ledger import DeploymentLedgerCodec
    from ...integration.base_integrator import BaseIntegrator

    snapshot = DeploymentLedgerCodec.cleanup_snapshot(lockfile, dependency_keys)
    return BaseIntegrator.normalize_managed_files(snapshot.paths) or set(), snapshot.hashes


@dataclass(frozen=True)
class _LockPersistence:
    """Outcome of the survivor lock save that gates native reconciliation."""

    saved: bool
    removal_pending: bool


def _persist_uninstall_lockfile(
    lockfile,
    lockfile_path,
    logger: CommandLogger,
    integration_cleanup: IntegrationCleanupOutcome,
    *,
    defer_empty_removal: bool = True,
) -> _LockPersistence:
    """Bind actual survivor writes only after final lock persistence succeeds.

    With ``defer_empty_removal``, an empty lock is written instead of removed so
    an interrupted last-package native cleanup can retry from authoritative
    survivors; the caller removes it afterwards (``removal_pending``).
    """
    from ...deps.lockfile import LockfileSymlinkError, ensure_lockfile_writable
    from .lockfile_state import lockfile_has_persisted_state

    removal_pending = False
    try:
        if lockfile_has_persisted_state(lockfile):
            lockfile.write(lockfile_path)
        elif defer_empty_removal:
            lockfile.write(lockfile_path)
            removal_pending = True
        else:
            ensure_lockfile_writable(lockfile_path)
            remove_lockfile(lockfile_path)
        # Survivors are durable before native registrations change.
        flush_lockfile_writes()
    except LockfileSymlinkError:
        raise
    except Exception:
        logger.warning(
            "Failed to update lockfile -- it may be out of sync with uninstalled packages."
        )
        saved = False
    else:
        if integration_cleanup.materialization_session is not None:
            integration_cleanup.materialization_session.bind_committed_claims(lockfile)
        saved = True
    return _LockPersistence(saved=saved, removal_pending=removal_pending)


def _hold_native_registration(native_before, logger: CommandLogger) -> None:
    """Report that Claude ownership waits for a durable survivor lock."""
    if native_before:
        logger.warning(
            "Claude native plugin registrations were left unchanged because the lockfile "
            f"update did not complete. {CLAUDE_PLUGIN_REGISTRATION_STALE.sentence()}"
        )


def _persist_lock_then_resync_native(
    *,
    lock_changed: bool,
    persist_allowed: bool,
    lockfile,
    lockfile_path,
    logger: CommandLogger,
    integration_cleanup: IntegrationCleanupOutcome,
    deploy_root,
    modules_dir,
    scope,
    native_before,
) -> None:
    """Persist survivors, converge native registrations, then drop an empty lock.

    An empty last-package lock stays on disk until Claude native cleanup
    converges, so an interrupted removal can retry from authoritative survivors.
    Native state never runs ahead of the lock: a withheld or failed required
    save leaves registrations unchanged; an unchanged lock still reconciles.
    """
    saved = None
    if lock_changed and persist_allowed:
        saved = _persist_uninstall_lockfile(lockfile, lockfile_path, logger, integration_cleanup)
    if lock_changed and (saved is None or not saved.saved):
        _hold_native_registration(native_before, logger)
        return
    _resync_native_registration_after_uninstall(
        deploy_root=deploy_root,
        modules_dir=modules_dir,
        scope=scope,
        lockfile=lockfile,
        logger=logger,
        native_before=native_before,
        lockfile_path=lockfile_path,
    )
    if saved is not None and saved.removal_pending:
        remove_lockfile(lockfile_path)


def _preflight_uninstall_writes(
    surviving_deps,
    deploy_root,
    manifest_path,
    scope,
    logger,
    *,
    dry_run,
    lockfile,
    removed_keys,
    kept_paths=frozenset(),
):
    """Refuse before the first write; return the survivors' trust context (#4185).

    Uninstall refuses out-of-project paths before any change, and survivor
    re-integration needs the org-policy trust map; either refusal stops the
    command while apm.yml, the lockfile and deployed files are untouched.
    """
    from ...core.scope import InstallScope
    from ...security.executables import resolve_project_exec_allow_map

    if dry_run:
        return (None, False)
    _refuse_unvalidated_paths(lockfile, removed_keys, deploy_root, scope, logger, kept_paths)
    if not surviving_deps:
        return (None, False)
    try:
        apm_package = APMPackage.from_apm_yml(manifest_path)
    except Exception:
        return (None, False)
    try:
        allow_map = resolve_project_exec_allow_map(
            deploy_root, apm_package, user_scope=scope is InstallScope.USER, logger=logger
        )
    except PolicyViolationError as exc:
        logger.error(str(exc))
        logger.progress("Resolve the org policy issue above and retry; no APM writes were made.")
        sys.exit(1)
    return (allow_map, True)


def _refuse_unvalidated_paths(
    lockfile, dependency_keys, deploy_root, scope, logger, kept_paths=frozenset()
) -> None:
    """Refuse out-of-project paths before any change.

    Each path this uninstall removes goes through the deploy-path gate the
    cleanup uses; rows another profile keeps on disk are not removed.
    """
    from ...core.deployment_ledger import DeploymentLedgerCodec
    from ...core.scope import InstallScope
    from ...integration.base_integrator import BaseIntegrator

    if lockfile is None or not dependency_keys:
        return
    paths = DeploymentLedgerCodec.cleanup_snapshot(lockfile, dependency_keys).paths
    rejected = sorted(
        path
        for path in paths
        # URI rows (cowork://, copilot-app-db://) have their own owners.
        if "://" not in path
        and path not in kept_paths
        and not BaseIntegrator.validate_deploy_path(
            path,
            deploy_root,
            user_scope=scope is InstallScope.USER,
            allow_final_symlink=True,
        )
    )
    if not rejected:
        return
    logger.error("Uninstall stopped: apm.lock.yaml records paths APM does not manage:")
    for path in rejected:
        logger.error(f"  - {path}")
    logger.progress(
        "Restore apm.lock.yaml from version control or "
        f"{LOCKFILE_UNMANAGED_PATHS.clause()}; no APM writes were made."
    )
    sys.exit(1)


def _sync_integrations_for_manifest(
    *,
    manifest_path,
    deploy_root,
    all_deployed_files,
    logger,
    user_scope: bool,
    lockfile,
    modules_dir,
    deployed_file_hashes,
    default_counts,
    evidence,
    excluded_survivor_keys=frozenset(),
    executable_context=(None, False),
) -> tuple[IntegrationCleanupOutcome, Exception | None]:
    """Run post-uninstall integration cleanup when the manifest is parseable."""
    default_outcome = IntegrationCleanupOutcome(
        counts=default_counts,
        deployed_files={},
        failed_paths=[],
        error_count=0,
    )
    try:
        apm_package = APMPackage.from_apm_yml(manifest_path)
    except Exception as manifest_err:
        logger.warning("Integration cleanup did not finish.")
        logger.warning(INTEGRATIONS_UNSYNCED.sentence())
        logger.verbose_detail(
            f"Integration cleanup skipped: {type(manifest_err).__name__}: {manifest_err}"
        )
        return default_outcome, None

    try:
        return (
            _sync_integrations_after_uninstall(
                apm_package,
                deploy_root,
                all_deployed_files,
                logger,
                user_scope=user_scope,
                lockfile=lockfile,
                modules_dir=modules_dir,
                deployed_file_hashes=deployed_file_hashes,
                excluded_survivor_keys=excluded_survivor_keys,
                evidence=evidence,
                executable_context=executable_context,
            ),
            None,
        )
    except Exception as sync_err:
        logger.warning("Integration cleanup did not finish.")
        if isinstance(sync_err, HookContractError):
            # The survivor preflight refused before any hook settings changed;
            # 'apm install' would refuse the same package until it is migrated.
            logger.warning(str(sync_err))
            logger.warning(
                INTEGRATIONS_UNSYNCED_AFTER.bind(fix="migrate the package above").sentence()
            )
        elif isinstance(sync_err, PolicyViolationError):
            logger.warning(str(sync_err))
            logger.warning(
                INTEGRATIONS_UNSYNCED_AFTER.bind(
                    fix="resolve the org policy issue above"
                ).sentence()
            )
        else:
            logger.warning(INTEGRATIONS_UNSYNCED.sentence())
        logger.verbose_detail(f"Integration cleanup failed: {type(sync_err).__name__}: {sync_err}")
        logger.verbose_detail(traceback.format_exc().rstrip())
        return default_outcome, sync_err


def _enter_manifest_host_scope(stack: contextlib.ExitStack, data: dict, logger) -> None:
    """Resolve shorthand against the manifest's ``default_host`` (req-mf-019, #3843).

    Uninstall must parse declarations exactly as ``apm install`` recorded them,
    so ``apm uninstall acme/x`` finds the ``gitlab.com/acme/x`` lock entry.
    """
    from ...models.dependency.manifest_host import manifest_host_scope_for

    try:
        scope = manifest_host_scope_for(data)
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)
    stack.enter_context(scope)


def _prepare_dependency_sections(data: dict) -> tuple[bool, list, list, list]:
    """Ensure dependency sections exist and return their mutable list views."""
    if "dependencies" not in data:
        data["dependencies"] = {}
    if "apm" not in data["dependencies"]:
        data["dependencies"]["apm"] = []
    had_dev_section = "devDependencies" in data
    if not had_dev_section:
        data["devDependencies"] = {}
    if "apm" not in data["devDependencies"]:
        data["devDependencies"]["apm"] = []
    prod_deps = data["dependencies"]["apm"] or []
    dev_deps = data["devDependencies"]["apm"] or []
    return had_dev_section, prod_deps, dev_deps, manifest_dependency_entries(data)


def _refuse_unwritable_lockfile(lockfile, lockfile_path, removed_keys) -> None:
    """Refuse before scripts, deletions or apm.yml edits when the lock cannot be rewritten."""
    from ...deps.lockfile import refuse_lockfile_before_mutation

    refuse_lockfile_before_mutation(
        lockfile,
        lockfile_path,
        would_mutate=any(lockfile.get_dependency(key) for key in removed_keys)
        if lockfile
        else False,
    )


def _with_orphans(removed_keys: builtins.set, orphans: Any) -> builtins.set:
    """Add the removed packages' orphans to *removed_keys* in place and return it."""
    removed_keys.update(orphans)
    return removed_keys


def _gate_denied_package_keys(
    apm_package: Any, lockfile: Any, modules_dir: Any, deploy_root: Any, user_scope: bool
) -> dict[str, str]:
    """Survivors the org policy now denies, with why, from the gate retirement owner (#4446)."""
    from pathlib import Path

    from ...install.exec_gate import gate_retirements
    from ...install.lsp.integration import has_recorded_lsp_state

    if apm_package is None or not has_recorded_lsp_state(lockfile):
        return {}

    retirements = gate_retirements(
        lockfile, Path(modules_dir), apm_package, Path(deploy_root), user_scope=user_scope
    )
    return dict(retirements.denied_packages) if retirements is not None else {}


def _cleanup_stale_lsp(
    *,
    apm_package: Any,
    manifest_path: Any,
    lockfile: Any,
    lockfile_path: Any,
    modules_dir: Any,
    deploy_root: Any,
    user_scope: bool,
    logger: Any,
) -> tuple[bool, Exception | None]:
    """Reconcile LSP state after uninstall and render an actionable failure.

    The post-removal manifest is loaded here when MCP cleanup did not already
    load it, so LSP-only scopes reconcile too (#4045, #3768).
    """
    try:
        from ...install.lsp.integration import (
            has_recorded_lsp_state,
            reconcile_lsp_after_uninstall,
        )

        if apm_package is None and has_recorded_lsp_state(lockfile):
            apm_package = APMPackage.from_apm_yml(manifest_path)
        updated = reconcile_lsp_after_uninstall(
            excluded_package_keys=_gate_denied_package_keys(
                apm_package, lockfile, modules_dir, deploy_root, user_scope
            ),
            apm_package=apm_package,
            lockfile=lockfile,
            lock_path=lockfile_path,
            modules_dir=modules_dir,
            project_root=deploy_root,
            user_scope=user_scope,
            logger=logger,
        )
        return updated, None
    except Exception as cleanup_error:
        recovery_command = "apm install --global" if user_scope else "apm install"
        logger.error(
            "Uninstall incomplete: package removal completed, but LSP cleanup "
            f"failed: {cleanup_error}. Fix the LSP config path, then run "
            f"'{recovery_command}' to reconcile stale entries."
        )
        logger.verbose_detail(traceback.format_exc().rstrip())
        return False, cleanup_error


def _abort_if_retained_target_cleanup_paths(
    outcome: Any, lockfile: Any, lockfile_path: Any, logger: Any
) -> None:
    """Stop before ownership release when owned target files remain.

    Files already removed are dropped from the lockfile so it records exactly
    what is still on disk; apm.yml keeps the package for the retry (#3656).
    """
    if not outcome.retained_paths:
        return
    # Name why each file was kept (#3701) before stopping.
    logger.render_summary()
    outcome.apply_to(lockfile)
    try:
        lockfile.write(lockfile_path)
        flush_lockfile_writes()
        lock_state = "apm.lock.yaml now records only the files still on disk"
    except OSError as exc:
        lock_state = f"apm.lock.yaml could not be updated ({exc}) and may list removed files"
    logger.error(
        "Uninstall could not remove tracked target files; apm.yml still declares the "
        f"package and {lock_state}. Package directories may already have been removed."
    )
    for path in sorted(outcome.retained_paths):
        logger.error(f"  - {path}")
    logger.error("Resolve or remove the listed files, then retry uninstall.")
    sys.exit(1)


def _uninstall_deletion_evidence(
    lockfile, lockfile_path, deploy_root, cleanup_keys, logger, *, user_scope: bool
):
    """Collect deletion evidence and render it before package sources go (#3701)."""
    from ...integration.cleanup import DeploymentEvidence

    evidence = DeploymentEvidence.collect(
        deploy_root,
        user_scope=user_scope,
        lockfile=lockfile,
        lockfile_root=lockfile_path.parent,
        diagnostics=logger.diagnostics,
        # Not an integrating install: nothing was kept this run.
        kept=(),
    )
    if lockfile:
        evidence.prepare(
            path
            for key in cleanup_keys
            if key in lockfile.dependencies
            for path in lockfile.dependencies[key].deployed_files
        )
    return evidence


def _refuse_uninstall_without_evidence(
    evidence,
    lockfile,
    cleanup_keys: set[str],
    preserved_values,
    staged_refreshes,
    modules_dir,
    logger: CommandLogger,
    *,
    user_scope: bool,
) -> None:
    """Refuse before any change when a file uninstall would delete lacks evidence.

    A lockfile hash alone never authorizes deleting a deployed file
    (req-lk-024, #3701). Checked before package sources, apm.yml or
    apm.lock.yaml change, so a refusal leaves all of them as they were.
    """
    from ...core.deployment_ledger import DeploymentLedgerCodec
    from ...integration.cleanup import refusal_reason, uninstall_evidence_remedies

    if not lockfile or not cleanup_keys:
        return
    # Hash-tracked skill members count too: directory cleanup deletes them.
    selected, selected_hashes = _collect_deployed_cleanup_state(lockfile, cleanup_keys)
    survivors, survivor_hashes = _collect_deployed_cleanup_state(
        lockfile,
        DeploymentLedgerCodec.valid_owner_keys(lockfile, excluded_dependency_keys=cleanup_keys),
    )
    candidates = (selected | builtins.set(selected_hashes)) - survivors
    candidates -= builtins.set(survivor_hashes) | builtins.set(lockfile.local_deployed_files)
    candidates -= builtins.set(preserved_values or ())
    refused = evidence.refusals(sorted(candidates))
    if not refused:
        return
    _cleanup_staged_local_refreshes(staged_refreshes, modules_dir)
    logger.render_summary()
    logger.error(
        "Uninstall refused: deleting these tracked files needs deployment evidence that "
        "APM wrote them. Nothing was removed; apm.yml and apm.lock.yaml were not changed."
    )
    for claim, verdict in refused:
        logger.error(f"  - {claim}: {refusal_reason(verdict)}")
    for remedy in uninstall_evidence_remedies(user_scope=user_scope):
        logger.error(remedy)
    sys.exit(1)


def _report_package_removal_failure(
    logger: CommandLogger,
    error: OSError | PathTraversalError | LocalSurvivorRefreshError | CodexNativeError,
    *,
    user_scope: bool,
) -> None:
    """Explain recoverable metadata without promising filesystem rollback."""
    if isinstance(error, CodexNativeError):
        logger.error(
            f"Codex native removal incomplete: {error} "
            "APM package files, declarations, and lockfile ownership were retained. "
            "Resolve the native error and retry the same uninstall command."
        )
        return
    recovery = "apm install --global" if user_scope else "apm install"
    failure = (
        "shared-slot refresh failed"
        if isinstance(error, LocalSurvivorRefreshError)
        else "package deletion failed"
    )
    cause = (
        "Resolve the unsafe package path"
        if isinstance(error, PathTraversalError)
        else "Resolve the filesystem error"
    )
    logger.error(
        f"Uninstall incomplete: {failure}. "
        "apm.yml and lockfile ownership were retained; some package files "
        f"may already have been removed. {cause} and "
        f"retry the same uninstall command, or run '{recovery}' to restore "
        "the declared packages. Run with --verbose for detailed diagnostics."
    )
    logger.verbose_detail(traceback.format_exc().rstrip())


def _split_selected_profile_packages(
    packages_to_remove: list, lockfile, *, user_scope: bool
) -> tuple[list, list]:
    """Split requested packages into full removals and selected-profile removals.

    A package still deployed to another Claude/Hermes/Codex profile is removed
    only from the selected profile and stays declared (#3425).
    """
    if not user_scope or not lockfile:
        return packages_to_remove, []
    from .profile_scope import split_profile_retained

    return split_profile_retained(
        packages_to_remove,
        lockfile,
        lambda package: _removed_lock_key(package, lockfile),
    )


def _report_selected_profile_packages(
    retained_packages: list, logger: CommandLogger, *, dry_run: bool
) -> None:
    """Explain why packages removed from the selected profile stay declared."""
    for package in retained_packages:
        label = _dependency_public_label(package)
        if dry_run:
            logger.progress(
                f"Dry run: Would remove {label} from the selected profile only; "
                "another profile still uses it"
            )
        else:
            logger.progress(
                f"Removed {label} from the selected profile; kept in apm.yml "
                "because another profile still uses it"
            )


def _dry_run_with_selected_profile(
    packages_to_remove: list,
    retained_packages: list,
    modules_dir,
    logger: CommandLogger,
    apm_yml_path,
    *,
    surviving_dependencies: list,
    lockfile=None,
) -> None:
    """Preview full removals and selected-profile-only removals."""
    if packages_to_remove:
        _dry_run_uninstall(
            packages_to_remove,
            modules_dir,
            logger,
            apm_yml_path,
            surviving_dependencies=surviving_dependencies,
            lockfile=lockfile,
        )
    _report_selected_profile_packages(retained_packages, logger, dry_run=True)


def _retire_selected_profile_packages(
    lockfile,
    profile_keys: frozenset[str],
    profile_survivors: frozenset[str],
    retained_packages: list,
    logger: CommandLogger,
) -> bool:
    """Drop selected-profile rows after cleanup and report kept declarations."""
    if not profile_keys:
        return False
    from .profile_scope import retire_selected_profile_rows

    updated = retire_selected_profile_rows(lockfile, profile_keys, profile_survivors)
    _report_selected_profile_packages(retained_packages, logger, dry_run=False)
    return updated


def _plan_selected_profile_removal(
    lockfile,
    retained_packages: list,
    *,
    excluded_keys: set[str],
    modules_dir,
    apm_yml_path,
    logger: CommandLogger,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return keys losing only selected-profile rows, and rows kept on disk.

    Transitive dependencies reachable only from retained packages lose the
    same rows; their declarations and other rows stay.
    """
    if not retained_packages:
        return frozenset(), frozenset()
    from .profile_scope import selected_profile_survivors

    retained_orphans, _ = _project_transitive_orphans(
        lockfile,
        retained_packages,
        modules_dir,
        apm_yml_path,
        logger,
        warn_on_incomplete=False,
    )
    keys = {_removed_lock_key(package, lockfile) for package in retained_packages}
    keys = frozenset((keys | set(retained_orphans)) - excluded_keys)
    return keys, frozenset(selected_profile_survivors(lockfile, keys))


def _finish_selected_profile_uninstall(
    *,
    lockfile,
    lockfile_path,
    lock_updated: bool,
    retained_count: int,
    labels: tuple[str, ...],
    scope,
    manifest_path,
    logger: CommandLogger,
    verbose: bool,
    deploy_root,
) -> None:
    """Persist selected-profile row removal when every package stays declared."""
    if lock_updated:
        _persist_uninstall_lockfile(
            lockfile,
            lockfile_path,
            logger,
            IntegrationCleanupOutcome(counts={}, deployed_files={}, failed_paths=[], error_count=0),
            defer_empty_removal=False,
        )
    logger.success(
        f"Uninstall complete: removed {_count_noun(retained_count, 'package')} "
        "from the selected profile only"
    )
    _fire_uninstall_scripts(
        "post-uninstall",
        packages=labels,
        scope=scope,
        manifest_path=manifest_path,
        logger=logger,
        verbose=verbose,
        deploy_root=deploy_root,
    )


@click.command(
    help="Remove packages using manifest entries or direct locked keys from 'apm deps list'",
    short_help="Remove packages by manifest entry or locked key",
)
@click.argument("packages", nargs=-1, required=True)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview removal plan (pre-uninstall scripts still run)",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed removal information")
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Remove from the APM user home instead of the current project",
)
@click.pass_context
@serialized_lifecycle
@lockfile_writer(LockfileWriter.UNINSTALL)
def uninstall(ctx, packages, dry_run, verbose, global_):
    """Remove APM packages from apm.yml and apm_modules (like npm uninstall).

    This command removes packages from both the apm.yml dependencies list
    and the apm_modules/ directory. It's the opposite of 'apm install <package>'.

    Examples:
        apm uninstall acme/my-package                # Remove one package
        apm uninstall org/pkg1 org/pkg2              # Remove multiple packages
        apm uninstall acme/my-package --dry-run      # Show what would be removed
        apm uninstall -g acme/my-package             # Remove from user scope
        apm uninstall my-plugin@official             # Remove by marketplace name
    """
    from ...core.scope import (
        InstallScope,
        get_apm_dir,
        get_deploy_root,
        get_manifest_path,
        get_modules_dir,
    )

    scope = InstallScope.USER if global_ else InstallScope.PROJECT

    manifest_path = get_manifest_path(scope)
    apm_dir = require_current_cli(get_apm_dir(scope), command="apm uninstall")
    deploy_root = get_deploy_root(scope)
    modules_dir = get_modules_dir(scope)
    from ...install.mcp.command import manifest_label

    manifest_display = (
        manifest_label(manifest_path) if scope is InstallScope.USER else APM_YML_FILENAME
    )

    logger = CommandLogger("uninstall", verbose=verbose, dry_run=dry_run)
    staged_local_refreshes = {}
    apm_package = None
    registration_token = _publish_native_registration(deploy_root, scope, manifest_path)
    host_scope = contextlib.ExitStack()
    try:
        # Check if apm.yml exists
        if not manifest_path.exists():
            if scope is InstallScope.USER:
                logger.error(
                    f"No user manifest found at {manifest_display}. Install a package globally "
                    "first with 'apm install -g <package>' or create the file manually."
                )
            else:
                logger.error(f"No {manifest_display} found. {PROJECT_MANIFEST_MISSING.sentence()}")
            sys.exit(1)

        if not packages:
            logger.error("No packages specified. Specify packages to uninstall.")
            sys.exit(1)

        if scope is InstallScope.USER:
            logger.progress(f"Uninstalling from user scope ({manifest_path.parent}/)")

        logger.start(f"Uninstalling {_count_noun(len(packages), 'package')}...")

        # Read current apm.yml
        from ...models.manifest_io import load_manifest_roundtrip
        from ...utils.yaml_io import dump_yaml_roundtrip

        apm_yml_path = manifest_path
        try:
            data = load_manifest_roundtrip(apm_yml_path) or {}
        except Exception as e:
            logger.error(f"Failed to read {manifest_display}: {e}")
            sys.exit(1)
        _enter_manifest_host_scope(host_scope, data, logger)

        # Track whether devDependencies was synthesised so we don't leave
        # an empty section behind for projects that never used --dev.
        had_dev_section, prod_deps, dev_deps, current_deps = _prepare_dependency_sections(data)
        # `apm install --dev <pkg>` writes under devDependencies.apm. Uninstall
        # must scan both sections so dev-installed packages are removable
        # (regression trap for #1549).

        # Load lockfile early: used for marketplace ref resolution in Step 1
        # and reused for MCP state capture and transitive orphan cleanup below.
        from ...deps.lockfile import LockFile, get_lockfile_path

        lockfile_path = get_lockfile_path(apm_dir)
        lockfile = LockFile.read(lockfile_path)

        # Step 1: Validate packages
        from ...core.auth import AuthResolver

        # Lazy: only construct the resolver when we will actually call the registry.
        auth_resolver = None if dry_run else AuthResolver()
        packages_to_remove, packages_not_found = _validate_uninstall_packages(
            packages, current_deps, logger, lockfile, auth_resolver=auth_resolver, dry_run=dry_run
        )
        if packages_not_found:
            logger.error(
                f"Uninstall aborted: {_count_noun(len(packages_not_found), 'requested package')} "
                "could not be selected. Resolve the errors above and retry; "
                "no changes were made."
            )
            sys.exit(1)
        if not packages_to_remove:
            logger.error("No packages were selected; no changes were made.")
            sys.exit(1)
        selected_package_labels = tuple(
            _dependency_lifecycle_label(package) for package in packages_to_remove
        )
        surviving_deps = list(current_deps)
        for declaration in manifest_declarations_of(current_deps, packages_to_remove):
            surviving_deps.remove(declaration)
        removed_keys = builtins.set()
        for package in packages_to_remove:
            removed_keys.add(_removed_lock_key(package, lockfile))
        projected_orphans, _ = _project_transitive_orphans(
            lockfile,
            packages_to_remove,
            modules_dir,
            apm_yml_path,
            logger,
            warn_on_incomplete=False,
        )
        _preflight_uninstall_survivors(
            surviving_deps,
            modules_dir,
            lockfile=lockfile,
            excluded_keys=removed_keys | builtins.set(projected_orphans),
            source_root=manifest_path.parent,
        )
        if not dry_run:
            _refuse_unwritable_lockfile(lockfile, lockfile_path, removed_keys)

        # Fire scripts only after every requested identifier has selected one
        # dependency, so failed validation is an atomic no-op.
        _fire_uninstall_scripts(
            "pre-uninstall",
            packages=selected_package_labels,
            scope=scope,
            manifest_path=manifest_path,
            logger=logger,
            verbose=verbose,
            deploy_root=deploy_root,
        )

        # Pre-uninstall scripts may intentionally edit the manifest or lockfile.
        # Reload both, then revalidate without repeating success diagnostics so
        # those edits are preserved rather than overwritten by a stale snapshot.
        try:
            data = load_manifest_roundtrip(apm_yml_path) or {}
        except Exception as e:
            logger.error(f"Failed to re-read {manifest_display} after pre-uninstall: {e}")
            sys.exit(1)
        had_dev_section, prod_deps, dev_deps, current_deps = _prepare_dependency_sections(data)
        lockfile = LockFile.read(lockfile_path)
        native_before = _native_ownership_before(lockfile, lockfile_path)
        packages_to_remove, packages_not_found = _validate_uninstall_packages(
            packages,
            current_deps,
            logger,
            lockfile,
            auth_resolver=auth_resolver,
            dry_run=dry_run,
            log_matches=False,
        )
        if packages_not_found or not packages_to_remove:
            logger.error(
                "Uninstall aborted because pre-uninstall scripts changed dependency "
                "selection. Review apm.yml and retry; no APM writes were made."
            )
            sys.exit(1)
        selected_package_labels = tuple(
            _dependency_lifecycle_label(package) for package in packages_to_remove
        )
        packages_to_remove, retained_packages = _split_selected_profile_packages(
            packages_to_remove, lockfile, user_scope=scope is InstallScope.USER
        )
        surviving_deps = list(current_deps)
        for declaration in manifest_declarations_of(current_deps, packages_to_remove):
            surviving_deps.remove(declaration)
        removed_keys = builtins.set()
        for package in packages_to_remove:
            removed_keys.add(_removed_lock_key(package, lockfile))
        projected_orphans, _ = _project_transitive_orphans(
            lockfile,
            packages_to_remove,
            modules_dir,
            apm_yml_path,
            logger,
            warn_on_incomplete=False,
        )
        profile_keys, profile_survivors = _plan_selected_profile_removal(
            lockfile,
            retained_packages,
            excluded_keys=removed_keys | builtins.set(projected_orphans),
            modules_dir=modules_dir,
            apm_yml_path=apm_yml_path,
            logger=logger,
        )
        _preflight_uninstall_survivors(
            surviving_deps,
            modules_dir,
            lockfile=lockfile,
            excluded_keys=removed_keys | builtins.set(projected_orphans),
            source_root=manifest_path.parent,
        )
        survivor_trust = _preflight_uninstall_writes(
            surviving_deps,
            deploy_root,
            manifest_path,
            scope,
            logger,
            dry_run=dry_run,
            lockfile=lockfile,
            removed_keys=removed_keys | builtins.set(projected_orphans) | profile_keys,
            kept_paths=profile_survivors,
        )
        if not dry_run:
            staged_local_refreshes = _stage_shared_local_survivors(
                packages_to_remove,
                surviving_deps,
                modules_dir,
                manifest_path.parent,
                logger,
            )

        # Step 2: Dry run
        if dry_run:
            _dry_run_with_selected_profile(
                packages_to_remove,
                retained_packages,
                modules_dir,
                logger,
                apm_yml_path,
                surviving_dependencies=surviving_deps,
                lockfile=lockfile,
            )
            return

        # Deletion evidence (#3701): receipts, or the locked render, which is
        # materialized here while the removed packages' sources still exist.
        deletion_evidence = _uninstall_deletion_evidence(
            lockfile,
            lockfile_path,
            deploy_root,
            removed_keys | builtins.set(projected_orphans) | profile_keys,
            logger,
            user_scope=scope is InstallScope.USER,
        )
        _refuse_uninstall_without_evidence(
            deletion_evidence,
            lockfile,
            removed_keys | builtins.set(projected_orphans) | profile_keys,
            profile_survivors,
            staged_local_refreshes,
            modules_dir,
            logger,
            user_scope=scope is InstallScope.USER,
        )

        # Remove package content before releasing declarations or ownership.
        # A failed recursive delete can be partial; retain metadata for retry,
        # rather than claiming that already-deleted files were rolled back.
        refreshed_survivor_keys = builtins.set()
        compile_inputs = _snapshot_compile_inputs(deploy_root, manifest_path, modules_dir, scope)
        try:
            from ...codex_plugins.registrar import remove_codex_plugins

            remove_codex_plugins(
                modules_dir=modules_dir,
                dependency_keys=removed_keys | builtins.set(projected_orphans),
                cwd=manifest_path.parent,
                scope=scope,
            )
            _remove_packages_from_disk(
                packages_to_remove,
                modules_dir,
                logger,
                staged_refreshes=staged_local_refreshes,
                refreshed_survivor_keys=refreshed_survivor_keys,
                lockfile=lockfile,
            )
            orphan_removed, actual_orphans = _cleanup_transitive_orphans(
                lockfile, packages_to_remove, modules_dir, apm_yml_path, logger
            )
            # Survivors whose selection shrinks are redeployed in full below.
            refreshed_survivor_keys |= _shrink_surviving_selections(
                lockfile, packages_to_remove, modules_dir, apm_yml_path, logger
            )
        except (OSError, PathTraversalError, LocalSurvivorRefreshError, CodexNativeError) as exc:
            _report_package_removal_failure(logger, exc, user_scope=scope is InstallScope.USER)
            sys.exit(1)

        # Step 3: Remove target-scoped files while their lockfile ownership is
        # still recoverable. The post-removal manifest can omit a removed
        # package's target, so its target must not be reconstructed from it.
        if lockfile and (removed_keys or profile_keys):
            from ...install.manifest_reconcile import (
                SelectedCleanupOutcome,
                reconcile_target_deployed_files,
            )
            from ...integration.skill_layout import (
                recorded_deployed_paths,
                resolve_reintegration_targets,
            )

            cleanup_target_names = list(APMPackage.from_apm_yml(manifest_path).canonical_targets)
            cleanup_targets = resolve_reintegration_targets(
                deploy_root,
                user_scope=scope is InstallScope.USER,
                explicit_target=cleanup_target_names or None,
                recorded_paths=recorded_deployed_paths(lockfile),
            )
            cleanup_keys = removed_keys | builtins.set(projected_orphans) | profile_keys
            cleanup_outcome = SelectedCleanupOutcome()
            reconcile_target_deployed_files(
                project_root=deploy_root,
                lockfile=lockfile,
                active_targets=cleanup_targets,
                declared_targets=cleanup_targets,
                diagnostics=logger.diagnostics,
                evidence=deletion_evidence,
                dependency_keys=cleanup_keys,
                remove_selected_ownership=True,
                selected_outcome=cleanup_outcome,
                user_scope=scope is InstallScope.USER,
                logger=logger,
                preserved_selected_values=profile_survivors,
            )
            _abort_if_retained_target_cleanup_paths(
                cleanup_outcome, lockfile, lockfile_path, logger
            )

        profile_lock_updated = _retire_selected_profile_packages(
            lockfile, profile_keys, profile_survivors, retained_packages, logger
        )
        if not packages_to_remove:
            _finish_selected_profile_uninstall(
                lockfile=lockfile,
                lockfile_path=lockfile_path,
                lock_updated=profile_lock_updated,
                retained_count=len(retained_packages),
                labels=selected_package_labels,
                scope=scope,
                manifest_path=manifest_path,
                logger=logger,
                verbose=verbose,
                deploy_root=deploy_root,
            )
            return

        # Step 4: Remove from apm.yml
        printed: builtins.set[str] = builtins.set()
        for section, declarations in (
            ("dependencies.apm", prod_deps),
            ("devDependencies.apm", dev_deps),
        ):
            for declaration in manifest_declarations_of(declarations, packages_to_remove):
                declarations.remove(declaration)
                package_label = _dependency_public_label(declaration)
                _progress_once(
                    logger, printed, f"Removed {package_label} from {section} in apm.yml"
                )
        data["dependencies"]["apm"] = prod_deps
        data["devDependencies"]["apm"] = dev_deps
        # Drop empty devDependencies wrappers so the manifest stays clean
        # for projects that never used --dev.
        if not data["devDependencies"]["apm"]:
            del data["devDependencies"]["apm"]
            if not data["devDependencies"] and not had_dev_section:
                del data["devDependencies"]
        try:
            dump_yaml_roundtrip(data, apm_yml_path)
            logger.success(f"Updated {manifest_display}")
        except Exception as e:
            logger.error(f"Failed to write {manifest_display}: {e}")
            sys.exit(1)

        # Step 5: Capture pre-uninstall MCP state (lockfile already read above)
        _pre_uninstall_mcp_servers = (
            builtins.set(lockfile.mcp_servers) if lockfile else builtins.set()
        )

        # Step 8: Collect deployed files for removed packages (before lockfile mutation)
        # Orphans join the removed set; survivors whose hooks the gates no longer
        # allow are redeployed in full under install's gate (#4446).
        refreshed_survivor_keys, gate_excluded = gate_retired_hook_survivors(
            lockfile,
            modules_dir,
            manifest_path,
            deploy_root,
            _with_orphans(removed_keys, actual_orphans),
            refreshed_survivor_keys,
            user_scope=scope is InstallScope.USER,
            logger=logger,
        )
        if lockfile:
            all_deployed_files, all_deployed_file_hashes = _collect_deployed_cleanup_state(
                lockfile,
                removed_keys | refreshed_survivor_keys,
            )
        else:
            all_deployed_files, all_deployed_file_hashes = set(), {}

        # Step 9: Mutate dependency state in memory. Persistence happens once
        # after survivor ownership, hashes, ledger, and MCP state agree.
        lockfile_updated = False
        if lockfile:
            for pkg in packages_to_remove:
                key = _removed_lock_key(pkg, lockfile)
                if key in lockfile.dependencies:
                    del lockfile.dependencies[key]
                    lockfile_updated = True
            for orphan_key in actual_orphans:
                if orphan_key in lockfile.dependencies:
                    del lockfile.dependencies[orphan_key]
                    lockfile_updated = True

        # Step 10: Sync integrations
        cleaned = {
            "prompts": 0,
            "agents": 0,
            "skills": 0,
            "commands": 0,
            "hooks": 0,
            "instructions": 0,
        }
        surviving_deployed_files = {}
        integration_cleanup = IntegrationCleanupOutcome(
            counts=cleaned,
            deployed_files=surviving_deployed_files,
            failed_paths=[],
            error_count=0,
        )
        integration_cleanup, integration_cleanup_error = _sync_integrations_for_manifest(
            manifest_path=manifest_path,
            deploy_root=deploy_root,
            all_deployed_files=all_deployed_files,
            logger=logger,
            user_scope=scope is InstallScope.USER,
            lockfile=lockfile,
            modules_dir=modules_dir,
            deployed_file_hashes=all_deployed_file_hashes,
            default_counts=cleaned,
            excluded_survivor_keys=profile_keys | gate_excluded,
            evidence=deletion_evidence,
            executable_context=survivor_trust,
        )
        cleaned = integration_cleanup.counts
        surviving_deployed_files = integration_cleanup.deployed_files
        lockfile_ready = True

        if lockfile:
            try:
                from .lockfile_state import reconcile_uninstall_deployment_state

                lockfile_updated = (
                    reconcile_uninstall_deployment_state(
                        lockfile,
                        deploy_root=deploy_root,
                        all_deployed_files=all_deployed_files,
                        surviving_deployed_files=surviving_deployed_files,
                        fully_refreshed_dependency_keys=refreshed_survivor_keys,
                    )
                    or lockfile_updated
                )
            except Exception as state_err:
                lockfile_ready = False
                logger.warning(
                    "Lockfile state could not be reconciled. "
                    f"{LOCKFILE_STATE_UNRECONCILED.sentence()}"
                )
                logger.verbose_detail(f"Lockfile reconciliation error: {state_err}")

        for label, count in cleaned.items():
            if count > 0:
                kind = label.removesuffix("s")
                logger.progress(
                    f"Cleaned up {_count_noun(count, f'integrated {kind}')}", symbol="check"
                )
                logger.verbose_detail(f"    Removed {_count_noun(count, f'deployed {kind} file')}")

        # Step 11: MCP cleanup
        from ...adapters.client.intellij import IntelliJConfigError

        mcp_cleanup_error = None
        mcp_cleanup_fatal = False
        try:
            if _pre_uninstall_mcp_servers or (lockfile and lockfile.mcp_target_servers):
                apm_package = APMPackage.from_apm_yml(manifest_path)
                _cleanup_stale_mcp(
                    apm_package,
                    lockfile,
                    lockfile_path,
                    _pre_uninstall_mcp_servers,
                    modules_dir=get_modules_dir(scope),
                    project_root=deploy_root,
                    user_scope=scope is InstallScope.USER,
                    scope=scope,
                    persist=False,
                    logger=logger,
                )
        except (
            IntelliJConfigError,
            MCPUninstallCleanupError,
            PathTraversalError,
        ) as cleanup_error:
            mcp_cleanup_error = cleanup_error
            mcp_cleanup_fatal = True
            recovery = ""
            if isinstance(cleanup_error, PathTraversalError):
                recovery = (
                    f" {MCP_ENTRY_STALE.bind(fix='fix the MCP config path').sentence()}"
                    " The package was already removed from apm.yml."
                )
            elif isinstance(cleanup_error, MCPUninstallCleanupError):
                recovery = (
                    f" {MCP_ENTRY_STALE.bind(fix='fix the reported target configs').sentence()}"
                    " Lock ownership was retained."
                )
            logger.error(
                "Uninstall incomplete: package removal completed, but MCP cleanup failed: "
                f"{cleanup_error}{recovery}"
            )
            logger.verbose_detail(traceback.format_exc().rstrip())
        except Exception as cleanup_error:
            mcp_cleanup_error = cleanup_error
            logger.error(
                "Uninstall incomplete: package removal completed, but MCP cleanup failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}. "
                f"{MCP_ENTRY_STALE.bind(fix='fix the target config').sentence()} "
                "Lock ownership was retained."
            )
            logger.verbose_detail(traceback.format_exc().rstrip())

        lsp_lock_updated, lsp_cleanup_error = _cleanup_stale_lsp(
            apm_package=apm_package,
            manifest_path=manifest_path,
            lockfile=lockfile,
            lockfile_path=lockfile_path,
            modules_dir=modules_dir,
            deploy_root=deploy_root,
            user_scope=scope is InstallScope.USER,
            logger=logger,
        )
        lockfile_updated = lsp_lock_updated or lockfile_updated

        # Persist survivors, then rebuild APM-owned native plugin registrations
        # from that locked state: only APM's own rows disappear, user bytes never do.
        _persist_lock_then_resync_native(
            lock_changed=bool(lockfile and (lockfile_updated or profile_lock_updated)),
            persist_allowed=bool(lockfile_ready and mcp_cleanup_error is None),
            lockfile=lockfile,
            lockfile_path=lockfile_path,
            logger=logger,
            integration_cleanup=integration_cleanup,
            deploy_root=deploy_root,
            modules_dir=modules_dir,
            scope=scope,
            native_before=native_before,
        )

        # Final summary: count each package once, whichever stores it left.
        summary = _uninstall_summary(
            len(packages_to_remove), len(retained_packages), orphan_removed
        )
        if compile_inputs is not None:
            hint_compile_after_removal(compile_inputs, modules_dir, logger)
        cleanup_incomplete = _report_uninstall_outcome(
            logger,
            summary,
            integration_cleanup,
            integration_cleanup_error,
            mcp_cleanup_error,
            lsp_cleanup_error,
            mcp_cleanup_fatal,
        )

        # Fire post-uninstall lifecycle scripts
        _fire_uninstall_scripts(
            "post-uninstall",
            packages=selected_package_labels,
            scope=scope,
            manifest_path=manifest_path,
            logger=logger,
            verbose=verbose,
            deploy_root=deploy_root,
        )
        if cleanup_incomplete:
            sys.exit(1)

    except Exception as e:
        logger.error(f"Error uninstalling packages: {e}")
        sys.exit(1)
    finally:
        host_scope.close()
        _retire_native_registration(registration_token)
        _cleanup_staged_local_refreshes(staged_local_refreshes, modules_dir)


def _publish_native_registration(deploy_root, scope, manifest_path):
    """Publish the Copilot native-plugin capability for this uninstall.

    Reads the SAME canonical target declaration (the manifest's ``target:``/
    ``targets:`` field, via :func:`package_target_selection`) that ``install``
    and hook reconciliation already use -- never falls back to directory
    auto-detection, which would incorrectly treat ``copilot`` as active
    whenever a ``.github/`` directory happens to exist on disk regardless of
    what this project actually declares.
    """
    from ...copilot_plugins.capability import (
        activate_native_registration,
        resolve_native_registration_capability,
    )
    from ...core.scope import InstallScope
    from ...integration.targets import resolve_targets
    from ...models.apm_package import package_target_selection

    manifest_target = None
    with contextlib.suppress(Exception):
        manifest_target = package_target_selection(APMPackage.from_apm_yml(manifest_path))
    try:
        targets = resolve_targets(
            deploy_root, user_scope=scope is InstallScope.USER, explicit_target=manifest_target
        )
    except Exception:
        targets = ()
    return activate_native_registration(resolve_native_registration_capability(targets))


@dataclass(frozen=True)
class _NativeBefore:
    """Claude native ownership and lock bytes before uninstall mutates the lock."""

    ownership: dict
    lock_bytes: bytes | None

    def __bool__(self) -> bool:
        return bool(self.ownership)


def _native_ownership_before(lockfile, lockfile_path) -> _NativeBefore:
    """Snapshot what a failed native reconcile must restore (req-na-004)."""
    from ...claude_plugins.registrar import native_ownership

    if lockfile is None:
        return _NativeBefore({}, None)
    return _NativeBefore(native_ownership(lockfile), lockfile_path.read_bytes())


def _resync_native_registration_after_uninstall(
    *, deploy_root, modules_dir, scope, lockfile, logger, native_before=None, lockfile_path=None
) -> None:
    """Rebuild native plugin rows from surviving locked state.

    Claude removal errors remain fatal; Copilot settings refresh is advisory.
    """
    from ...claude_plugins.admission import ClaudeNativePluginError
    from ...claude_plugins.registrar import reconcile_claude_plugins

    try:
        reconcile_claude_plugins(
            modules_dir=modules_dir,
            scope=scope,
            previous=native_before.ownership if native_before is not None else None,
            lockfile=lockfile,
            deploy_root=deploy_root,
            logger=logger,
        )
    except ClaudeNativePluginError as exc:
        if native_before is not None and native_before.lock_bytes and lockfile_path is not None:
            # One rollback unit: keep the removed plugins' ownership for the retry.
            restore_lockfile_bytes(lockfile_path, native_before.lock_bytes)
        raise ClaudeNativePluginError(
            "Package removal may already be complete, but Claude registration did not converge. "
            f"{exc} Reconcile remaining declarations with 'apm install --global' and the "
            "original --target selection; do not re-add the removed package."
        ) from exc
    try:
        from ...agent_plugins.errors import AgentPluginError
        from ...copilot_plugins.registrar import resync_native_plugins
        from ...copilot_plugins.settings import CopilotSettingsCollisionError

        resync_native_plugins(
            project_root=deploy_root,
            modules_dir=modules_dir,
            scope=scope,
            lockfile=lockfile,
            logger=logger,
        )
    except (CopilotSettingsCollisionError, AgentPluginError, OSError) as registration_error:
        logger.warning(
            f"GitHub Copilot plugin registration could not be updated: {registration_error} "
            f"{COPILOT_PLUGIN_REGISTRATION_STALE.sentence()}"
        )


def _retire_native_registration(token) -> None:
    """Retire the published capability once the command finishes."""
    from ...copilot_plugins.capability import reset_native_registration

    if token is None:
        return
    # A token created in another context is not ours to reset.
    with contextlib.suppress(ValueError):
        reset_native_registration(token)


def _fire_uninstall_scripts(
    event_name: str,
    *,
    packages,
    scope,
    manifest_path,
    logger,
    verbose: bool,
    deploy_root,
) -> None:
    """Build a script runner and fire an uninstall lifecycle event.

    Best-effort: all exceptions are swallowed so scripts never block
    the uninstall flow.
    """
    import contextlib

    with contextlib.suppress(Exception):
        from apm_cli.core.lifecycle_scripts import (
            LifecycleEvent,
            PackageInfo,
            build_runner_from_context,
        )
        from apm_cli.core.scope import is_user_scope, manifest_root

        runner = build_runner_from_context(
            logger=logger,
            verbose=verbose,
            project_root=str(manifest_root(deploy_root, user_scope=is_user_scope(scope))),
        )

        pkg_infos = [PackageInfo(name=str(pkg)) for pkg in packages]
        scope_name = scope.value if hasattr(scope, "value") else str(scope)
        event = LifecycleEvent.create(
            event=event_name,
            packages=pkg_infos,
            scope=scope_name,
            working_directory=str(deploy_root),
        )

        runner.fire(event_name, event)
