"""MCP server integration for the APM install pipeline.

Mirrors the LSP integration pattern (see ``apm_cli.install.lsp.integration``)
with runtime-neutral target selection.
"""

import builtins
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.deps.lockfile_write_authority import lockfile_exists

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.target_detection import EffectiveTargetDecision
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.mcp.entry_integration import McpEntryRequest
    from apm_cli.integration.mcp_config_view import CurrentMcpConfigView
    from apm_cli.integration.mcp_integrator_install import ResolvedMcpTargets
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.models.dependency.mcp import MCPDependency
    from apm_cli.utils.diagnostics import DiagnosticCollector


@dataclass(frozen=True)
class PreparedMcpIntegration:
    """Selected comparison view, filtered declarations, and omission candidates."""

    dependencies: list["MCPDependency"]
    current_view: "CurrentMcpConfigView | None"
    omission_view: "CurrentMcpConfigView | None"

    def omissions(self, lockfile: "LockFile") -> dict[str, dict[str, str]] | None:
        """Evidence against the lock this run produced: a kept baseline supersedes it."""
        if self.omission_view is None:
            return None
        return self.omission_view.omission_evidence(lockfile)


def prepare_mcp_integration(  # noqa: PLR0913
    *,
    apm_package: "APMPackage",
    mcp_deps: list["MCPDependency"],
    apm_modules_path: Path,
    lock_path: Path,
    should_install: bool,
    trust_transitive_mcp: bool,
    logger: "InstallLogger",
    diagnostics: "DiagnosticCollector | None" = None,
    allow_pending: bool | None = None,
    active_package_keys: frozenset[str] | None = None,
    user_scope: bool,
    project_root: Path | None = None,
    executable_context: tuple[dict | None, bool] = (None, False),
    no_policy: bool = False,
    claude_native_keys: frozenset[str] = frozenset(),
    prospective_lock: "LockFile | None" = None,
) -> PreparedMcpIntegration:
    """Collect and admit once, without native writes or ownership adoption.

    Every dependency-declared server is admitted by the security/trust.py
    owner inside ``CurrentMcpConfigView.select``; parked servers carry their
    omission evidence on the same view. *prospective_lock* names the package
    set an install is about to commit, before ``apm.lock.yaml`` is written.
    """
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.mcp_config_view import CurrentMcpConfigView
    from apm_cli.security.trust import McpAdmission

    # McpAdmission reads grants from the scope's manifest (APM_HOME/apm.yml
    # under -g, #3954) and discovers policy from the deploy root.
    project = project_root or apm_package.package_path or Path.cwd()
    allow_map, _resolved = executable_context
    admission = McpAdmission.for_project(
        project,
        user_scope=user_scope,
        one_off=trust_transitive_mcp,
        no_policy=no_policy,
        logger=logger,
        trust_context=getattr(allow_map, "trust_context", None),
    )
    current_view = None
    lockfile = prospective_lock
    if lockfile is None and lockfile_exists(lock_path):
        lockfile = LockFile.read(lock_path)
    if should_install or mcp_deps or apm_modules_path.exists() or lockfile is not None:
        # This run records new omission evidence, so none is verified here.
        current_view = CurrentMcpConfigView.select(
            apm_package,
            lockfile,
            apm_modules_path,
            admission=admission,
            recorded=None,
            diagnostics=diagnostics,
            logger=logger,
            allow_pending=allow_pending,
            active_package_keys=active_package_keys,
            include_dependency_mcp=should_install,
            claude_native_keys=claude_native_keys,
        )
    if should_install and current_view is not None:
        root_count = len(apm_package.get_all_mcp_dependencies())
        transitive_count = max(0, len(current_view.dependencies) - root_count)
        if transitive_count:
            logger.verbose_detail(f"Collected {transitive_count} transitive MCP dependency(ies)")
        mcp_deps = list(current_view.dependencies)
        warn_one_off_trust(current_view.one_off_admitted, logger)
    return PreparedMcpIntegration(
        mcp_deps,
        current_view if should_install else None,
        current_view,
    )


def warn_one_off_trust(one_off: tuple[tuple[str, str], ...], logger: "InstallLogger") -> None:
    """Say that --trust-transitive-mcp consent lasts for this run only."""
    if not one_off:
        return
    noun = "server" if len(one_off) == 1 else "servers"
    shown = "; ".join(f"{name}, {package}" for name, package in one_off[:3])
    more = f"; and {len(one_off) - 3} more" if len(one_off) > 3 else ""
    first_package = one_off[0][1]
    logger.warning(
        f"--trust-transitive-mcp deployed {len(one_off)} unapproved MCP {noun} for this run "
        f"only ({shown}{more})\n"
        "    It is not saved: the next install without the flag removes it. "
        f"Keep it: apm approve {first_package}",
        symbol="warning",
    )


def run_owned_mcp_integration(
    *,
    dependencies: list["MCPDependency"],
    owner: str,
    lock_path: Path,
    project_root: Path,
    user_scope: bool,
    target_runtimes: list[str],
    logger=None,
    verbose: bool = False,
) -> int:
    """Reconcile one bundle owner's MCP servers and persist ownership."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.core.scope import InstallScope
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.errors import MCPNativeConflictError
    from apm_cli.install.mcp.ownership import (
        deselected_mcp_claims,
        names_owned_by_active_peers,
        retain_deferred_mcp_claims,
    )
    from apm_cli.install.mcp.retired_destinations import (
        heal_retired_project_mcp_destinations,
        healable_names,
        restore_pending_retired_claims,
        split_mcp_claims,
    )
    from apm_cli.integration.mcp_config_view import bundle_mcp_owner_token
    from apm_cli.integration.mcp_integrator import MCPIntegrator as _OwnedMCPIntegrator
    from apm_cli.integration.mcp_integrator_install import mcp_servers_for_runtime

    lockfile = LockFile.read(lock_path) or LockFile()
    native_hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
    old_native_hashes = {target: dict(hashes) for target, hashes in native_hashes.items()}
    owner_token = bundle_mcp_owner_token(owner)
    old_owned = {
        name
        for name, provenance in lockfile.mcp_config_provenance.items()
        if provenance == owner_token
    }
    if not dependencies and not old_owned:
        return 0
    new_names = _OwnedMCPIntegrator.get_server_names(dependencies)
    conflicts = {
        name
        for name in new_names
        if (existing := lockfile.mcp_config_provenance.get(name)) is not None
        and existing != owner_token
    }
    if conflicts:
        raise ValueError(
            "Bundle MCP server name conflicts with another owner: " + ", ".join(sorted(conflicts))
        )

    previous_targets = {target: set(names) for target, names in lockfile.mcp_target_servers.items()}
    # Claims naming the retired Copilot MCP files are healed, never live ownership.
    retired = split_mcp_claims(
        previous_targets,
        native_hashes,
        apm_version=lockfile.apm_version,
        project_root=project_root,
        user_scope=user_scope,
        stored_configs=lockfile.mcp_configs,
    )
    managed_targets = {target: names & old_owned for target, names in previous_targets.items()}
    count = 0
    conflict = None
    if dependencies:
        try:
            count = _OwnedMCPIntegrator.install(
                dependencies,
                verbose=verbose,
                stored_mcp_configs=lockfile.mcp_configs,
                apm_config={"targets": target_runtimes, "scripts": {}},
                project_root=project_root,
                user_scope=user_scope,
                explicit_target=target_runtimes,
                logger=logger,
                managed_target_servers=managed_targets,
                native_hashes=native_hashes,
                fail_on_write_error=True,
            )
        except MCPNativeConflictError as exc:
            if not exc.wrote_connections:
                raise
            conflict = exc
    selected_targets = set(managed_targets) if dependencies else set()
    retain_deferred_mcp_claims(managed_targets, previous_targets, new_names)
    stale = old_owned - new_names
    active = {
        target: names for target, names in managed_targets.items() if target in selected_targets
    }
    for target, names in list(managed_targets.items()):
        stale_for_target = (stale if target in selected_targets else old_owned) & names
        if target in selected_targets:
            # Declared servers whose `targets:` no longer select this runtime.
            stale_for_target |= deselected_mcp_claims(
                previous_targets.get(target, set()) & old_owned,
                new_names,
                mcp_servers_for_runtime(dependencies, target),
            )
        if stale_for_target and conflict is None:
            # An entry in a shared file (.mcp.json) stays while an active peer
            # owns it; only this runtime's claim retires.
            removable = stale_for_target - names_owned_by_active_peers(
                target, stale_for_target, active, user_scope=user_scope
            )
            if removable:
                _OwnedMCPIntegrator.remove_stale(
                    removable,
                    runtime=target,
                    project_root=project_root,
                    user_scope=user_scope,
                    scope=InstallScope.USER if user_scope else InstallScope.PROJECT,
                    fail_on_write_error=True,
                    stored_configs=lockfile.mcp_configs,
                    native_hashes=old_native_hashes,
                )
            names.difference_update(stale_for_target)
    healed = healable_names(
        {runtime: names & old_owned for runtime, names in retired.names.items()},
        declared=new_names,
        copilot_claims=managed_targets.get("copilot") if "copilot" in selected_targets else None,
    )
    if conflict is None and any(healed.values()):
        heal_retired_project_mcp_destinations(
            healed,
            stored_configs=lockfile.mcp_configs,
            native_hashes=retired.hashes,
            project_root=project_root,
            keep_signal_files=False,
        )
    restore_pending_retired_claims(
        retired,
        healed if conflict is None else {},
        target_servers=managed_targets,
        native_hashes=native_hashes,
        project_root=project_root,
        stored_configs=lockfile.mcp_configs,
    )

    retired = old_owned if conflict is None else set()
    for name in retired:
        lockfile.mcp_configs.pop(name, None)
        lockfile.mcp_config_provenance.pop(name, None)
    lockfile.mcp_servers = sorted((set(lockfile.mcp_servers) - retired) | new_names)
    lockfile.mcp_configs.update(_OwnedMCPIntegrator.get_server_configs(dependencies))
    lockfile.mcp_config_provenance.update(dict.fromkeys(new_names, owner_token))
    DeploymentLedgerCodec.replace_mcp_target_servers(
        lockfile,
        {target: sorted(names) for target, names in managed_targets.items() if names},
        native_hashes=native_hashes,
    )
    lockfile.write(lock_path)
    if conflict is not None:
        raise conflict
    return count


def _legacy_cleanup_runtimes(
    *,
    runtime: str | None,
    target_decision: "EffectiveTargetDecision | None",
) -> list[str | None]:
    """Return the project runtimes a pre-ownership lockfile's stale servers are cleaned from."""
    if runtime is not None:
        return [runtime]
    if target_decision is not None:
        selected = target_decision.runtime_targets_for_scope(user_scope=False)
        if selected:
            return list(selected)
    return [None]


def run_mcp_integration(  # noqa: PLR0913
    *,
    apm_package: "APMPackage",
    mcp_deps: list,
    apm_modules_path: Path,
    lock_path: Path,
    old_mcp_servers: builtins.set,
    old_mcp_configs: builtins.dict,
    old_mcp_provenance: builtins.dict,
    old_mcp_target_servers: builtins.dict | None = None,
    old_mcp_target_servers_present: bool = True,
    project_root: Path,
    user_scope: bool,
    should_install: bool,
    logger,
    diagnostics=None,
    runtime: str | None = None,
    exclude: str | None = None,
    trust_transitive_mcp: bool = False,
    no_policy: bool = False,
    verbose: bool = False,
    explicit_target: str | list[str] | None = None,
    target_decision: "EffectiveTargetDecision | None" = None,
    scope=None,
    prepared: PreparedMcpIntegration | None = None,
    active_package_keys: frozenset[str] | None = None,
    executable_context: tuple[dict | None, bool] = (None, False),
    pre_install_lock: "LockFile | None" = None,
    frozen: bool = False,
    claude_native_keys: frozenset[str] = frozenset(),
    policy_enforced: bool = False,
    mcp_entry: "McpEntryRequest | None" = None,
    resolved_targets: "ResolvedMcpTargets | None" = None,
) -> tuple[int, dict]:
    """Run MCP server integration after APM package installation.

    Mirrors the LSP integration pattern:
    1. Collect direct + transitive MCP deps
    2. Deduplicate (first occurrence wins)
    3. Filter by ``allowExecutables``
    4. Enforce policy against the merged (direct + transitive) set
    5. Install to each target's MCP config
    6. Clean up stale servers
    7. Update lockfile

    Args:
        apm_package: Root APM package with MCP deps.
        mcp_deps: Direct MCP dependencies (pre-merge with transitive).
        apm_modules_path: Path to apm_modules directory.
        lock_path: Path to apm.lock.yaml.
        old_mcp_servers: MCP server names from the lockfile before this run.
        old_mcp_configs: MCP server configs from the lockfile before this run.
        old_mcp_provenance: Transitive MCP provenance from the lockfile before
            this run.
        old_mcp_target_servers: APM-owned server names previously written per target.
        project_root: Project root directory.
        user_scope: If True, write to user-scope runtime config paths.
        should_install: Whether MCP integration should run.
        logger: Install logger instance.
        diagnostics: Optional DiagnosticCollector.
        runtime: Optional runtime override.
        exclude: Optional runtime exclusion.
        trust_transitive_mcp: Auto-trust self-defined MCP servers declared by
            transitive dependencies.
        no_policy: Skip policy enforcement for the merged MCP set.
        verbose: Show detailed installation information.
        explicit_target: Explicit target selected by CLI or manifest.
        scope: Optional InstallScope for user/project filtering.
        pre_install_lock: The lockfile as it was before this install. The
            package phase may already have rewritten *lock_path* with the
            running version, so retired-destination heal eligibility is read
            from this snapshot.
        frozen: ``--frozen`` replay; recorded pre-#3825 plugin roots are kept.
        mcp_entry: The server ``apm install --mcp`` adds (#4085). Only it is
            installed and re-targeted; the commit gate judged it before
            apm.yml was written, and every other recorded server is kept.
        resolved_targets: MCP targets this run already resolved, reused as-is.

    Returns:
        Tuple of ``(mcp_count, mcp_apm_config)``. ``mcp_apm_config`` is the
        target-resolution metadata derived from *apm_package*, returned so
        callers can forward it to :func:`apm_cli.install.lsp.run_lsp_integration`.

    Raises:
        apm_cli.policy.install_preflight.PolicyBlockError: When the merged
            MCP set is blocked by org policy. Callers must catch this,
            report the violation, and exit non-zero. A package install
            enforces the same policy before anything deploys.
    """
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.core.null_logger import NullCommandLogger
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.errors import MCPNativeConflictError
    from apm_cli.install.mcp.retired_destinations import (
        RetiredMcpClaims,
        heal_retired_project_mcp_destinations,
        healable_names,
        lock_holds_untranslated_mcp_claims,
        moved_root_entry_fingerprints,
        report_retired_mcp_heal,
        restore_pending_retired_claims,
        split_mcp_claims,
    )
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    if mcp_entry is not None and should_install:
        from apm_cli.install.mcp.entry_integration import integrate_mcp_entry

        mcp_count = integrate_mcp_entry(
            mcp_entry,
            lock_path=lock_path,
            project_root=project_root,
            user_scope=user_scope,
            scope=scope,
            runtime=runtime,
            exclude=exclude,
            verbose=verbose,
            explicit_target=explicit_target,
            target_decision=target_decision,
            logger=logger,
        )
        return mcp_count, {"scripts": apm_package.scripts or {}}

    previous_lock = LockFile.read(lock_path) if lockfile_exists(lock_path) else None
    native_hashes = DeploymentLedgerCodec.mcp_native_hashes(previous_lock) if previous_lock else {}
    old_native_hashes = {target: dict(hashes) for target, hashes in native_hashes.items()}

    prepared = prepared or prepare_mcp_integration(
        apm_package=apm_package,
        mcp_deps=mcp_deps,
        apm_modules_path=apm_modules_path,
        lock_path=lock_path,
        should_install=should_install,
        trust_transitive_mcp=trust_transitive_mcp,
        logger=logger,
        diagnostics=diagnostics,
        active_package_keys=active_package_keys,
        user_scope=user_scope,
        project_root=project_root,
        executable_context=executable_context,
        no_policy=no_policy,
        claude_native_keys=claude_native_keys,
    )
    mcp_deps, current_view = prepared.dependencies, prepared.current_view
    if frozen and current_view is not None:
        mcp_deps = current_view.replay_recorded_roots(mcp_deps, old_mcp_configs)

    # Org policy on the merged (direct + transitive) MCP set is owned by
    # install/commit_gate.py. A package install enforced it before anything
    # deployed (policy_enforced); service-only entries enforce it here,
    # before MCPIntegrator writes runtime configs.
    if should_install and not policy_enforced:
        from apm_cli.install.commit_gate import enforce_mcp_policy

        enforce_mcp_policy(
            mcp_deps,
            brought_in_by=current_view.brought_in_by if current_view is not None else None,
            runtime=runtime,
            target_decision=target_decision,
            user_scope=user_scope,
            project_root=project_root,
            no_policy=no_policy,
            logger=logger,
        )

    mcp_count = 0
    new_mcp_servers: builtins.set = builtins.set()
    mcp_apm_config: dict = {"scripts": apm_package.scripts or {}}
    from apm_cli.core.errors import TargetResolutionError
    from apm_cli.models.apm_package import (
        canonical_package_target_config,
        package_target_declaration,
    )

    try:
        mcp_apm_config.update(canonical_package_target_config(apm_package))
        package_target_declaration(apm_package).validate_for_install()
    except TargetResolutionError as exc:
        logger.error(str(exc), symbol="error")
        logger.render_summary()
        raise SystemExit(2) from exc

    # Entries APM <= 0.36 wrote to the retired project MCP destinations heal
    # out after this install has written their .mcp.json replacements.
    # Eligibility reads the pre-install lock: the package phase may already
    # have rewritten lock_path with the running version.
    heal_basis = pre_install_lock if pre_install_lock is not None else previous_lock
    retired_owned = (
        split_mcp_claims(
            old_mcp_target_servers,
            native_hashes,
            apm_version=heal_basis.apm_version,
            project_root=project_root,
            user_scope=user_scope,
            stored_configs=old_mcp_configs,
        )
        if should_install and old_mcp_target_servers and heal_basis is not None
        else RetiredMcpClaims({}, {})
    )
    # A --frozen install moved these servers and they are no longer
    # declared: their .mcp.json entry is removed on the same proof as adoption.
    undeclared_moved = moved_root_entry_fingerprints(
        retired_owned.moved_unrecorded - MCPIntegrator.get_server_names(mcp_deps),
        project_root=project_root,
        stored_configs=old_mcp_configs,
    )
    if undeclared_moved:
        old_native_hashes.setdefault("copilot", {}).update(undeclared_moved)
    # The claims are translated once this install persists them (heal and
    # receipts), so the lock may then record the running version.
    translated = heal_basis is not None and lock_holds_untranslated_mcp_claims(
        heal_basis.apm_version, heal_basis.mcp_target_servers
    )

    def _heal_retired(
        declared: builtins.set, copilot_claims: builtins.set | None
    ) -> dict[str, builtins.set]:
        if not retired_owned.names:
            return {}
        healable = healable_names(
            retired_owned.names, declared=declared, copilot_claims=copilot_claims
        )
        heal = heal_retired_project_mcp_destinations(
            healable,
            stored_configs=old_mcp_configs,
            native_hashes=retired_owned.hashes,
            project_root=project_root,
            keep_signal_files=frozen,
        )
        report_retired_mcp_heal(heal, logger)
        return healable

    def _keep_unhealed(healed: dict[str, builtins.set]) -> None:
        """Keep the retired claims this run did not heal, for the next install."""
        restore_pending_retired_claims(
            retired_owned,
            healed,
            target_servers=managed_target_servers,
            native_hashes=native_hashes,
            project_root=project_root,
            stored_configs=old_mcp_configs,
        )

    # Recorded local-bundle servers stay owned by their bundle install (#3955).
    bundle_owned = current_view.bundle_owned if current_view is not None else frozenset()
    if should_install and (mcp_deps or bundle_owned):
        from apm_cli.install.mcp.ownership import resolve_mcp_target_servers

        old_mcp_target_servers = resolve_mcp_target_servers(
            recorded_target_servers=old_mcp_target_servers or {},
            ownership_present=old_mcp_target_servers_present,
            server_names=builtins.set(old_mcp_servers),
            stored_configs=old_mcp_configs,
            project_root=project_root,
            user_scope=user_scope,
        )
        if target_decision is not None:
            from apm_cli.install.mcp.ownership import migrate_legacy_project_target_servers

            # An explicit --runtime overrides the target decision, as in run_mcp_install.
            active_runtimes = (
                [runtime]
                if runtime is not None
                else target_decision.runtime_targets_for_scope(user_scope=user_scope)
            )
            if active_runtimes is not None:
                migrate_legacy_project_target_servers(
                    old_mcp_target_servers,
                    active_runtimes=set(active_runtimes),
                    user_scope=user_scope,
                )
        managed_target_servers = {
            target: builtins.set(servers) for target, servers in old_mcp_target_servers.items()
        }
        conflict = None
        from apm_cli.install.mcp.ownership import adoptable_root_mcp_entries

        # A --frozen install may already have moved these declared servers to
        # .mcp.json without recording it; their root entry is APM's write.
        adoptable = retired_owned.moved_unrecorded & MCPIntegrator.get_server_names(mcp_deps)
        try:
            with adoptable_root_mcp_entries(frozenset(adoptable)):
                mcp_count = (
                    MCPIntegrator.install(
                        mcp_deps,
                        runtime,
                        exclude,
                        verbose,
                        stored_mcp_configs=old_mcp_configs,
                        apm_config=mcp_apm_config,
                        project_root=project_root,
                        user_scope=user_scope,
                        explicit_target=explicit_target,
                        target_decision=target_decision,
                        fail_on_write_error=True,
                        diagnostics=diagnostics,
                        scope=scope,
                        managed_target_servers=managed_target_servers,
                        native_hashes=native_hashes,
                        resolved_targets=resolved_targets,
                        # Console output as before; the Targets line stays once per command.
                        logger=NullCommandLogger(targets_owner=logger),
                    )
                    if mcp_deps
                    else 0
                )
        except MCPNativeConflictError as exc:
            if not exc.wrote_connections:
                raise
            conflict = exc
        # The dependency install manages declared servers only; a bundle-owned
        # server keeps its claim and receipt on every target this run selects,
        # so a later bundle reinstall can still retire it (#3955).
        for owned_target, owned in old_mcp_target_servers.items():
            kept_bundle = builtins.set(owned) & bundle_owned
            if not kept_bundle or owned_target not in managed_target_servers:
                continue
            managed_target_servers[owned_target] |= kept_bundle
            for name in kept_bundle:
                receipt = old_native_hashes.get(owned_target, {}).get(name)
                if receipt is not None:
                    native_hashes.setdefault(owned_target, {}).setdefault(name, receipt)
        new_mcp_servers = MCPIntegrator.get_server_names(mcp_deps) | bundle_owned
        new_mcp_configs = dict(current_view.configs) if current_view is not None else {}
        new_mcp_provenance = dict(current_view.provenance) if current_view is not None else {}

        if conflict is not None:
            from apm_cli.install.mcp.ownership import retain_deferred_mcp_claims

            # No cleanup has happened: retain prior declarations and retired targets.
            retain_deferred_mcp_claims(
                managed_target_servers, old_mcp_target_servers, new_mcp_servers
            )
            new_mcp_servers |= old_mcp_servers
            new_mcp_configs = {**old_mcp_configs, **new_mcp_configs}
            new_mcp_provenance = {**old_mcp_provenance, **new_mcp_provenance}
            # Heal only what this run replaced; the retry heals the rest.
            healed = _heal_retired(
                builtins.set(new_mcp_servers),
                builtins.set(managed_target_servers.get("copilot", ())),
            )
            _keep_unhealed(healed)
            MCPIntegrator.update_lockfile(
                new_mcp_servers,
                lock_path,
                mcp_configs=new_mcp_configs,
                mcp_target_servers=managed_target_servers,
                mcp_config_provenance=new_mcp_provenance,
                native_hashes=native_hashes,
                logger=logger,
                fail_on_write_error=True,
            )
            raise conflict

        healed = _heal_retired(
            builtins.set(new_mcp_servers),
            managed_target_servers.get("copilot") if "copilot" in managed_target_servers else None,
        )
        from apm_cli.install.mcp.ownership import (
            deselected_mcp_claims,
            names_owned_by_active_peers,
        )
        from apm_cli.integration.mcp_integrator_install import mcp_servers_for_runtime

        # Targets the manifest itself stopped selecting leave every entry's reach;
        # a target only excluded from this run (--exclude) is still listed there.
        manifest_runtimes = (
            builtins.set(target_decision.runtime_targets_for_scope(user_scope=user_scope) or ())
            if runtime is None and target_decision is not None and target_decision.from_manifest
            else None
        )
        retired_by_target: builtins.dict[str, builtins.set] = {}
        retained_names: builtins.set = builtins.set()
        for owned_target, owned in sorted(old_mcp_target_servers.items()):
            if owned_target in managed_target_servers:
                # Declared servers whose `targets:` no longer select this runtime.
                # Bundle-owned servers declare no targets:, so only dependency
                # declarations can be deselected (#3955 with #2606).
                admitted = mcp_servers_for_runtime(mcp_deps, owned_target)
                retired_by_target[owned_target] = deselected_mcp_claims(
                    owned, new_mcp_servers - bundle_owned, admitted
                )
                continue
            # A runtime this run does not select keeps the servers that name it,
            # and the servers a local bundle wired there (#3955).
            kept = builtins.set(owned) & (
                mcp_servers_for_runtime(mcp_deps, owned_target, named_only=True) | bundle_owned
            )
            if manifest_runtimes is not None and owned_target not in manifest_runtimes:
                kept = builtins.set()
            if kept:
                managed_target_servers[owned_target] = kept
                retained_names |= kept
            retired_by_target[owned_target] = builtins.set(owned) - kept
        # A skipped target still holds the old declaration: keep that baseline
        # so its next install sees the drift and applies the new one.
        for name in MCPIntegrator._detect_mcp_config_drift(
            [dep for dep in mcp_deps if getattr(dep, "name", None) in retained_names],
            old_mcp_configs,
        ):
            new_mcp_configs[name] = old_mcp_configs[name]
        for removed_target, retired_names in retired_by_target.items():
            # An entry in a file the removed runtime shares (.mcp.json) stays
            # while an active runtime still owns it.
            retired_names -= names_owned_by_active_peers(
                removed_target, retired_names, managed_target_servers, user_scope=user_scope
            )
            if not retired_names:
                continue
            MCPIntegrator.remove_stale(
                retired_names,
                runtime=removed_target,
                project_root=project_root,
                user_scope=user_scope,
                scope=scope,
                fail_on_write_error=True,
                stored_configs=old_mcp_configs,
                native_hashes=old_native_hashes,
            )
            logger.record_mcp_removed(retired_names)

        # Remove stale MCP servers that are no longer needed
        stale_servers = old_mcp_servers - new_mcp_servers
        if stale_servers:
            cleanup_owners = {
                target: builtins.set(servers) for target, servers in old_mcp_target_servers.items()
            }
            if undeclared_moved:
                cleanup_owners.setdefault("copilot", builtins.set()).update(undeclared_moved)
            if not old_mcp_target_servers_present and not user_scope:
                # A lockfile written before per-target ownership names no runtime for
                # its servers, so a project install cleans the runtimes it targets,
                # as those versions did. User scope stays ownership-only.
                for legacy_runtime in _legacy_cleanup_runtimes(
                    runtime=runtime, target_decision=target_decision
                ):
                    cleanup_owners.setdefault(legacy_runtime, builtins.set()).update(stale_servers)
            for cleanup_runtime, owned_servers in sorted(
                cleanup_owners.items(), key=lambda item: item[0] or ""
            ):
                scoped_stale = stale_servers.intersection(owned_servers)
                if not scoped_stale:
                    continue
                MCPIntegrator.remove_stale(
                    scoped_stale,
                    cleanup_runtime,
                    exclude,
                    project_root=project_root,
                    user_scope=user_scope,
                    scope=scope,
                    fail_on_write_error=True,
                    stored_configs=old_mcp_configs,
                    native_hashes=old_native_hashes,
                )
                logger.record_mcp_removed(scoped_stale)

        _keep_unhealed(healed)
        # Persist the new MCP server set, configs, and transitive provenance.
        MCPIntegrator.update_lockfile(
            new_mcp_servers,
            lock_path,
            mcp_configs=new_mcp_configs,
            mcp_target_servers=managed_target_servers,
            mcp_config_provenance=new_mcp_provenance,
            native_hashes=native_hashes,
            logger=logger,
            fail_on_write_error=True,
            stamp_version=translated,
        )
    elif should_install and not mcp_deps:
        # No MCP deps at all -- remove any old APM-managed servers
        _heal_retired(builtins.set(), None)
        if old_mcp_servers:
            from apm_cli.install.mcp.ownership import resolve_mcp_target_servers

            cleanup_owners = resolve_mcp_target_servers(
                recorded_target_servers=old_mcp_target_servers or {},
                ownership_present=old_mcp_target_servers_present,
                server_names=builtins.set(old_mcp_servers),
                stored_configs=old_mcp_configs,
                project_root=project_root,
                user_scope=user_scope,
            )
            if undeclared_moved:
                cleanup_owners.setdefault("copilot", builtins.set()).update(undeclared_moved)
            for cleanup_runtime, managed_servers in sorted(cleanup_owners.items()):
                scoped_stale = builtins.set(old_mcp_servers).intersection(managed_servers)
                if not scoped_stale:
                    continue
                MCPIntegrator.remove_stale(
                    scoped_stale,
                    cleanup_runtime,
                    exclude,
                    project_root=project_root,
                    user_scope=user_scope,
                    scope=scope,
                    fail_on_write_error=True,
                    stored_configs=old_mcp_configs,
                    native_hashes=old_native_hashes,
                )
                logger.record_mcp_removed(scoped_stale)
            MCPIntegrator.update_lockfile(
                builtins.set(),
                lock_path,
                mcp_configs={},
                mcp_target_servers={},
                mcp_config_provenance={},
                logger=logger,
                fail_on_write_error=True,
                stamp_version=translated,
            )
        logger.verbose_detail("No MCP dependencies found in apm.yml")
    elif not should_install and old_mcp_servers:
        # --only=apm: APM install regenerated the lockfile and dropped
        # mcp_servers.  Restore the previous set so it is not lost.
        MCPIntegrator.update_lockfile(
            old_mcp_servers,
            lock_path,
            mcp_configs=old_mcp_configs,
            mcp_target_servers=old_mcp_target_servers,
            mcp_config_provenance=old_mcp_provenance,
            logger=logger,
            fail_on_write_error=True,
        )

    if prepared.omission_view is not None and lockfile_exists(lock_path):
        lockfile = LockFile.read(lock_path)
        omissions = prepared.omissions(lockfile) if lockfile is not None else None
        if lockfile is not None and omissions != lockfile.mcp_omissions:
            replace(lockfile, mcp_omissions=omissions).write(lock_path, existing_lockfile=lockfile)

    return mcp_count, mcp_apm_config
