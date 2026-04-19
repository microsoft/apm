"""APM install command and dependency installation engine."""

import builtins
import sys
from pathlib import Path
from typing import List

import click

from ..constants import (
    APM_LOCK_FILENAME,
    APM_MODULES_DIR,
    APM_YML_FILENAME,
    GITHUB_DIR,
    CLAUDE_DIR,
    SKILL_MD_FILENAME,
    InstallMode,
)
from ..drift import (
    build_download_ref,
    detect_orphans,
    detect_ref_change,
    detect_stale_files,
)
from ..models.results import InstallResult
from ..core.command_logger import InstallLogger, _ValidationOutcome
from ..utils.console import _rich_echo, _rich_error, _rich_info, _rich_success
from ..utils.diagnostics import DiagnosticCollector


# Re-export lockfile hash helper so existing call sites and the regression
# test pinned in #762 (test_hash_deployed_is_module_level_and_works) keep
# working via "apm_cli.commands.install._hash_deployed".
from apm_cli.install.phases.lockfile import compute_deployed_hashes as _hash_deployed

from ..utils.github_host import default_host, is_valid_fqdn
from ..utils.path_security import safe_rmtree

# Re-export validation leaf helpers so that existing test patches like
# @patch("apm_cli.commands.install._validate_package_exists") keep working.
# _validate_and_add_packages_to_apm_yml stays here (not moved) because it
# calls _validate_package_exists and _local_path_failure_reason via module-
# level name lookup -- keeping it co-located means @patch on this module
# intercepts those calls without test changes.
from apm_cli.install.validation import (
    _local_path_failure_reason,
    _local_path_no_markers_hint,
    _validate_package_exists,
)

# Re-export local-content leaf helpers so that callers inside this module
# (e.g. _install_apm_dependencies) and any future test patches against
# "apm_cli.commands.install._copy_local_package" keep working.
# _integrate_local_content stays here (not moved) because it calls
# _integrate_package_primitives via bare-name lookup and tests patch
# apm_cli.commands.install._integrate_package_primitives to intercept it.
from apm_cli.install.phases.local_content import (
    _copy_local_package,
    _has_local_apm_content,
    _project_has_root_primitives,
)

# Re-export the pre-deploy security scan so that bare-name call sites inside
# this module and ``tests/unit/test_install_scanning.py``'s direct import
# (``from apm_cli.commands.install import _pre_deploy_security_scan``) keep
# working without modification.
from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan

from ._helpers import (
    _create_minimal_apm_yml,
    _get_default_config,
    _rich_blank_line,
    _update_gitignore_for_apm_modules,
)

# CRITICAL: Shadow Python builtins that share names with Click commands
set = builtins.set
list = builtins.list
dict = builtins.dict

# AuthResolver has no optional deps (stdlib + internal utils only), so it must
# be imported unconditionally here -- NOT inside the APM_DEPS_AVAILABLE guard.
# If it were gated, a missing optional dep (e.g. GitPython) would cause a
# NameError in install() before the graceful APM_DEPS_AVAILABLE check fires.
from ..core.auth import AuthResolver

# APM Dependencies (conditional import for graceful degradation)
APM_DEPS_AVAILABLE = False
_APM_IMPORT_ERROR = None
try:
    from ..deps.apm_resolver import APMDependencyResolver
    from ..deps.github_downloader import GitHubPackageDownloader
    from ..deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed
    from ..integration import AgentIntegrator, PromptIntegrator
    from ..integration.mcp_integrator import MCPIntegrator
    from ..models.apm_package import APMPackage, DependencyReference

    APM_DEPS_AVAILABLE = True
except ImportError as e:
    _APM_IMPORT_ERROR = str(e)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_and_add_packages_to_apm_yml(packages, dry_run=False, dev=False, logger=None, manifest_path=None, auth_resolver=None, scope=None):
    """Validate packages exist and can be accessed, then add to apm.yml dependencies section.

    Implements normalize-on-write: any input form (HTTPS URL, SSH URL, FQDN, shorthand)
    is canonicalized before storage. Default host (github.com) is stripped;
    non-default hosts are preserved. Duplicates are detected by identity.

    Args:
        packages: Package specifiers to validate and add.
        dry_run: If True, only show what would be added.
        dev: If True, write to devDependencies instead of dependencies.
        logger: InstallLogger for structured output.
        manifest_path: Explicit path to apm.yml (defaults to cwd/apm.yml).
        auth_resolver: Shared auth resolver for caching credentials.
        scope: InstallScope controlling project vs user deployment.

    Returns:
        Tuple of (validated_packages list, _ValidationOutcome).
    """
    import subprocess
    import tempfile
    from pathlib import Path

    apm_yml_path = manifest_path or Path(APM_YML_FILENAME)

    # Read current apm.yml
    try:
        from ..utils.yaml_io import load_yaml
        data = load_yaml(apm_yml_path) or {}
    except Exception as e:
        if logger:
            logger.error(f"Failed to read {APM_YML_FILENAME}: {e}")
        else:
            _rich_error(f"Failed to read {APM_YML_FILENAME}: {e}")
        sys.exit(1)

    # Ensure dependencies structure exists
    dep_section = "devDependencies" if dev else "dependencies"
    if dep_section not in data:
        data[dep_section] = {}
    if "apm" not in data[dep_section]:
        data[dep_section]["apm"] = []

    current_deps = data[dep_section]["apm"] or []
    validated_packages = []

    # Build identity set from existing deps for duplicate detection
    existing_identities = builtins.set()
    for dep_entry in current_deps:
        try:
            if isinstance(dep_entry, str):
                ref = DependencyReference.parse(dep_entry)
            elif isinstance(dep_entry, builtins.dict):
                ref = DependencyReference.parse_from_dict(dep_entry)
            else:
                continue
            existing_identities.add(ref.get_identity())
        except (ValueError, TypeError, AttributeError, KeyError):
            continue

    # First, validate all packages
    valid_outcomes = []  # (canonical, already_present) tuples
    invalid_outcomes = []  # (package, reason) tuples
    _marketplace_provenance = {}  # canonical -> {discovered_via, marketplace_plugin_name}

    if logger:
        logger.validation_start(len(packages))

    for package in packages:
        # --- Marketplace pre-parse intercept ---
        # If input has no slash and is not a local path, check if it is a
        # marketplace ref (NAME@MARKETPLACE).  If so, resolve it to a
        # canonical owner/repo[#ref] string before entering the standard
        # parse path.  Anything that doesn't match is rejected as an
        # invalid format.
        marketplace_provenance = None
        if "/" not in package and not DependencyReference.is_local_path(package):
            try:
                from ..marketplace.resolver import (
                    parse_marketplace_ref,
                    resolve_marketplace_plugin,
                )

                mkt_ref = parse_marketplace_ref(package)
            except ImportError:
                mkt_ref = None

            if mkt_ref is not None:
                plugin_name, marketplace_name = mkt_ref
                try:
                    if logger:
                        logger.verbose_detail(
                            f"    Resolving {plugin_name}@{marketplace_name} via marketplace..."
                        )
                    canonical_str, resolved_plugin = resolve_marketplace_plugin(
                        plugin_name,
                        marketplace_name,
                        auth_resolver=auth_resolver,
                    )
                    if logger:
                        logger.verbose_detail(
                            f"    Resolved to: {canonical_str}"
                        )
                    marketplace_provenance = {
                        "discovered_via": marketplace_name,
                        "marketplace_plugin_name": plugin_name,
                    }
                    package = canonical_str
                except Exception as mkt_err:
                    reason = str(mkt_err)
                    invalid_outcomes.append((package, reason))
                    if logger:
                        logger.validation_fail(package, reason)
                    continue
            else:
                # No slash, not a local path, and not a marketplace ref
                reason = "invalid format -- use 'owner/repo' or 'plugin-name@marketplace'"
                invalid_outcomes.append((package, reason))
                if logger:
                    logger.validation_fail(package, reason)
                continue

        # Canonicalize input
        try:
            dep_ref = DependencyReference.parse(package)
            canonical = dep_ref.to_canonical()
            identity = dep_ref.get_identity()
        except ValueError as e:
            reason = str(e)
            invalid_outcomes.append((package, reason))
            if logger:
                logger.validation_fail(package, reason)
            continue

        # Reject local packages at user scope -- relative paths resolve
        # against cwd during validation but against $HOME during copy,
        # causing silent failures.
        if dep_ref.is_local and scope is not None:
            from ..core.scope import InstallScope
            if scope is InstallScope.USER:
                reason = (
                    "local packages are not supported at user scope (--global). "
                    "Use a remote reference (owner/repo) instead"
                )
                invalid_outcomes.append((package, reason))
                if logger:
                    logger.validation_fail(package, reason)
                continue

        # Check if package is already in dependencies (by identity)
        already_in_deps = identity in existing_identities

        # Validate package exists and is accessible
        verbose = bool(logger and logger.verbose)
        if _validate_package_exists(package, verbose=verbose, auth_resolver=auth_resolver, logger=logger):
            valid_outcomes.append((canonical, already_in_deps))
            if logger:
                logger.validation_pass(canonical, already_present=already_in_deps)

            if not already_in_deps:
                validated_packages.append(canonical)
                existing_identities.add(identity)  # prevent duplicates within batch
            if marketplace_provenance:
                _marketplace_provenance[identity] = marketplace_provenance
        else:
            reason = _local_path_failure_reason(dep_ref)
            if not reason:
                reason = "not accessible or doesn't exist"
                if not verbose:
                    reason += " -- run with --verbose for auth details"
            invalid_outcomes.append((package, reason))
            if logger:
                logger.validation_fail(package, reason)

    outcome = _ValidationOutcome(
        valid=valid_outcomes,
        invalid=invalid_outcomes,
        marketplace_provenance=_marketplace_provenance or None,
    )

    # Let the logger emit a summary and decide whether to continue
    if logger:
        should_continue = logger.validation_summary(outcome)
        if not should_continue:
            return [], outcome

    if not validated_packages:
        if dry_run:
            if logger:
                logger.progress("No new packages to add")
        # If all packages already exist in apm.yml, that's OK - we'll reinstall them
        return [], outcome

    if dry_run:
        if logger:
            logger.progress(
                f"Dry run: Would add {len(validated_packages)} package(s) to apm.yml"
            )
            for pkg in validated_packages:
                logger.verbose_detail(f"  + {pkg}")
        return validated_packages, outcome

    # Add validated packages to dependencies (already canonical)
    dep_label = "devDependencies" if dev else "apm.yml"
    for package in validated_packages:
        current_deps.append(package)
        if logger:
            logger.verbose_detail(f"Added {package} to {dep_label}")

    # Update dependencies
    data[dep_section]["apm"] = current_deps

    # Write back to apm.yml
    try:
        from ..utils.yaml_io import dump_yaml
        dump_yaml(data, apm_yml_path)
        if logger:
            logger.success(f"Updated {APM_YML_FILENAME} with {len(validated_packages)} new package(s)")
    except Exception as e:
        if logger:
            logger.error(f"Failed to write {APM_YML_FILENAME}: {e}")
        else:
            _rich_error(f"Failed to write {APM_YML_FILENAME}: {e}")
        sys.exit(1)

    return validated_packages, outcome


# ---------------------------------------------------------------------------
# Install command
# ---------------------------------------------------------------------------


@click.command(
    help="Install APM and MCP dependencies (auto-creates apm.yml when installing packages)"
)
@click.argument("packages", nargs=-1)
@click.option("--runtime", help="Target specific runtime only (copilot, codex, vscode)")
@click.option("--exclude", help="Exclude specific runtime from installation")
@click.option(
    "--only",
    type=click.Choice(["apm", "mcp"]),
    help="Install only specific dependency type",
)
@click.option(
    "--update", is_flag=True, help="Update dependencies to latest Git references"
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be installed without installing"
)
@click.option("--force", is_flag=True, help="Overwrite locally-authored files on collision and deploy despite critical security findings")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed installation information")
@click.option(
    "--trust-transitive-mcp",
    is_flag=True,
    help="Trust self-defined MCP servers from transitive packages (skip re-declaration requirement)",
)
@click.option(
    "--parallel-downloads",
    type=int,
    default=4,
    show_default=True,
    help="Max concurrent package downloads (0 to disable parallelism)",
)
@click.option(
    "--dev",
    is_flag=True,
    default=False,
    help="Install as development dependency (devDependencies)",
)
@click.option(
    "--target",
    "-t",
    "target",
    type=click.Choice(
        ["copilot", "claude", "cursor", "opencode", "codex", "vscode", "agents", "all"],
        case_sensitive=False,
    ),
    default=None,
    help="Force deployment to a specific target (overrides auto-detection)",
)
@click.option(
    "--global", "-g", "global_",
    is_flag=True,
    default=False,
    help="Install to user scope (~/.apm/) instead of the current project",
)
@click.pass_context
def install(ctx, packages, runtime, exclude, only, update, dry_run, force, verbose, trust_transitive_mcp, parallel_downloads, dev, target, global_):
    """Install APM and MCP dependencies from apm.yml (like npm install).

    This command automatically detects AI runtimes from your apm.yml scripts and installs
    MCP servers for all detected and available runtimes. It also installs APM package
    dependencies from GitHub repositories.

    The --only flag filters by dependency type (apm or mcp). Internally converted
    to an InstallMode enum for type-safe dispatch.

    Examples:
        apm install                             # Install existing deps from apm.yml
        apm install org/pkg1                    # Add package to apm.yml and install
        apm install org/pkg1 org/pkg2           # Add multiple packages and install
        apm install --exclude codex             # Install for all except Codex CLI
        apm install --only=apm                  # Install only APM dependencies
        apm install --only=mcp                  # Install only MCP dependencies
        apm install --update                    # Update dependencies to latest Git refs
        apm install --dry-run                   # Show what would be installed
        apm install -g org/pkg1                 # Install to user scope (~/.apm/)
    """
    try:
        # Create structured logger for install output early so exception
        # handlers can always reference it (avoids UnboundLocalError if
        # scope initialisation below throws).
        is_partial = bool(packages)
        logger = InstallLogger(verbose=verbose, dry_run=dry_run, partial=is_partial)

        # Resolve scope
        from ..core.scope import InstallScope, get_apm_dir, get_manifest_path, get_modules_dir, ensure_user_dirs, warn_unsupported_user_scope
        scope = InstallScope.USER if global_ else InstallScope.PROJECT

        if scope is InstallScope.USER:
            ensure_user_dirs()
            logger.progress("Installing to user scope (~/.apm/)")
            _scope_warn = warn_unsupported_user_scope()
            if _scope_warn:
                logger.warning(_scope_warn)

        # Scope-aware paths
        manifest_path = get_manifest_path(scope)
        apm_dir = get_apm_dir(scope)
        # Display name for messages (short for project scope, full for user scope)
        manifest_display = str(manifest_path) if scope is InstallScope.USER else APM_YML_FILENAME

        # Project root for integration (used by both dep and local integration)
        from ..core.scope import get_deploy_root
        project_root = get_deploy_root(scope)

        # Create shared auth resolver for all downloads in this CLI invocation
        # to ensure credentials are cached and reused (prevents duplicate auth popups)
        auth_resolver = AuthResolver()

        # Check if apm.yml exists
        apm_yml_exists = manifest_path.exists()

        # Auto-bootstrap: create minimal apm.yml when packages specified but no apm.yml
        if not apm_yml_exists and packages:
            # Get current directory name as project name
            project_name = Path.cwd().name if scope is InstallScope.PROJECT else Path.home().name
            config = _get_default_config(project_name)
            _create_minimal_apm_yml(config, target_path=manifest_path)
            logger.success(f"Created {manifest_display}")

        # Error when NO apm.yml AND NO packages
        if not apm_yml_exists and not packages:
            logger.error(f"No {manifest_display} found")
            if scope is InstallScope.USER:
                logger.progress("Run 'apm install -g <org/repo>' to auto-create + install")
            else:
                logger.progress("Run 'apm init' to create one, or:")
                logger.progress("  apm install <org/repo> to auto-create + install")
            sys.exit(1)

        # If packages are specified, validate and add them to apm.yml first
        if packages:
            validated_packages, outcome = _validate_and_add_packages_to_apm_yml(
                packages, dry_run, dev=dev, logger=logger,
                manifest_path=manifest_path, auth_resolver=auth_resolver,
                scope=scope,
            )
            # Short-circuit: all packages failed validation -- nothing to install
            if outcome.all_failed:
                return
            # Note: Empty validated_packages is OK if packages are already in apm.yml
            # We'll proceed with installation from apm.yml to ensure everything is synced

        logger.resolution_start(
            to_install_count=len(validated_packages) if packages else 0,
            lockfile_count=0,  # Refined later inside _install_apm_dependencies
        )

        # Parse apm.yml to get both APM and MCP dependencies
        try:
            apm_package = APMPackage.from_apm_yml(manifest_path)
        except Exception as e:
            logger.error(f"Failed to parse {manifest_display}: {e}")
            sys.exit(1)

        logger.verbose_detail(
            f"Parsed {APM_YML_FILENAME}: {len(apm_package.get_apm_dependencies())} APM deps, "
            f"{len(apm_package.get_mcp_dependencies())} MCP deps"
            + (f", {len(apm_package.get_dev_apm_dependencies())} dev deps"
               if apm_package.get_dev_apm_dependencies() else "")
        )

        # Get APM and MCP dependencies
        apm_deps = apm_package.get_apm_dependencies()
        dev_apm_deps = apm_package.get_dev_apm_dependencies()
        has_any_apm_deps = bool(apm_deps) or bool(dev_apm_deps)
        mcp_deps = apm_package.get_mcp_dependencies()

        # Convert --only string to InstallMode enum
        if only is None:
            install_mode = InstallMode.ALL
        else:
            install_mode = InstallMode(only)

        # Determine what to install based on install mode
        should_install_apm = install_mode != InstallMode.MCP
        should_install_mcp = install_mode != InstallMode.APM
        # MCP servers are workspace-scoped (.vscode/mcp.json); skip at user scope
        if scope is InstallScope.USER:
            should_install_mcp = False
            if logger:
                logger.verbose_detail(
                    "MCP servers skipped at user scope (workspace-scoped concept)"
                )

        # Show what will be installed if dry run
        if dry_run:
            from apm_cli.install.presentation.dry_run import render_and_exit

            render_and_exit(
                logger=logger,
                should_install_apm=should_install_apm,
                apm_deps=apm_deps,
                mcp_deps=mcp_deps,
                dev_apm_deps=dev_apm_deps,
                should_install_mcp=should_install_mcp,
                update=update,
                apm_dir=apm_dir,
            )
            return

        # Install APM dependencies first (if requested)
        apm_count = 0
        prompt_count = 0
        agent_count = 0

        # Migrate legacy apm.lock -> apm.lock.yaml if needed (one-time, transparent)
        migrate_lockfile_if_needed(apm_dir)

        # Capture old MCP servers and configs from lockfile BEFORE
        # _install_apm_dependencies regenerates it (which drops the fields).
        # We always read this -- even when --only=apm -- so we can restore the
        # field after the lockfile is regenerated by the APM install step.
        old_mcp_servers: builtins.set = builtins.set()
        old_mcp_configs: builtins.dict = {}
        old_local_deployed: builtins.list = []
        _lock_path = get_lockfile_path(apm_dir)
        _existing_lock = LockFile.read(_lock_path)
        if _existing_lock:
            old_mcp_servers = builtins.set(_existing_lock.mcp_servers)
            old_mcp_configs = builtins.dict(_existing_lock.mcp_configs)
            old_local_deployed = builtins.list(_existing_lock.local_deployed_files)

        # Also enter the APM install path when the project root has local .apm/
        # primitives, even if there are no external APM dependencies (#714).
        from apm_cli.core.scope import get_deploy_root as _get_deploy_root
        _cli_project_root = _get_deploy_root(scope)

        apm_diagnostics = None
        if should_install_apm and (has_any_apm_deps or _project_has_root_primitives(_cli_project_root)):
            if not APM_DEPS_AVAILABLE:
                logger.error("APM dependency system not available")
                logger.progress(f"Import error: {_APM_IMPORT_ERROR}")
                sys.exit(1)

            try:
                # If specific packages were requested, only install those
                # Otherwise install all from apm.yml.
                # Use validated_packages (canonical strings) instead of
                # raw packages (which may contain marketplace refs like
                # NAME@MARKETPLACE that don't match resolved dep identities).
                only_pkgs = builtins.list(validated_packages) if packages else None
                install_result = _install_apm_dependencies(
                    apm_package, update, verbose, only_pkgs, force=force,
                    parallel_downloads=parallel_downloads,
                    logger=logger,
                    scope=scope,
                    auth_resolver=auth_resolver,
                    target=target,
                    marketplace_provenance=(
                        outcome.marketplace_provenance if packages and outcome else None
                    ),
                )
                apm_count = install_result.installed_count
                prompt_count = install_result.prompts_integrated
                agent_count = install_result.agents_integrated
                apm_diagnostics = install_result.diagnostics
            except Exception as e:
                logger.error(f"Failed to install APM dependencies: {e}")
                if not verbose:
                    logger.progress("Run with --verbose for detailed diagnostics")
                sys.exit(1)
        elif should_install_apm and not has_any_apm_deps:
            logger.verbose_detail("No APM dependencies found in apm.yml")

        # When --update is used, package files on disk may have changed.
        # Clear the parse cache so transitive MCP collection reads fresh data.
        if update:
            from apm_cli.models.apm_package import clear_apm_yml_cache
            clear_apm_yml_cache()

        # Collect transitive MCP dependencies from resolved APM packages
        apm_modules_path = get_modules_dir(scope)
        if should_install_mcp and apm_modules_path.exists():
            lock_path = get_lockfile_path(apm_dir)
            transitive_mcp = MCPIntegrator.collect_transitive(
                apm_modules_path, lock_path, trust_transitive_mcp,
                diagnostics=apm_diagnostics,
            )
            if transitive_mcp:
                logger.verbose_detail(f"Collected {len(transitive_mcp)} transitive MCP dependency(ies)")
                mcp_deps = MCPIntegrator.deduplicate(mcp_deps + transitive_mcp)

        # Continue with MCP installation (existing logic)
        mcp_count = 0
        new_mcp_servers: builtins.set = builtins.set()
        if should_install_mcp and mcp_deps:
            mcp_count = MCPIntegrator.install(
                mcp_deps, runtime, exclude, verbose,
                stored_mcp_configs=old_mcp_configs,
                diagnostics=apm_diagnostics,
            )
            new_mcp_servers = MCPIntegrator.get_server_names(mcp_deps)
            new_mcp_configs = MCPIntegrator.get_server_configs(mcp_deps)

            # Remove stale MCP servers that are no longer needed
            stale_servers = old_mcp_servers - new_mcp_servers
            if stale_servers:
                MCPIntegrator.remove_stale(stale_servers, runtime, exclude)

            # Persist the new MCP server set and configs in the lockfile
            MCPIntegrator.update_lockfile(new_mcp_servers, mcp_configs=new_mcp_configs)
        elif should_install_mcp and not mcp_deps:
            # No MCP deps at all -- remove any old APM-managed servers
            if old_mcp_servers:
                MCPIntegrator.remove_stale(old_mcp_servers, runtime, exclude)
                MCPIntegrator.update_lockfile(builtins.set(), mcp_configs={})
            logger.verbose_detail("No MCP dependencies found in apm.yml")
        elif not should_install_mcp and old_mcp_servers:
            # --only=apm: APM install regenerated the lockfile and dropped
            # mcp_servers.  Restore the previous set so it is not lost.
            MCPIntegrator.update_lockfile(old_mcp_servers, mcp_configs=old_mcp_configs)

        # --- Local .apm/ content integration ---
        # Deploy primitives from the project's own .apm/ folder to target
        # directories, just like dependency primitives.  Runs AFTER deps so
        # local content wins on collision.
        if (
            should_install_apm
            and scope is InstallScope.PROJECT
            and not dry_run
            and (_has_local_apm_content(project_root) or old_local_deployed)
        ):
            try:
                from apm_cli.integration.targets import resolve_targets as _local_resolve
                from apm_cli.integration.skill_integrator import SkillIntegrator
                from apm_cli.integration.command_integrator import CommandIntegrator
                from apm_cli.integration.hook_integrator import HookIntegrator
                from apm_cli.integration.instruction_integrator import InstructionIntegrator
                from apm_cli.integration.base_integrator import BaseIntegrator
                from apm_cli.deps.lockfile import LockFile as _LocalLF, get_lockfile_path as _local_lf_path
                from apm_cli.integration import AgentIntegrator as _AgentInt, PromptIntegrator as _PromptInt

                # Resolve targets (same precedence as _install_apm_dependencies)
                _local_config_target = apm_package.target
                _local_explicit = target or _local_config_target or None
                _local_targets = _local_resolve(
                    project_root, user_scope=False, explicit_target=_local_explicit,
                )

                if _local_targets:
                    # Build managed_files: dep-deployed files + previous local
                    # deployed files.  This ensures local content wins
                    # collisions with deps and previous local files are not
                    # treated as user-authored content.
                    _local_managed = builtins.set()
                    _local_lock_path = _local_lf_path(apm_dir)
                    _local_lock = _LocalLF.read(_local_lock_path)
                    if _local_lock:
                        for dep in _local_lock.dependencies.values():
                            _local_managed.update(dep.deployed_files)
                    # Include previous local deployed files so re-deploys
                    # overwrite rather than skip.
                    _local_managed.update(old_local_deployed)
                    _local_managed = BaseIntegrator.normalize_managed_files(_local_managed)

                    # Create integrators
                    _local_diagnostics = apm_diagnostics or DiagnosticCollector(verbose=verbose)
                    _errors_before_local = _local_diagnostics.error_count
                    _local_prompt_int = _PromptInt()
                    _local_agent_int = _AgentInt()
                    _local_skill_int = SkillIntegrator()
                    _local_instr_int = InstructionIntegrator()
                    _local_cmd_int = CommandIntegrator()
                    _local_hook_int = HookIntegrator()

                    logger.verbose_detail("Integrating local .apm/ content...")

                    local_int_result = _integrate_local_content(
                        project_root,
                        targets=_local_targets,
                        prompt_integrator=_local_prompt_int,
                        agent_integrator=_local_agent_int,
                        skill_integrator=_local_skill_int,
                        instruction_integrator=_local_instr_int,
                        command_integrator=_local_cmd_int,
                        hook_integrator=_local_hook_int,
                        force=force,
                        managed_files=_local_managed,
                        diagnostics=_local_diagnostics,
                        logger=logger,
                        scope=scope,
                    )

                    # Track what local integration deployed
                    _local_deployed = local_int_result.get("deployed_files", [])
                    _local_total = sum(
                        local_int_result.get(k, 0)
                        for k in ("prompts", "agents", "skills", "sub_skills",
                                  "instructions", "commands", "hooks")
                    )

                    if _local_total > 0:
                        logger.verbose_detail(
                            f"Deployed {_local_total} local primitive(s) from .apm/"
                        )

                    # Stale cleanup: remove files deployed by previous local
                    # integration that are no longer produced.  Only run when
                    # integration completed without errors to avoid deleting
                    # files that failed to re-deploy.
                    _local_had_errors = (
                        _local_diagnostics is not None
                        and _local_diagnostics.error_count > _errors_before_local
                    )
                    if old_local_deployed and not _local_had_errors:
                        from ..integration.cleanup import remove_stale_deployed_files as _rmstale
                        _stale = builtins.set(old_local_deployed) - builtins.set(_local_deployed)
                        if _stale:
                            _local_prev_hashes = {}
                            _prev_local_lf = _LocalLF.read(_local_lock_path)
                            if _prev_local_lf:
                                _local_prev_hashes = dict(
                                    _prev_local_lf.local_deployed_file_hashes
                                )
                            _cleanup_result = _rmstale(
                                _stale, project_root,
                                dep_key="<local .apm/>",
                                targets=_local_targets,
                                diagnostics=_local_diagnostics,
                                recorded_hashes=_local_prev_hashes,
                            )
                            # Failed paths stay in lockfile so we retry next time.
                            _local_deployed.extend(_cleanup_result.failed)
                            if _cleanup_result.deleted_targets:
                                BaseIntegrator.cleanup_empty_parents(
                                    _cleanup_result.deleted_targets, project_root
                                )
                            for _skipped in _cleanup_result.skipped_user_edit:
                                logger.cleanup_skipped_user_edit(
                                    _skipped, "<local .apm/>"
                                )
                            logger.stale_cleanup(
                                "<local .apm/>", len(_cleanup_result.deleted)
                            )

                    # Persist local_deployed_files (and hashes) in the lockfile
                    _persist_lock = _LocalLF.read(_local_lock_path) or _LocalLF()
                    _persist_lock.local_deployed_files = sorted(_local_deployed)
                    _persist_lock.local_deployed_file_hashes = _hash_deployed(
                        _local_deployed, project_root
                    )
                    # Only write if changed
                    _existing_for_cmp = _LocalLF.read(_local_lock_path)
                    if not _existing_for_cmp or not _persist_lock.is_semantically_equivalent(_existing_for_cmp):
                        _persist_lock.save(_local_lock_path)

                    # Ensure diagnostics flow into the final summary
                    if apm_diagnostics is None:
                        apm_diagnostics = _local_diagnostics

            except Exception as e:
                logger.verbose_detail(f"Local .apm/ integration failed: {e}")
                if apm_diagnostics:
                    apm_diagnostics.error(f"Local .apm/ integration failed: {e}")

        # Show diagnostics and final install summary
        if apm_diagnostics and apm_diagnostics.has_diagnostics:
            apm_diagnostics.render_summary()
        else:
            _rich_blank_line()

        error_count = 0
        if apm_diagnostics:
            try:
                error_count = int(apm_diagnostics.error_count)
            except (TypeError, ValueError):
                error_count = 0
        logger.install_summary(
            apm_count=apm_count,
            mcp_count=mcp_count,
            errors=error_count,
            stale_cleaned=logger.stale_cleaned_total,
        )

        # Hard-fail when critical security findings blocked any package.
        # Consistent with apm unpack which also hard-fails on critical.
        # Use --force to override.
        if not force and apm_diagnostics and apm_diagnostics.has_critical_security:
            sys.exit(1)

    except Exception as e:
        logger.error(f"Error installing dependencies: {e}")
        if not verbose:
            logger.progress("Run with --verbose for detailed diagnostics")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Install engine
# ---------------------------------------------------------------------------


def _integrate_package_primitives(
    package_info,
    project_root,
    *,
    targets,
    prompt_integrator,
    agent_integrator,
    skill_integrator,
    instruction_integrator,
    command_integrator,
    hook_integrator,
    force,
    managed_files,
    diagnostics,
    package_name="",
    logger=None,
    scope=None,
):
    """Run the full integration pipeline for a single package.

    Iterates over *targets* (``TargetProfile`` list) and dispatches each
    primitive to the appropriate integrator via the target-driven API.
    Skills are handled separately because ``SkillIntegrator`` already
    routes across all targets internally.

    When *scope* is ``InstallScope.USER``, targets and primitives that
    do not support user-scope deployment are silently skipped.

    Returns a dict with integration counters and the list of deployed file paths.
    """
    from apm_cli.integration.dispatch import get_dispatch_table

    _dispatch = get_dispatch_table()
    result = {
        "prompts": 0,
        "agents": 0,
        "skills": 0,
        "sub_skills": 0,
        "instructions": 0,
        "commands": 0,
        "hooks": 0,
        "links_resolved": 0,
        "deployed_files": [],
    }

    deployed = result["deployed_files"]

    if not targets:
        return result

    def _log_integration(msg):
        if logger:
            logger.tree_item(msg)

    # Map integrator kwargs to dispatch table keys
    _INTEGRATOR_KWARGS = {
        "prompts": prompt_integrator,
        "agents": agent_integrator,
        "commands": command_integrator,
        "instructions": instruction_integrator,
        "hooks": hook_integrator,
        "skills": skill_integrator,
    }

    # --- per-target dispatch loop ---
    for _target in targets:
        for _prim_name, _mapping in _target.primitives.items():
            _entry = _dispatch.get(_prim_name)
            if not _entry or _entry.multi_target:
                continue  # skills handled below

            _integrator = _INTEGRATOR_KWARGS[_prim_name]
            _int_result = getattr(_integrator, _entry.integrate_method)(
                _target, package_info, project_root,
                force=force, managed_files=managed_files,
                diagnostics=diagnostics,
            )

            if _int_result.files_integrated > 0:
                result[_entry.counter_key] += _int_result.files_integrated
                _effective_root = _mapping.deploy_root or _target.root_dir
                _deploy_dir = f"{_effective_root}/{_mapping.subdir}/" if _mapping.subdir else f"{_effective_root}/"
                # Determine display label
                if _prim_name == "instructions" and _mapping.format_id in ("cursor_rules", "claude_rules"):
                    _label = "rule(s)"
                elif _prim_name == "instructions":
                    _label = "instruction(s)"
                elif _prim_name == "hooks":
                    if _target.name == "claude":
                        _deploy_dir = ".claude/settings.json"
                    elif _target.name == "cursor":
                        _deploy_dir = ".cursor/hooks.json"
                    elif _target.name == "codex":
                        _deploy_dir = ".codex/hooks.json"
                    _label = "hook(s)"
                else:
                    _label = _prim_name
                _log_integration(
                    f"  |-- {_int_result.files_integrated} {_label} integrated -> {_deploy_dir}"
                )
            result["links_resolved"] += _int_result.links_resolved
            for tp in _int_result.target_paths:
                deployed.append(tp.relative_to(project_root).as_posix())

    # --- skills (multi-target, handled by SkillIntegrator internally) ---
    skill_result = skill_integrator.integrate_package_skill(
        package_info, project_root,
        diagnostics=diagnostics, managed_files=managed_files, force=force,
        targets=targets,
    )
    _skill_target_dirs: set[str] = builtins.set()
    for tp in skill_result.target_paths:
        rel = tp.relative_to(project_root)
        if rel.parts:
            _skill_target_dirs.add(rel.parts[0])
    _skill_targets = sorted(_skill_target_dirs)
    _skill_target_str = ", ".join(f"{d}/skills/" for d in _skill_targets) or "skills/"
    if skill_result.skill_created:
        result["skills"] += 1
        _log_integration(f"  |-- Skill integrated -> {_skill_target_str}")
    if skill_result.sub_skills_promoted > 0:
        result["sub_skills"] += skill_result.sub_skills_promoted
        _log_integration(f"  |-- {skill_result.sub_skills_promoted} skill(s) integrated -> {_skill_target_str}")
    for tp in skill_result.target_paths:
        deployed.append(tp.relative_to(project_root).as_posix())

    return result


def _integrate_local_content(
    project_root,
    *,
    targets,
    prompt_integrator,
    agent_integrator,
    skill_integrator,
    instruction_integrator,
    command_integrator,
    hook_integrator,
    force,
    managed_files,
    diagnostics,
    logger=None,
    scope=None,
):
    """Integrate primitives from the project's own .apm/ directory.

    This treats the project root as a synthetic package so that local
    skills, instructions, agents, prompts, hooks, and commands in .apm/
    are deployed to target directories exactly like dependency primitives.

    Only .apm/ sub-directories are processed.  A root-level SKILL.md is
    intentionally ignored (it describes the project itself, not a
    deployable skill).

    Returns a dict with integration counters and deployed file paths,
    same shape as ``_integrate_package_primitives()``.
    """
    from ..models.apm_package import APMPackage, PackageInfo, PackageType

    # Build a lightweight synthetic PackageInfo rooted at the project.
    # package_type=APM_PACKAGE prevents SkillIntegrator from treating
    # a root SKILL.md as a native skill to deploy.
    local_pkg = APMPackage(
        name="_local",
        version="0.0.0",
        package_path=project_root,
        source="local",
    )
    local_info = PackageInfo(
        package=local_pkg,
        install_path=project_root,
        package_type=PackageType.APM_PACKAGE,
    )

    return _integrate_package_primitives(
        local_info,
        project_root,
        targets=targets,
        prompt_integrator=prompt_integrator,
        agent_integrator=agent_integrator,
        skill_integrator=skill_integrator,
        instruction_integrator=instruction_integrator,
        command_integrator=command_integrator,
        hook_integrator=hook_integrator,
        force=force,
        managed_files=managed_files,
        diagnostics=diagnostics,
        package_name="_local",
        logger=logger,
        scope=scope,
    )


# ---------------------------------------------------------------------------
# Pipeline entry point -- thin re-export preserving the patch path
# ``apm_cli.commands.install._install_apm_dependencies`` used by tests.
#
# The real implementation lives in ``apm_cli.install.pipeline`` (F2).
# ---------------------------------------------------------------------------
def _install_apm_dependencies(
    apm_package: "APMPackage",
    update_refs: bool = False,
    verbose: bool = False,
    only_packages: "builtins.list" = None,
    force: bool = False,
    parallel_downloads: int = 4,
    logger: "InstallLogger" = None,
    scope=None,
    auth_resolver: "AuthResolver" = None,
    target: str = None,
    marketplace_provenance: dict = None,
):
    """Thin wrapper -- delegates to :func:`apm_cli.install.pipeline.run_install_pipeline`.

    Kept here so that ``@patch("apm_cli.commands.install._install_apm_dependencies")``
    continues to intercept calls from the Click handler.
    """
    if not APM_DEPS_AVAILABLE:
        raise RuntimeError("APM dependency system not available")

    from apm_cli.install.pipeline import run_install_pipeline

    return run_install_pipeline(
        apm_package,
        update_refs=update_refs,
        verbose=verbose,
        only_packages=only_packages,
        force=force,
        parallel_downloads=parallel_downloads,
        logger=logger,
        scope=scope,
        auth_resolver=auth_resolver,
        target=target,
        marketplace_provenance=marketplace_provenance,
    )




