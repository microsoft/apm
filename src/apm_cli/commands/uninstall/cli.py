"""APM uninstall command CLI."""

import builtins
import contextlib
import sys
import traceback
from typing import Any

import click

from ...codex_plugins.client import CodexNativeError
from ...constants import APM_YML_FILENAME
from ...core.command_logger import CommandLogger
from ...install.locking import serialized_lifecycle
from ...models.apm_package import APMPackage
from ...utils.path_security import PathTraversalError
from .engine import (
    IntegrationCleanupOutcome,
    LocalSurvivorRefreshError,
    MCPUninstallCleanupError,
    _cleanup_staged_local_refreshes,
    _cleanup_stale_mcp,
    _cleanup_transitive_orphans,
    _dependency_public_label,
    _dry_run_uninstall,
    _parse_dependency_entry,
    _preflight_uninstall_survivors,
    _project_transitive_orphans,
    _remove_packages_from_disk,
    _stage_shared_local_survivors,
    _sync_integrations_after_uninstall,
    _validate_uninstall_packages,
)


def _report_uninstall_outcome(
    logger: CommandLogger,
    summary_lines: list[str],
    integration_cleanup: IntegrationCleanupOutcome,
    integration_cleanup_error: Exception | None,
    mcp_cleanup_error: Exception | None,
    lsp_cleanup_error: Exception | None,
    mcp_cleanup_fatal: bool,
) -> bool:
    """Render the final summary and return whether cleanup was incomplete."""
    integration_incomplete = not integration_cleanup.complete
    if not mcp_cleanup_fatal and lsp_cleanup_error is None and not integration_incomplete:
        if integration_cleanup_error is None:
            logger.success("Uninstall complete: " + ", ".join(summary_lines))
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


def _collect_deployed_cleanup_state(
    lockfile,
    dependency_keys: set[str],
) -> tuple[set[str], dict[str, str]]:
    """Snapshot selected deployment paths and hashes before lockfile mutation."""
    from ...core.deployment_ledger import DeploymentLedgerCodec
    from ...integration.base_integrator import BaseIntegrator

    snapshot = DeploymentLedgerCodec.cleanup_snapshot(lockfile, dependency_keys)
    return BaseIntegrator.normalize_managed_files(snapshot.paths) or set(), snapshot.hashes


def _persist_uninstall_lockfile(
    lockfile,
    lockfile_path,
    logger: CommandLogger,
    integration_cleanup: IntegrationCleanupOutcome,
    *,
    defer_empty_removal: bool = True,
) -> bool:
    """Bind actual survivor writes only after final lock persistence succeeds.

    With ``defer_empty_removal``, an empty lock is written instead of removed so
    an interrupted last-package native cleanup can retry from authoritative
    survivors; the caller removes it afterwards. Returns whether that is pending.
    """
    from ...deps.lockfile import LockfileSymlinkError
    from .lockfile_state import lockfile_has_persisted_state

    removal_pending = False
    try:
        if lockfile_has_persisted_state(lockfile):
            lockfile.write(lockfile_path)
        elif lockfile_path.is_symlink():
            raise LockfileSymlinkError(lockfile_path)
        elif defer_empty_removal:
            lockfile.write(lockfile_path)
            removal_pending = True
        else:
            lockfile_path.unlink(missing_ok=True)
    except LockfileSymlinkError:
        raise
    except Exception:
        logger.warning(
            "Failed to update lockfile -- it may be out of sync with uninstalled packages."
        )
    else:
        if integration_cleanup.materialization_session is not None:
            integration_cleanup.materialization_session.bind_committed_claims(lockfile)
    return removal_pending


def _persist_lock_then_resync_native(
    *,
    persist: bool,
    lockfile,
    lockfile_path,
    logger: CommandLogger,
    integration_cleanup: IntegrationCleanupOutcome,
    deploy_root,
    modules_dir,
    scope,
) -> None:
    """Persist survivors, converge native registrations, then drop an empty lock.

    An empty last-package lock stays on disk until Claude native cleanup
    converges, so an interrupted removal can retry from authoritative survivors.
    """
    empty = persist and bool(
        _persist_uninstall_lockfile(lockfile, lockfile_path, logger, integration_cleanup)
    )
    _resync_native_registration_after_uninstall(
        deploy_root=deploy_root,
        modules_dir=modules_dir,
        scope=scope,
        lockfile=lockfile,
        logger=logger,
    )
    if empty:
        lockfile_path.unlink(missing_ok=True)


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
    excluded_survivor_keys=frozenset(),
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
        logger.warning("Run 'apm install' to resync remaining integrations.")
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
            ),
            None,
        )
    except Exception as sync_err:
        logger.warning("Integration cleanup did not finish.")
        logger.warning("Run 'apm install' to resync remaining integrations.")
        logger.verbose_detail(f"Integration cleanup failed: {type(sync_err).__name__}: {sync_err}")
        logger.verbose_detail(traceback.format_exc().rstrip())
        return default_outcome, sync_err


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
    return had_dev_section, prod_deps, dev_deps, [*prod_deps, *dev_deps]


def _cleanup_stale_lsp(
    *,
    apm_package: Any,
    lockfile: Any,
    lockfile_path: Any,
    modules_dir: Any,
    deploy_root: Any,
    user_scope: bool,
    logger: Any,
) -> tuple[bool, Exception | None]:
    """Reconcile LSP state after uninstall and render an actionable failure."""
    try:
        from ...install.lsp.integration import reconcile_lsp_after_uninstall

        updated = reconcile_lsp_after_uninstall(
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


def _abort_if_retained_target_cleanup_paths(retained_cleanup_paths: set[Any], logger: Any) -> None:
    """Stop before ownership release when owned target files remain."""
    if retained_cleanup_paths:
        logger.error(
            "Uninstall could not remove tracked target files; manifest and lockfile "
            "ownership were preserved. Package directories may already have been removed."
        )
        for path in sorted(retained_cleanup_paths):
            logger.error(f"  - {path}")
        logger.error("Resolve or remove the listed files, then retry uninstall.")
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

    A package still deployed to another Claude/Hermes profile is removed only
    from the selected profile and stays declared (#3425).
    """
    if not user_scope or not lockfile:
        return packages_to_remove, []
    from .profile_scope import split_profile_retained

    return split_profile_retained(
        packages_to_remove,
        lockfile,
        lambda package: _parse_dependency_entry(package).get_unique_key(),
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
) -> None:
    """Preview full removals and selected-profile-only removals."""
    if packages_to_remove:
        _dry_run_uninstall(
            packages_to_remove,
            modules_dir,
            logger,
            apm_yml_path,
            surviving_dependencies=surviving_dependencies,
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
    keys = {_parse_dependency_entry(package).get_unique_key() for package in retained_packages}
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
        f"Uninstall complete: removed {retained_count} package(s) from the selected profile only"
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
    help="Remove packages using manifest entries or direct locked keys from 'apm deps list'"
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
    help="Remove from user scope (~/.apm/) instead of the current project",
)
@click.pass_context
@serialized_lifecycle
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
    apm_dir = get_apm_dir(scope)
    deploy_root = get_deploy_root(scope)
    modules_dir = get_modules_dir(scope)
    manifest_display = str(manifest_path) if scope is InstallScope.USER else APM_YML_FILENAME

    logger = CommandLogger("uninstall", verbose=verbose, dry_run=dry_run)
    staged_local_refreshes = {}
    apm_package = None
    registration_token = _publish_native_registration(deploy_root, scope, manifest_path)
    try:
        # Check if apm.yml exists
        if not manifest_path.exists():
            if scope is InstallScope.USER:
                logger.error(
                    f"No user manifest found at {manifest_display}. Install a package globally "
                    "first with 'apm install -g <package>' or create the file manually."
                )
            else:
                logger.error(f"No {manifest_display} found. Run 'apm init' in this project first.")
            sys.exit(1)

        if not packages:
            logger.error("No packages specified. Specify packages to uninstall.")
            sys.exit(1)

        if scope is InstallScope.USER:
            logger.progress("Uninstalling from user scope (~/.apm/)")

        logger.start(f"Uninstalling {len(packages)} package(s)...")

        # Read current apm.yml
        from ...utils.yaml_io import dump_yaml_roundtrip, load_yaml_roundtrip

        apm_yml_path = manifest_path
        try:
            data = load_yaml_roundtrip(apm_yml_path) or {}
        except Exception as e:
            logger.error(f"Failed to read {apm_yml_path}: {e}")
            sys.exit(1)

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
                f"Uninstall aborted: {len(packages_not_found)} requested package(s) "
                "could not be selected. Resolve the errors above and retry; "
                "no changes were made."
            )
            sys.exit(1)
        if not packages_to_remove:
            logger.error("No packages were selected; no changes were made.")
            sys.exit(1)
        selected_package_labels = tuple(
            _dependency_public_label(package) for package in packages_to_remove
        )
        surviving_deps = list(current_deps)
        removed_keys = builtins.set()
        for package in packages_to_remove:
            surviving_deps.remove(package)
            removed_keys.add(_parse_dependency_entry(package).get_unique_key())
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
            data = load_yaml_roundtrip(apm_yml_path) or {}
        except Exception as e:
            logger.error(f"Failed to re-read {apm_yml_path} after pre-uninstall: {e}")
            sys.exit(1)
        had_dev_section, prod_deps, dev_deps, current_deps = _prepare_dependency_sections(data)
        lockfile = LockFile.read(lockfile_path)
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
            _dependency_public_label(package) for package in packages_to_remove
        )
        packages_to_remove, retained_packages = _split_selected_profile_packages(
            packages_to_remove, lockfile, user_scope=scope is InstallScope.USER
        )
        surviving_deps = list(current_deps)
        removed_keys = builtins.set()
        for package in packages_to_remove:
            surviving_deps.remove(package)
            removed_keys.add(_parse_dependency_entry(package).get_unique_key())
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
            )
            return

        # Remove package content before releasing declarations or ownership.
        # A failed recursive delete can be partial; retain metadata for retry,
        # rather than claiming that already-deleted files were rolled back.
        refreshed_survivor_keys = builtins.set()
        try:
            from ...codex_plugins.registrar import remove_codex_plugins

            remove_codex_plugins(
                modules_dir=modules_dir,
                dependency_keys=removed_keys | builtins.set(projected_orphans),
                cwd=manifest_path.parent,
                scope=scope,
            )
            removed_from_modules = _remove_packages_from_disk(
                packages_to_remove,
                modules_dir,
                logger,
                staged_refreshes=staged_local_refreshes,
                refreshed_survivor_keys=refreshed_survivor_keys,
            )
            orphan_removed, actual_orphans = _cleanup_transitive_orphans(
                lockfile, packages_to_remove, modules_dir, apm_yml_path, logger
            )
        except (OSError, PathTraversalError, LocalSurvivorRefreshError, CodexNativeError) as exc:
            _report_package_removal_failure(logger, exc, user_scope=scope is InstallScope.USER)
            sys.exit(1)
        removed_from_modules += orphan_removed

        # Step 3: Remove target-scoped files while their lockfile ownership is
        # still recoverable. The post-removal manifest can omit a removed
        # package's target, so its target must not be reconstructed from it.
        if lockfile and (removed_keys or profile_keys):
            from ...install.manifest_reconcile import reconcile_target_deployed_files
            from ...integration.targets import resolve_targets
            from ...utils.diagnostics import DiagnosticCollector

            cleanup_target_names = list(APMPackage.from_apm_yml(manifest_path).canonical_targets)
            cleanup_targets = resolve_targets(
                deploy_root,
                user_scope=scope is InstallScope.USER,
                explicit_target=cleanup_target_names or None,
            )
            cleanup_keys = removed_keys | builtins.set(projected_orphans) | profile_keys
            retained_cleanup_paths = builtins.set()
            reconcile_target_deployed_files(
                project_root=deploy_root,
                lockfile=lockfile,
                active_targets=cleanup_targets,
                declared_targets=cleanup_targets,
                diagnostics=DiagnosticCollector(verbose=verbose),
                dependency_keys=cleanup_keys,
                remove_selected_ownership=True,
                retained_selected_paths=retained_cleanup_paths,
                user_scope=scope is InstallScope.USER,
                logger=logger,
                preserved_selected_values=profile_survivors,
            )
            _abort_if_retained_target_cleanup_paths(retained_cleanup_paths, logger)

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
        for package in packages_to_remove:
            package_label = _dependency_public_label(package)
            if package in dev_deps:
                dev_deps.remove(package)
                section = "devDependencies.apm"
            elif package in prod_deps:
                prod_deps.remove(package)
                section = "dependencies.apm"
            logger.progress(f"Removed {package_label} from {section} in apm.yml")
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
            logger.success(f"Updated {apm_yml_path} (removed {len(packages_to_remove)} package(s))")
        except Exception as e:
            logger.error(f"Failed to write {apm_yml_path}: {e}")
            sys.exit(1)

        # Step 5: Capture pre-uninstall MCP state (lockfile already read above)
        _pre_uninstall_mcp_servers = (
            builtins.set(lockfile.mcp_servers) if lockfile else builtins.set()
        )

        # Step 8: Collect deployed files for removed packages (before lockfile mutation)
        removed_keys.update(actual_orphans)
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
                try:
                    ref = _parse_dependency_entry(pkg)
                    key = ref.get_unique_key()
                except (ValueError, TypeError, AttributeError, KeyError):
                    key = pkg
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
            excluded_survivor_keys=profile_keys,
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
                    "Run 'apm install --force' to resync before retrying."
                )
                logger.verbose_detail(f"Lockfile reconciliation error: {state_err}")

        for label, count in cleaned.items():
            if count > 0:
                logger.progress(f"Cleaned up {count} integrated {label}", symbol="check")
                logger.verbose_detail(f"    Removed {count} deployed {label} file(s)")

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
                    " Fix the MCP config path, then run 'apm install' to reconcile "
                    "the stale entry; the package was already removed from apm.yml."
                )
            elif isinstance(cleanup_error, MCPUninstallCleanupError):
                recovery = (
                    " Fix the reported target configs, then run 'apm install' "
                    "to reconcile stale MCP entries; lock ownership was retained."
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
                f"{type(cleanup_error).__name__}: {cleanup_error}. Fix the target config, "
                "then run 'apm install' to reconcile stale MCP entries; lock ownership "
                "was retained."
            )
            logger.verbose_detail(traceback.format_exc().rstrip())

        lsp_lock_updated, lsp_cleanup_error = _cleanup_stale_lsp(
            apm_package=apm_package,
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
            persist=bool(
                lockfile
                and (lockfile_updated or profile_lock_updated)
                and lockfile_ready
                and mcp_cleanup_error is None
            ),
            lockfile=lockfile,
            lockfile_path=lockfile_path,
            logger=logger,
            integration_cleanup=integration_cleanup,
            deploy_root=deploy_root,
            modules_dir=modules_dir,
            scope=scope,
        )

        # Final summary
        summary_lines = [
            f"Removed {len(packages_to_remove)} package(s) from apm.yml",
            *(
                [f"removed {len(retained_packages)} package(s) from the selected profile only"]
                if retained_packages
                else []
            ),
        ]
        if removed_from_modules > 0:
            summary_lines.append(f"Removed {removed_from_modules} package(s) from apm_modules/")
        cleanup_incomplete = _report_uninstall_outcome(
            logger,
            summary_lines,
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


def _resync_native_registration_after_uninstall(
    *, deploy_root, modules_dir, scope, lockfile, logger
) -> None:
    """Rebuild native plugin rows from surviving locked state.

    Claude removal errors remain fatal; Copilot settings refresh is advisory.
    """
    from ...claude_plugins.admission import ClaudeNativePluginError
    from ...claude_plugins.registrar import resync_claude_plugins

    try:
        resync_claude_plugins(
            modules_dir=modules_dir,
            scope=scope,
            lockfile=lockfile,
            logger=logger,
        )
    except ClaudeNativePluginError as exc:
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
            "Re-run 'apm install' to re-register once resolved."
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

        runner = build_runner_from_context(
            logger=logger,
            verbose=verbose,
            project_root=str(deploy_root),
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
