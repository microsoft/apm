"""MCP server integration for the APM install pipeline.

Mirrors the LSP integration pattern (see ``apm_cli.install.lsp.integration``)
with runtime-neutral target selection.
"""

import builtins
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.models.apm_package import APMPackage


def run_mcp_integration(  # noqa: PLR0913
    *,
    apm_package: "APMPackage",
    mcp_deps: list,
    apm_modules_path: Path,
    lock_path: Path,
    old_mcp_servers: builtins.set,
    old_mcp_configs: builtins.dict,
    old_mcp_provenance: builtins.dict,
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
    scope=None,
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

    Returns:
        Tuple of ``(mcp_count, mcp_apm_config)``. ``mcp_apm_config`` is the
        target-resolution metadata derived from *apm_package*, returned so
        callers can forward it to :func:`apm_cli.install.lsp.run_lsp_integration`.

    Raises:
        apm_cli.policy.install_preflight.PolicyBlockError: When the merged
            MCP set is blocked by org policy. Callers must catch this,
            report the violation, and exit non-zero; already-installed APM
            packages are left in place.
    """
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.mcp_config_view import CurrentMcpConfigView
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.policy.install_preflight import run_policy_preflight

    current_view = None
    if should_install:
        lockfile = LockFile.read(lock_path) if lock_path.exists() else None
        current_view = CurrentMcpConfigView.derive(
            apm_package,
            lockfile,
            apm_modules_path,
            trust_transitive_self_defined=trust_transitive_mcp,
            diagnostics=diagnostics,
        )
        root_count = len(apm_package.get_all_mcp_dependencies())
        transitive_count = max(0, len(current_view.dependencies) - root_count)
        if transitive_count:
            logger.verbose_detail(f"Collected {transitive_count} transitive MCP dependency(ies)")
        mcp_deps = list(current_view.dependencies)

    # allowExecutables MCP gate.
    from apm_cli.security.executables import filter_mcp_by_allow_executables

    mcp_deps = filter_mcp_by_allow_executables(
        mcp_deps, getattr(apm_package, "allow_executables", None), logger
    )

    # The pipeline gate phase (policy_gate.py) checks direct APM deps
    # and direct MCP deps from apm.yml.  However, transitive MCP
    # servers (discovered via collect_transitive above) are only known
    # after APM packages are installed.  Run a second preflight
    # against the *merged* MCP set (direct + transitive) BEFORE
    # MCPIntegrator writes runtime configs.  The caller is responsible
    # for catching PolicyBlockError and aborting the MCP write while
    # leaving already-installed APM packages in place.
    if should_install and mcp_deps:
        run_policy_preflight(
            project_root=project_root,
            mcp_deps=mcp_deps,
            no_policy=no_policy,
            logger=logger,
            dry_run=False,
        )

    mcp_count = 0
    new_mcp_servers: builtins.set = builtins.set()
    # Forward only the targets-key the user actually declared so parse_targets_field
    # in the gate sees the same dict shape it sees from raw apm.yml. Including a
    # `targets: None` placeholder when the user wrote `target:` (singular) would
    # falsely trip the conflict-mutex check (see core.apm_yml.parse_targets_field).
    # This restores parity with `apm install` for users on the modern `targets:`
    # plural form -- without this, `targets:` was silently dropped at the call
    # site and the gate fell back to permissive directory detection (#1335).
    mcp_apm_config: dict = {"scripts": apm_package.scripts or {}}
    if apm_package.targets is not None:
        mcp_apm_config["targets"] = apm_package.targets
    elif apm_package.target is not None:
        mcp_apm_config["target"] = apm_package.target

    if should_install and mcp_deps:
        mcp_count = MCPIntegrator.install(
            mcp_deps,
            runtime,
            exclude,
            verbose,
            stored_mcp_configs=old_mcp_configs,
            apm_config=mcp_apm_config,
            project_root=project_root,
            user_scope=user_scope,
            explicit_target=explicit_target,
            diagnostics=diagnostics,
            scope=scope,
        )
        new_mcp_servers = MCPIntegrator.get_server_names(mcp_deps)
        new_mcp_configs = dict(current_view.configs) if current_view is not None else {}
        new_mcp_provenance = dict(current_view.provenance) if current_view is not None else {}

        # Remove stale MCP servers that are no longer needed
        stale_servers = old_mcp_servers - new_mcp_servers
        if stale_servers:
            MCPIntegrator.remove_stale(
                stale_servers,
                runtime,
                exclude,
                project_root=project_root,
                user_scope=user_scope,
                scope=scope,
            )

        # Persist the new MCP server set, configs, and transitive provenance.
        MCPIntegrator.update_lockfile(
            new_mcp_servers,
            lock_path,
            mcp_configs=new_mcp_configs,
            mcp_config_provenance=new_mcp_provenance,
        )
    elif should_install and not mcp_deps:
        # No MCP deps at all -- remove any old APM-managed servers
        if old_mcp_servers:
            MCPIntegrator.remove_stale(
                old_mcp_servers,
                runtime,
                exclude,
                project_root=project_root,
                user_scope=user_scope,
                scope=scope,
            )
            MCPIntegrator.update_lockfile(
                builtins.set(), lock_path, mcp_configs={}, mcp_config_provenance={}
            )
        logger.verbose_detail("No MCP dependencies found in apm.yml")
    elif not should_install and old_mcp_servers:
        # --only=apm: APM install regenerated the lockfile and dropped
        # mcp_servers.  Restore the previous set so it is not lost.
        MCPIntegrator.update_lockfile(
            old_mcp_servers,
            lock_path,
            mcp_configs=old_mcp_configs,
            mcp_config_provenance=old_mcp_provenance,
        )

    return mcp_count, mcp_apm_config
