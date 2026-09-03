"""LSP server integration for the APM install pipeline.

Mirrors the MCP integration pattern with runtime-neutral target selection.
"""

import builtins
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.core.target_detection import EffectiveTargetDecision
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.models.dependency.lsp import LSPDependency

_PROJECT_LSP_OWNER = "project:."
_PACKAGE_LSP_OWNER_PREFIX = "package:"
_BUNDLE_LSP_OWNER_PREFIX = "bundle:"
_RESERVED_LSP_OWNER_PREFIXES = (
    _PROJECT_LSP_OWNER.split(".", 1)[0],
    _PACKAGE_LSP_OWNER_PREFIX,
    _BUNDLE_LSP_OWNER_PREFIX,
)


def _target_server_sets(lockfile: "LockFile") -> dict[str, set[str]]:
    """Return a mutable target ownership view from one lockfile."""
    target_servers = getattr(lockfile, "lsp_target_servers", {})
    if not isinstance(target_servers, dict):
        return {}
    return {runtime: set(server_names) for runtime, server_names in target_servers.items()}


def _dependency_provenance(dependencies: list["LSPDependency"]) -> dict[str, str]:
    """Map every regular install declaration to its stable owner token."""
    return {
        dependency.name: (
            f"{_PACKAGE_LSP_OWNER_PREFIX}{dependency.resolved_by}"
            if dependency.resolved_by
            else _PROJECT_LSP_OWNER
        )
        for dependency in dependencies
    }


def _is_regular_owner(owner: str) -> bool:
    """Return whether an LSP owner belongs to the replayable install graph."""
    return owner == _PROJECT_LSP_OWNER or owner.startswith(_PACKAGE_LSP_OWNER_PREFIX)


def _bundle_owner_aliases(owner: str) -> frozenset[str]:
    """Return canonical and safe legacy provenance tokens for one bundle."""
    canonical = f"{_BUNDLE_LSP_OWNER_PREFIX}{owner}"
    if owner.startswith(_RESERVED_LSP_OWNER_PREFIXES):
        return frozenset({canonical})
    return frozenset({canonical, owner})


def _clean_target_differences(
    *,
    old_targets: dict[str, set[str]],
    new_targets: dict[str, set[str]],
    project_root: Path,
    user_scope: bool,
    logger,
    fail_on_write_error: bool,
) -> None:
    """Remove only target-scoped LSP entries whose recorded ownership was dropped."""
    from apm_cli.integration.lsp_integrator import LSPIntegrator

    for runtime, old_names in old_targets.items():
        stale = old_names - new_targets.get(runtime, set())
        if stale:
            LSPIntegrator.remove_stale(
                stale,
                project_root=project_root,
                user_scope=user_scope,
                logger=logger,
                target_runtimes=[runtime],
                fail_on_write_error=fail_on_write_error,
            )


def run_owned_lsp_integration(
    *,
    dependencies: list["LSPDependency"],
    owner: str,
    lock_path: Path,
    project_root: Path,
    user_scope: bool,
    target_runtimes: list[str],
    logger,
    fail_on_write_error: bool = True,
    force: bool = False,
) -> int:
    """Reconcile one bundle owner's LSP servers and persist ownership."""
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.lsp_integrator import LSPIntegrator

    lockfile = LockFile.read(lock_path) or LockFile()
    owner_token = f"{_BUNDLE_LSP_OWNER_PREFIX}{owner}"
    owner_aliases = _bundle_owner_aliases(owner)
    old_owned = {
        name
        for name, recorded_owner in lockfile.lsp_config_provenance.items()
        if recorded_owner in owner_aliases
    }
    new_names = LSPIntegrator.get_server_names(dependencies)
    conflicts = {
        name
        for name in new_names
        if name in lockfile.lsp_servers
        and name not in old_owned
        and lockfile.lsp_config_provenance.get(name) not in owner_aliases
    }
    if conflicts:
        conflict_details = ", ".join(
            f"{name} (owned by {lockfile.lsp_config_provenance.get(name, 'legacy lock state')})"
            for name in sorted(conflicts)
        )
        raise ValueError(
            "Bundle LSP server name conflicts with another owner: "
            f"{conflict_details}. Rename the declaration or remove and reinstall "
            "the owning bundle; --force does not transfer ownership."
        )

    if not dependencies and not old_owned:
        return 0

    old_targets = _target_server_sets(lockfile)
    new_targets = {runtime: set(names) for runtime, names in old_targets.items()}
    for names in new_targets.values():
        names.difference_update(old_owned)
    supported_target_runtimes = LSPIntegrator.supported_target_runtimes(target_runtimes)
    if dependencies and not supported_target_runtimes:
        from apm_cli.install.errors import RequiredIntegrationError

        raise RequiredIntegrationError(
            "Bundle lsp.json cannot be configured because the resolved target set has "
            "no LSP-compatible runtime. Select --target claude or --target copilot."
        )
    for runtime in supported_target_runtimes:
        new_targets.setdefault(runtime, set()).update(new_names)

    count = 0
    if dependencies:
        count = LSPIntegrator.install(
            dependencies,
            project_root=project_root,
            user_scope=user_scope,
            logger=logger,
            target_runtimes=supported_target_runtimes,
            fail_on_write_error=fail_on_write_error,
            managed_target_servers=old_targets,
            force=force,
        )
    _clean_target_differences(
        old_targets=old_targets,
        new_targets=new_targets,
        project_root=project_root,
        user_scope=user_scope,
        logger=logger,
        fail_on_write_error=fail_on_write_error,
    )

    for name in old_owned:
        lockfile.lsp_configs.pop(name, None)
        lockfile.lsp_config_provenance.pop(name, None)
    lockfile.lsp_servers = sorted((set(lockfile.lsp_servers) - old_owned) | new_names)
    lockfile.lsp_configs.update(LSPIntegrator.get_server_configs(dependencies))
    lockfile.lsp_config_provenance.update(dict.fromkeys(new_names, owner_token))
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    DeploymentLedgerCodec.replace_lsp_target_servers(
        lockfile,
        {
            runtime: sorted(server_names)
            for runtime, server_names in new_targets.items()
            if server_names
        },
    )
    lockfile.write(lock_path)
    return count


def reconcile_lsp_after_uninstall(
    *,
    apm_package: "APMPackage",
    lockfile: "LockFile | None",
    lock_path: Path,
    modules_dir: Path,
    project_root: Path,
    user_scope: bool,
    logger,
) -> bool:
    """Recompute trusted LSP state from every surviving declaration."""
    if lockfile is None or not (
        lockfile.lsp_servers or lockfile.lsp_target_servers or lockfile.lsp_config_provenance
    ):
        return False
    if apm_package is None:
        raise ValueError("Cannot reconcile existing LSP state without a valid project manifest")
    before = (
        list(lockfile.lsp_servers),
        dict(lockfile.lsp_configs),
        dict(lockfile.lsp_target_servers),
        dict(lockfile.lsp_config_provenance),
    )
    from apm_cli.core.scope import InstallScope
    from apm_cli.core.target_detection import resolve_package_target_decision
    from apm_cli.models.apm_package import canonical_package_target_config

    scope = InstallScope.USER if user_scope else InstallScope.PROJECT
    target_decision = resolve_package_target_decision(
        project_root,
        package=apm_package,
        explicit_target=None,
        user_scope=user_scope,
    )
    apm_config = {"scripts": apm_package.scripts or {}}
    apm_config.update(canonical_package_target_config(apm_package))
    run_lsp_integration(
        apm_package=apm_package,
        apm_modules_path=modules_dir,
        lock_path=lock_path,
        existing_lock=lockfile,
        project_root=project_root,
        user_scope=user_scope,
        should_install=True,
        logger=logger,
        target_context=(apm_config, target_decision.value, scope),
        target_decision=target_decision,
        fail_on_write_error=True,
        persist=False,
    )
    after = (
        list(lockfile.lsp_servers),
        dict(lockfile.lsp_configs),
        dict(lockfile.lsp_target_servers),
        dict(lockfile.lsp_config_provenance),
    )
    return before != after


def run_lsp_integration(  # noqa: PLR0913
    *,
    apm_package: "APMPackage",
    apm_modules_path: Path,
    lock_path: Path,
    existing_lock: "LockFile | None",
    project_root: Path,
    user_scope: bool,
    should_install: bool,
    logger,
    diagnostics=None,
    runtime: str | None = None,
    exclude: str | None = None,
    apm_config: dict | None = None,
    explicit_target: str | list[str] | None = None,
    scope=None,
    target_context: tuple[dict | None, str | list[str] | None, object] | None = None,
    target_decision: "EffectiveTargetDecision | None" = None,
    fail_on_write_error: bool = False,
    effective_allow_executables: dict[str, dict[str, bool]] | None = None,
    effective_allow_resolved: bool = False,
    force: bool = False,
    no_policy: bool = False,
    persist: bool = True,
) -> int:
    """Run LSP server integration after APM package installation.

    Mirrors the MCP integration pattern:
    1. Collect direct + transitive LSP deps
    2. Deduplicate (first occurrence wins)
    3. Resolve runtime targets
    4. Install to each target's LSP config
    5. Clean up stale servers
    6. Update lockfile

    Args:
        apm_package: Root APM package with LSP deps.
        apm_modules_path: Path to apm_modules directory.
        lock_path: Path to apm.lock.yaml.
        existing_lock: Previously loaded lockfile (for old LSP state).
        project_root: Project root directory.
        user_scope: If True, write to user-scope runtime config paths.
        should_install: Whether LSP integration should run (same gate as MCP).
        logger: Install logger instance.
        diagnostics: Optional DiagnosticCollector.
        runtime: Optional runtime override.
        exclude: Optional runtime exclusion.
        apm_config: Parsed apm.yml target metadata for project-scope gating.
        explicit_target: Explicit target selected by CLI or manifest.
        scope: Optional InstallScope for user/project filtering.
        target_context: Compact `(apm_config, explicit_target, scope)` tuple
            used by the install command to keep entry-point glue small.

    Returns:
        Number of LSP servers configured.
    """
    from apm_cli.integration.lsp_integrator import LSPIntegrator

    lsp_deps = apm_package.get_lsp_dependencies()
    if not isinstance(lsp_deps, list):
        logger.verbose_detail("LSP dependencies were not a list; defaulting to empty")
        lsp_deps = []

    # Capture old LSP servers from lockfile
    old_lsp_servers: builtins.set = builtins.set()
    old_lsp_configs: builtins.dict = {}
    old_lsp_provenance: dict[str, str] = {}
    old_lsp_targets: dict[str, set[str]] = {}
    old_lsp_targets_present = False
    if existing_lock:
        old_lsp_servers = builtins.set(existing_lock.lsp_servers)
        old_lsp_configs = builtins.dict(existing_lock.lsp_configs)
        raw_provenance = getattr(existing_lock, "lsp_config_provenance", {})
        old_lsp_provenance = dict(raw_provenance) if isinstance(raw_provenance, dict) else {}
        old_lsp_targets = _target_server_sets(existing_lock)
        old_lsp_targets_present = (
            getattr(existing_lock, "_lsp_target_servers_present", False) is True
        )

    from apm_cli.security.executables import filter_lsp_by_allow_executables

    # Filter transitive declarations before first-wins deduplication so an
    # untrusted package cannot shadow an approved package's same-name server.
    if should_install and apm_modules_path.exists():
        transitive_lsp = LSPIntegrator.collect_transitive(
            apm_modules_path,
            lock_path,
            diagnostics=diagnostics,
        )
        if transitive_lsp:
            logger.verbose_detail(f"Collected {len(transitive_lsp)} transitive LSP dependency(ies)")
            if not effective_allow_resolved:
                from apm_cli.security.executables import effective_exec_map_for_project

                policy = None
                if not no_policy:
                    from apm_cli.policy.discovery import discover_policy_with_chain

                    policy = getattr(
                        discover_policy_with_chain(project_root),
                        "policy",
                        None,
                    )
                effective_allow_executables = effective_exec_map_for_project(
                    project_root,
                    policy=policy,
                    fallback_allow_executables=getattr(
                        apm_package,
                        "allow_executables",
                        None,
                    ),
                    logger=logger,
                )
                effective_allow_resolved = True
            transitive_lsp = filter_lsp_by_allow_executables(
                transitive_lsp,
                effective_allow_executables,
                logger,
            )
            lsp_deps = LSPIntegrator.deduplicate(lsp_deps + transitive_lsp)

    if should_install and not (
        lsp_deps or old_lsp_servers or old_lsp_provenance or old_lsp_targets
    ):
        logger.verbose_detail("No LSP dependencies found in apm.yml")
        return 0

    lsp_count = 0

    if target_context is not None:
        apm_config, explicit_target, scope = target_context

    target_runtimes = None
    if should_install and (lsp_deps or old_lsp_servers):
        target_runtimes = LSPIntegrator.resolve_target_runtimes(
            project_root=project_root,
            user_scope=user_scope,
            runtime=runtime,
            exclude=exclude,
            apm_config=apm_config,
            explicit_target=explicit_target,
            target_decision=target_decision,
            scope=scope,
            logger=logger,
        )

    if should_install:
        bundle_names = {
            name for name, owner in old_lsp_provenance.items() if not _is_regular_owner(owner)
        }
        old_regular_names = {
            name for name, owner in old_lsp_provenance.items() if _is_regular_owner(owner)
        }
        if old_lsp_targets_present:
            old_regular_names.update(set().union(*old_lsp_targets.values(), set()) - bundle_names)
        new_regular_names = LSPIntegrator.get_server_names(lsp_deps) if lsp_deps else builtins.set()
        conflicts = new_regular_names & bundle_names
        if conflicts:
            conflict_details = ", ".join(
                f"{name} (owned by {old_lsp_provenance[name]})" for name in sorted(conflicts)
            )
            raise ValueError(
                "Manifest LSP server name conflicts with an installed bundle owner: "
                f"{conflict_details}. Rename the declaration or remove and reinstall "
                "the owning bundle; --force does not transfer ownership."
            )

        if lsp_deps:
            if not target_runtimes and fail_on_write_error:
                from apm_cli.install.errors import RequiredIntegrationError

                raise RequiredIntegrationError(
                    "LSP dependencies are declared, but no effective target supports "
                    "LSP configuration. Choose --target claude or --target copilot, then retry."
                )
            lsp_count = LSPIntegrator.install(
                lsp_deps,
                project_root=project_root,
                user_scope=user_scope,
                logger=logger,
                diagnostics=diagnostics,
                target_runtimes=target_runtimes,
                fail_on_write_error=fail_on_write_error,
                managed_target_servers=old_lsp_targets,
                force=force,
            )

        new_targets = {
            runtime_name: set(server_names)
            for runtime_name, server_names in old_lsp_targets.items()
        }
        for server_names in new_targets.values():
            server_names.difference_update(old_regular_names)
        for target_runtime in target_runtimes or []:
            new_targets.setdefault(target_runtime, set()).update(new_regular_names)
        if old_lsp_targets_present:
            _clean_target_differences(
                old_targets=old_lsp_targets,
                new_targets=new_targets,
                project_root=project_root,
                user_scope=user_scope,
                logger=logger,
                fail_on_write_error=fail_on_write_error,
            )

        new_regular_configs = LSPIntegrator.get_server_configs(lsp_deps)
        new_configs = {
            name: config for name, config in old_lsp_configs.items() if name in bundle_names
        }
        new_configs.update(new_regular_configs)
        new_provenance = {
            name: owner for name, owner in old_lsp_provenance.items() if name in bundle_names
        }
        new_provenance.update(_dependency_provenance(lsp_deps))
        all_names = bundle_names | new_regular_names
        LSPIntegrator.update_lockfile(
            all_names,
            lock_path,
            lsp_configs=new_configs,
            lsp_target_servers=new_targets,
            lsp_config_provenance=new_provenance,
            lockfile_state=existing_lock if not persist else None,
            persist=persist,
            fail_on_write_error=fail_on_write_error,
        )
        if not lsp_deps:
            logger.verbose_detail("No LSP dependencies found in apm.yml")

    elif old_lsp_servers:
        # Selective APM or MCP installs preserve every LSP ownership view.
        LSPIntegrator.update_lockfile(
            old_lsp_servers,
            lock_path,
            lsp_configs=old_lsp_configs,
            lsp_target_servers=old_lsp_targets,
            lsp_config_provenance=old_lsp_provenance,
            lockfile_state=existing_lock if not persist else None,
            persist=persist,
            fail_on_write_error=fail_on_write_error,
        )

    return lsp_count
