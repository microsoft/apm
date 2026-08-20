"""APM uninstall command CLI."""

import builtins
import sys
import traceback

import click

from ...constants import APM_YML_FILENAME
from ...core.command_logger import CommandLogger
from ...models.apm_package import APMPackage
from .engine import (
    _cleanup_staged_local_refreshes,
    _cleanup_stale_mcp,
    _cleanup_transitive_orphans,
    _dependency_public_label,
    _dry_run_uninstall,
    _parse_dependency_entry,
    _remove_packages_from_disk,
    _stage_shared_local_survivors,
    _sync_integrations_after_uninstall,
    _validate_uninstall_packages,
)


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
        surviving_deps = list(current_deps)
        for package in packages_to_remove:
            surviving_deps.remove(package)
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
            _dry_run_uninstall(
                packages_to_remove,
                modules_dir,
                logger,
                apm_yml_path,
                surviving_dependencies=surviving_deps,
            )
            return

        # Step 3: Remove from apm.yml
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

        # Step 4: Capture pre-uninstall MCP state (lockfile already read above)
        _pre_uninstall_mcp_servers = (
            builtins.set(lockfile.mcp_servers) if lockfile else builtins.set()
        )

        # Step 5: Remove packages from disk
        refreshed_survivor_keys = builtins.set()
        removed_from_modules = _remove_packages_from_disk(
            packages_to_remove,
            modules_dir,
            logger,
            staged_refreshes=staged_local_refreshes,
            refreshed_survivor_keys=refreshed_survivor_keys,
        )

        # Step 6: Cleanup transitive orphans
        orphan_removed, actual_orphans = _cleanup_transitive_orphans(
            lockfile, packages_to_remove, modules_dir, apm_yml_path, logger
        )
        removed_from_modules += orphan_removed

        # Step 7: Collect deployed files for removed packages (before lockfile mutation)
        from ...integration.base_integrator import BaseIntegrator

        removed_keys = builtins.set()
        for pkg in packages_to_remove:
            try:
                ref = _parse_dependency_entry(pkg)
                removed_keys.add(ref.get_unique_key())
            except (ValueError, TypeError, AttributeError, KeyError):
                removed_keys.add(pkg)
        removed_keys.update(actual_orphans)
        all_deployed_files = builtins.set()
        if lockfile:
            for dep_key, dep in lockfile.dependencies.items():
                if dep_key in removed_keys or dep_key in refreshed_survivor_keys:
                    all_deployed_files.update(dep.deployed_files)
        all_deployed_files = (
            BaseIntegrator.normalize_managed_files(all_deployed_files) or builtins.set()
        )

        # Step 8: Mutate dependency state in memory. Persistence happens once
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

        # Step 9: Sync integrations
        cleaned = {
            "prompts": 0,
            "agents": 0,
            "skills": 0,
            "commands": 0,
            "hooks": 0,
            "instructions": 0,
        }
        surviving_deployed_files = {}
        lockfile_ready = True
        try:
            apm_package = APMPackage.from_apm_yml(manifest_path)
            cleaned, surviving_deployed_files = _sync_integrations_after_uninstall(
                apm_package,
                deploy_root,
                all_deployed_files,
                logger,
                user_scope=scope is InstallScope.USER,
                lockfile=lockfile,
                modules_dir=modules_dir,
            )
        except Exception as _sync_err:
            # Surface why integration cleanup failed instead of swallowing
            # silently. Previously a bare `except: pass` here masked
            # Windows-only failures where the DB row was never deleted on
            # `apm uninstall --target copilot-app`.
            logger.warning(f"Integration cleanup failed: {type(_sync_err).__name__}: {_sync_err}")
            # Preserve the traceback under verbose for diagnosing
            # platform-specific failures without spamming default output.
            logger.verbose_detail(traceback.format_exc().rstrip())
            logger.verbose_detail(
                "Some integrated files may remain. Run `apm install --force` to resync."
            )

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

        # Step 10: MCP cleanup
        from ...adapters.client.intellij import IntelliJConfigError
        from ...utils.path_security import PathTraversalError

        mcp_cleanup_error = None
        try:
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
        except (IntelliJConfigError, PathTraversalError) as cleanup_error:
            mcp_cleanup_error = cleanup_error
            recovery = ""
            if isinstance(cleanup_error, PathTraversalError):
                recovery = (
                    " Fix the MCP config path, then run 'apm install' to reconcile "
                    "the stale entry; the package was already removed from apm.yml."
                )
            logger.error(
                "Uninstall incomplete: package removal completed, but MCP cleanup failed: "
                f"{cleanup_error}{recovery}"
            )
            logger.verbose_detail(traceback.format_exc().rstrip())
        except Exception as cleanup_error:
            logger.warning(
                f"MCP cleanup during uninstall failed: {type(cleanup_error).__name__}: "
                f"{cleanup_error}. Package removal completed; run 'apm install' "
                "to reconcile stale MCP entries."
            )
            logger.verbose_detail(traceback.format_exc().rstrip())

        if lockfile and lockfile_updated and lockfile_ready:
            try:
                from .lockfile_state import lockfile_has_persisted_state

                if lockfile_has_persisted_state(lockfile):
                    lockfile.write(lockfile_path)
                else:
                    lockfile_path.unlink(missing_ok=True)
            except Exception:
                logger.warning(
                    "Failed to update lockfile -- it may be out of sync with uninstalled packages."
                )

        # Final summary
        summary_lines = [f"Removed {len(packages_to_remove)} package(s) from apm.yml"]
        if removed_from_modules > 0:
            summary_lines.append(f"Removed {removed_from_modules} package(s) from apm_modules/")
        if mcp_cleanup_error is None:
            logger.success("Uninstall complete: " + ", ".join(summary_lines))

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
        if mcp_cleanup_error is not None:
            sys.exit(1)

    except Exception as e:
        logger.error(f"Error uninstalling packages: {e}")
        sys.exit(1)
    finally:
        _cleanup_staged_local_refreshes(staged_local_refreshes, modules_dir)


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
