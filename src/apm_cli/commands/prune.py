"""APM prune command."""

import contextlib
import sys
from pathlib import Path

import click

from apm_cli.core.remediation import (
    HOOK_CONFIG_STALE,
    PROJECT_MANIFEST_MISSING,
    PRUNE_INCOMPLETE,
    PRUNE_NATIVE_GATE_UNCHECKED,
    PRUNE_PARTIAL,
)

from ..constants import APM_MODULES_DIR, APM_YML_FILENAME
from ..core.command_logger import CommandLogger
from ..core.deployment_ledger import DeploymentLedgerCodec
from ..core.deployment_state import LocatorKind
from ..core.scope import InstallScope, manifest_file

# APM Dependencies
from ..deps.lockfile import LockFile, LockfileFormatError, get_lockfile_path
from ..deps.lockfile_write_authority import (
    LockfileWriter,
    flush_lockfile_writes,
    lockfile_writer,
    remove_lockfile,
)
from ..install.locking import serialized_lifecycle_unless
from ..install.orphans import (
    InstalledPaths,
    OrphanOperation,
    ReachabilityProbe,
    decide_orphan_removal,
)
from ..install.phases.finalize import hint_compile_after_removal, snapshot_compile_inputs
from ..integration.base_integrator import BaseIntegrator
from ..integration.cleanup import DeploymentEvidence, remove_stale_deployed_files
from ..models.apm_package import APMPackage, package_target_selection
from ..models.dependency.materialization import MaterializationPathCollisionError
from ..native_plugins.uninstall import (
    NativeGateCheckError,
    preview_native_settings,
    retire_gated_native_plugins,
)
from ..utils.path_security import safe_rmtree
from ._helpers import _scan_installed_packages
from .uninstall.lockfile_state import lockfile_has_persisted_state


def _preflight_prune_survivors(
    apm_package: APMPackage,
    project_root: Path,
    apm_modules_dir: Path,
    *,
    lockfile: LockFile | None,
    excluded_keys: set[str],
    logger=None,
) -> None:
    """Reject unsafe survivors before prune can mutate package or target state."""
    from ..agent_plugins.errors import preflight_reintegration_survivors
    from ..models.apm_package import surviving_dependency_refs_for_reintegration

    del logger  # Nothing to warn about: target exclusion is routine and silent.
    survivors = surviving_dependency_refs_for_reintegration(
        apm_package,
        project_root,
        lockfile=lockfile,
    )
    preflight_reintegration_survivors(
        (
            dependency
            for dependency in survivors
            if dependency.get_unique_key() not in excluded_keys
        ),
        apm_modules_dir,
    )


@click.command(
    help=(
        "Remove APM packages absent from the resolved dependency graph "
        "and repair stale deployment owners"
    ),
    short_help="Remove packages absent from the dependency graph",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview package removal and ownership repair without mutating anything",
)
@click.pass_context
@serialized_lifecycle_unless("dry_run")
@lockfile_writer(LockfileWriter.PRUNE)
def prune(ctx, dry_run):
    """Remove orphaned packages and repair stale deployment ownership.

    This command cleans up the apm_modules/ directory by removing packages that are
    neither declared in apm.yml nor retained as transitive nodes in apm.lock.yaml.
    It also reconciles invalid canonical deployment owners in the lockfile.

    Examples:
        apm prune           # Remove orphaned packages
        apm prune --dry-run # Show what would be removed
    """
    from ..deps.lockfile_generator import require_current_cli

    require_current_cli(Path.cwd(), command="apm prune")
    if not dry_run:
        from ..core.deployment_ledger import retire_receipts_of_missing_projects

        retire_receipts_of_missing_projects()
    apm_modules_dir = Path(APM_MODULES_DIR)
    logger = CommandLogger("prune", dry_run=dry_run)
    registration_token = _publish_native_registration(Path.cwd())
    try:
        if not manifest_file(Path(".")).exists():
            logger.error(f"No apm.yml found. {PROJECT_MANIFEST_MISSING.sentence()}")
            sys.exit(1)

        logger.start("Analyzing installed packages vs apm.yml...")

        try:
            apm_package = APMPackage.from_apm_yml(manifest_file(Path(".")))
        except Exception as e:
            logger.error(f"Failed to parse {APM_YML_FILENAME}: {e}")
            sys.exit(1)

        # Runs before the lockfile is read: retiring a revoked bundle's hooks
        # rewrites apm.lock.yaml, and prune must not write a stale copy back.
        # A re-check that cannot resolve targets writes nothing; package
        # removal still proceeds, as with hook reconciliation below. Any other
        # re-check failure (policy, lockfile, integrator) fails closed.
        from ..core.remediation import BUNDLE_HOOKS_UNCHECKED
        from ..install.local_bundle_hook_trust import (
            BundleHookTargetsUnresolved,
            reconcile_scope_bundle_hooks,
        )

        try:
            reconcile_scope_bundle_hooks(
                InstallScope.PROJECT, apm_package, logger=logger, dry_run=dry_run
            )
        except BundleHookTargetsUnresolved as e:
            logger.warning(
                f"Local bundle hooks were NOT re-checked: target resolution failed ({e}). "
                f"{BUNDLE_HOOKS_UNCHECKED.sentence()}"
            )
        except Exception as e:
            logger.error(f"Local bundle hook re-check failed: {e}")
            sys.exit(1)

        try:
            declared_deps = apm_package.get_all_apm_dependencies()
            project_root = Path.cwd()
            lockfile_path = get_lockfile_path(project_root)
            lockfile = LockFile.read(lockfile_path)
            native_before = _native_ownership(lockfile, lockfile_path)
            # A gate-retired native record makes this a lock-changing prune (#3921);
            # a dry run previews it without touching the lock (#4754).
            native_retired = retire_gated_native_plugins(
                lockfile,
                apm_modules_dir,
                manifest_file(project_root),
                project_root,
                logger,
                dry_run=dry_run,
            )
        except LockfileFormatError as e:
            logger.error(str(e))
            sys.exit(1)
        except NativeGateCheckError as e:
            logger.error(str(e))
            logger.error_detail(
                "Prune stopped before removing packages or native plugins. "
                f"{PRUNE_NATIVE_GATE_UNCHECKED.sentence()}"
            )
            sys.exit(1)
        except Exception as e:
            logger.error(f"Failed to parse {APM_YML_FILENAME}: {e}")
            sys.exit(1)

        installed_packages = (
            _scan_installed_packages(apm_modules_dir) if apm_modules_dir.exists() else set()
        )
        try:
            decision = decide_orphan_removal(
                operation=OrphanOperation.PRUNE,
                roots=(),
                candidate_universe=lockfile,
                installed_paths=InstalledPaths(
                    root=apm_modules_dir, packages=frozenset(installed_packages)
                ),
                resolved_keys=None,
                intended=declared_deps,
                materialized=None,
                reachability=ReachabilityProbe(
                    project_root=project_root,
                    apm_modules_dir=apm_modules_dir,
                    direct_refs=tuple(declared_deps),
                    dev_refs=(),
                ),
                deployment_claims=None,
            )
        except MaterializationPathCollisionError as e:
            logger.error(str(e))
            sys.exit(1)
        except Exception as e:
            logger.error(f"Failed to parse {APM_YML_FILENAME}: {e}")
            sys.exit(1)
        orphaned_packages = list(decision.removed_paths)
        for package in decision.retained_paths:
            logger.progress(f"Retained {package}: required package content.")
        missing_orphaned_keys = list(decision.stale_keys)
        deployment_ledger = (
            DeploymentLedgerCodec.from_lockfile(lockfile) if lockfile is not None else None
        )
        owner_violations = (
            DeploymentLedgerCodec.owner_reference_violations(
                lockfile,
                ledger=deployment_ledger,
            )
            if lockfile is not None
            else ()
        )

        # Staging records whose root is already gone must not keep authorizing
        # compile if the directory is recreated (#3705).
        absent_staging = (
            lockfile.absent_staging_slugs(apm_modules_dir) if lockfile is not None else []
        )

        if (
            not orphaned_packages
            and not missing_orphaned_keys
            and not owner_violations
            and not absent_staging
            and not native_retired
        ):
            logger.nothing_to_prune()
            _resync_native_plugins(
                project_root,
                apm_modules_dir,
                lockfile,
                logger,
                dry_run,
                native_before,
            )
            return

        planned_pruned_keys = set(decision.removed_keys)
        if planned_pruned_keys and not dry_run:
            _preflight_prune_survivors(
                apm_package,
                project_root,
                apm_modules_dir,
                lockfile=lockfile,
                excluded_keys=planned_pruned_keys,
                logger=logger,
            )

        if orphaned_packages:
            logger.warning(f"Found {len(orphaned_packages)} orphaned package(s):")
            for pkg_name in orphaned_packages:
                suffix = " (would be removed)" if dry_run else ""
                logger.warning(f"  - {pkg_name}{suffix}")
        if owner_violations:
            logger.warning(
                f"Found {len(owner_violations)} invalid deployment ownership "
                "record(s) in apm.lock.yaml."
            )
        if missing_orphaned_keys:
            logger.progress(
                f"Found {len(missing_orphaned_keys)} stale lockfile dependency "
                "record(s) without removable package content."
            )
        if absent_staging:
            logger.progress(
                f"Found {len(absent_staging)} stale local bundle staging record(s) "
                f"without a staging directory: {', '.join(absent_staging)}."
            )

        if dry_run:
            if missing_orphaned_keys:
                logger.dry_run_notice(
                    f"remove {len(missing_orphaned_keys)} stale lockfile dependency record(s)"
                )
            if absent_staging:
                logger.dry_run_notice(
                    f"forget {len(absent_staging)} stale local bundle staging record(s)"
                )
            planned_owner_repairs = (
                DeploymentLedgerCodec.owner_reference_violations(
                    lockfile,
                    excluded_dependency_keys=planned_pruned_keys,
                    ledger=deployment_ledger,
                )
                if lockfile is not None
                else ()
            )
            if planned_owner_repairs:
                logger.dry_run_notice(
                    f"repair {len(planned_owner_repairs)} deployment ownership record(s)"
                )
                for violation in planned_owner_repairs:
                    removed_owners = set(violation.invalid_owners)
                    if violation.invalid_active_owner is not None:
                        removed_owners.add(violation.invalid_active_owner)
                    locator = violation.locator
                    # Name the path and target; the composite ledger key is internal (#3994).
                    logger.dry_run_notice(
                        f"repair {locator.value} ({locator.target}): "
                        f"remove owner(s) {', '.join(sorted(removed_owners))}"
                    )
                    logger.verbose_detail(f"    ledger key: {locator.key}")
            preview_native_settings(
                lockfile,
                planned_pruned_keys | set(missing_orphaned_keys) | set(native_retired),
                apm_modules_dir,
                project_root,
                logger,
            )
            logger.success("Dry run complete - no changes made")
            return

        # Deletion evidence (#3701): receipts, or the locked render, which is
        # materialized now while the orphaned package sources still exist.
        deletion_evidence = DeploymentEvidence.collect(
            project_root,
            user_scope=False,
            lockfile=lockfile,
            lockfile_root=project_root,
            diagnostics=logger.diagnostics,
            # Not an integrating install: nothing was kept this run.
            kept=(),
        )
        if lockfile is not None:
            deletion_evidence.prepare(
                path
                for key in planned_pruned_keys
                if key in lockfile.dependencies
                for path in lockfile.dependencies[key].deployed_files
            )

        removed_count = 0
        failed_count = 0
        removed_packages: list[str] = []
        pruned_keys = list(missing_orphaned_keys)
        pruned_key_set = set(pruned_keys)
        deleted_pkg_paths: list[Path] = []
        compile_inputs = snapshot_compile_inputs(
            project_root, package_target_selection(apm_package), apm_modules_dir
        )
        for org_repo_name in orphaned_packages:
            path_parts = org_repo_name.split("/")
            pkg_path = apm_modules_dir.joinpath(*path_parts)
            try:
                safe_rmtree(pkg_path, apm_modules_dir)
                logger.progress(f"Removed {org_repo_name}")
                removed_count += 1
                removed_packages.append(org_repo_name)
                for dep_key in decision.path_keys[org_repo_name]:
                    if dep_key not in pruned_key_set:
                        pruned_keys.append(dep_key)
                        pruned_key_set.add(dep_key)
                deleted_pkg_paths.append(pkg_path)
            except Exception as e:
                failed_count += 1
                logger.error(f"Failed to remove {org_repo_name}: {e}")

        BaseIntegrator.cleanup_empty_parents(deleted_pkg_paths, stop_at=apm_modules_dir)

        if lockfile is not None:
            cleanup_claims = {
                dep_key: (
                    tuple(lockfile.dependencies[dep_key].deployed_files),
                    dict(lockfile.dependencies[dep_key].deployed_file_hashes),
                )
                for dep_key in pruned_keys
                if dep_key in lockfile.dependencies
            }
            reconciled = DeploymentLedgerCodec.reconcile_owner_references(
                lockfile,
                excluded_dependency_keys=pruned_keys,
                ledger=deployment_ledger,
                project_root=project_root,
                diagnostics=logger.diagnostics,
            )
            retained_paths = {
                DeploymentLedgerCodec.legacy_value(record.locator)
                for record in reconciled.ledger.records.values()
            }
            deleted_targets: list[Path] = []
            trusted_cleanup_paths: set[str] = set()
            for dep_key, (paths, hashes) in cleanup_claims.items():
                trusted_paths = set(paths)
                trusted_cleanup_paths.update(trusted_paths)
                cleanup = remove_stale_deployed_files(
                    trusted_paths - retained_paths,
                    project_root,
                    dep_key=dep_key,
                    targets=None,
                    diagnostics=logger.diagnostics,
                    evidence=deletion_evidence,
                    recorded_hashes=hashes,
                    failed_path_retained=False,
                )
                logger.stale_cleanup(dep_key, cleanup)
                deleted_targets.extend(cleanup.deleted_targets)

            BaseIntegrator.cleanup_empty_parents(
                deleted_targets,
                stop_at=project_root,
            )

            for violation in owner_violations:
                locator = violation.locator
                if (
                    locator.kind == LocatorKind.PROJECT_RELATIVE
                    and locator.value not in trusted_cleanup_paths
                    and (project_root / locator.value).exists()
                ):
                    logger.diagnostics.warn(
                        f"Preserved {locator.value}: its deployment owner is "
                        "invalid, so the lockfile record was repaired without "
                        "deleting untrusted bytes. Inspect and remove the path "
                        "manually if it is no longer needed."
                    )

            for dep_key in pruned_keys:
                lockfile.dependencies.pop(dep_key, None)
            lockfile.retire_local_staging([*removed_packages, *absent_staging])
            DeploymentLedgerCodec.apply_to_lockfile(reconciled.ledger, lockfile)
            if pruned_keys and (lockfile.mcp_servers or lockfile.mcp_target_servers):
                from .uninstall.engine import _cleanup_stale_mcp

                _cleanup_stale_mcp(
                    apm_package,
                    lockfile,
                    lockfile_path,
                    set(lockfile.mcp_servers),
                    modules_dir=apm_modules_dir,
                    project_root=project_root,
                    scope=InstallScope.PROJECT,
                    persist=False,
                )
            try:
                if lockfile_has_persisted_state(lockfile):
                    lockfile.write(lockfile_path)
                    flush_lockfile_writes()
                else:
                    remove_lockfile(lockfile_path)
            except Exception as e:
                logger.render_summary()
                logger.error(f"Failed to update apm.lock.yaml: {e}")
                if removed_packages:
                    logger.error_detail(
                        f"Package directories already removed: {', '.join(removed_packages)}."
                    )
                logger.error_detail(
                    f"Filesystem cleanup may be partial. {PRUNE_PARTIAL.sentence()}"
                )
                sys.exit(1)

        _resync_native_plugins(
            project_root,
            apm_modules_dir,
            lockfile,
            logger,
            dry_run,
            native_before,
        )

        logger.render_summary()

        if pruned_keys:
            # Reconcile merged-hook ownership (settings.json / hooks.json
            # entries and their apm-hooks.json sidecars) for the packages
            # just pruned. This delegates to the same canonical
            # clear-then-rebuild owner `apm uninstall` already uses --
            # prune must not reimplement hook-entry filtering itself.
            # apm.yml is not mutated by prune (orphaned packages are, by
            # definition, already absent from it), so the manifest parsed
            # at the top of this command still reflects the desired state.
            #
            # Best-effort: package/lockfile pruning has already committed
            # by this point, so a reconciliation failure is a warning
            # (not an error) -- it does not roll back or fail the command.
            try:
                from ..integration.hook_integrator import HookIntegrator

                hook_stats = HookIntegrator().reconcile_after_removal(
                    apm_package, project_root, lockfile=lockfile
                )
                hook_errors = hook_stats.get("errors", 0)
                if hook_errors:
                    logger.warning(
                        f"Hook reconciliation incomplete for {hook_errors} "
                        "dependency(ies) -- some hook entries may be stale. "
                        f"{HOOK_CONFIG_STALE.sentence()}"
                    )
                elif hook_stats.get("merged_hooks_changed"):
                    logger.progress("Reconciled merged hook ownership for pruned package(s)")
            except Exception as e:
                logger.warning(
                    f"Hook reconciliation failed: {e}. Some hook entries may be "
                    f"stale -- {HOOK_CONFIG_STALE.clause()}."
                )

        hint_compile_after_removal(compile_inputs, apm_modules_dir, logger)

        if failed_count:
            logger.error(
                f"Prune incomplete: removed {removed_count} orphaned package(s); "
                f"failed to remove {failed_count} package(s)."
            )
            logger.error_detail(f"Filesystem cleanup may be partial. {PRUNE_INCOMPLETE.sentence()}")
            sys.exit(1)

        if removed_count > 0:
            message = f"Pruned {removed_count} orphaned package(s)"
            if owner_violations:
                message += f" and repaired {len(owner_violations)} deployment ownership record(s)"
            logger.success(message)
        elif pruned_keys:
            logger.success(f"Repaired {len(pruned_keys)} stale dependency record(s)")
        elif owner_violations:
            logger.success(f"Repaired {len(owner_violations)} deployment ownership record(s)")
        else:
            logger.warning("No packages were removed")

    except Exception as e:
        logger.error(f"Error pruning packages: {e}")
        sys.exit(1)
    finally:
        _retire_native_registration(registration_token)


def _native_ownership(lockfile, lockfile_path):
    """Native ownership and lock bytes before prune mutates the lock (req-na-004)."""
    from ..claude_plugins.registrar import native_ownership

    if lockfile is None:
        return None, None
    return native_ownership(lockfile), lockfile_path.read_bytes()


def _resync_native_plugins(
    project_root, apm_modules_dir, lockfile, logger, dry_run: bool, native_before
) -> None:
    """Retire pruned and gate-retired native plugins through the shared owner."""
    from ..claude_plugins.admission import ClaudeNativePluginError
    from ..native_plugins.uninstall import reconcile_after_uninstall

    ownership, lock_bytes = native_before
    try:
        reconcile_after_uninstall(
            deploy_root=project_root,
            modules_dir=apm_modules_dir.absolute(),
            scope=InstallScope.PROJECT,
            lockfile=lockfile,
            logger=logger,
            ownership=ownership,
            lock_bytes=lock_bytes,
            lockfile_path=get_lockfile_path(project_root),
            dry_run=dry_run,
        )
    except ClaudeNativePluginError as exc:
        logger.error(str(exc))
        sys.exit(1)


def _publish_native_registration(project_root: Path):
    """Publish the Copilot native-plugin capability for this prune.

    Reads the SAME canonical target declaration (``apm.yml``'s ``target:``/
    ``targets:`` field, via :func:`package_target_selection`) that ``install``
    and hook reconciliation already use -- never falls back to directory
    auto-detection, which would incorrectly treat ``copilot`` as active
    whenever a ``.github/`` directory happens to exist on disk regardless of
    what this project actually declares.
    """
    from ..copilot_plugins.capability import (
        activate_native_registration,
        resolve_native_registration_capability,
    )
    from ..integration.targets import resolve_targets
    from ..models.apm_package import package_target_selection

    manifest_target = None
    with contextlib.suppress(Exception):
        manifest_target = package_target_selection(
            APMPackage.from_apm_yml(manifest_file(project_root))
        )
    try:
        targets = resolve_targets(project_root, user_scope=False, explicit_target=manifest_target)
    except Exception:
        targets = ()
    return activate_native_registration(resolve_native_registration_capability(targets))


def _retire_native_registration(token) -> None:
    """Retire the published capability once the command finishes."""
    from ..copilot_plugins.capability import reset_native_registration

    if token is None:
        return
    # A token created in another context is not ours to reset.
    with contextlib.suppress(ValueError):
        reset_native_registration(token)
