"""The single server ``apm install --mcp`` adds, reconciled by the install's MCP stage.

``apm install --mcp NAME`` and ``apm mcp install NAME`` reach
:func:`apm_cli.install.mcp.integration.run_mcp_integration` through the shared
service stage (``install/service_integration.py``) with an
:class:`McpEntryRequest`. Only that entry is installed and re-targeted; every
other recorded server keeps its configs and per-target claims, so adding one
server never narrows where the others are deployed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.constants import InstallMode
from apm_cli.deps.lockfile import LockFile
from apm_cli.install.errors import MCPNativeConflictError
from apm_cli.install.mcp.ownership import names_owned_by_active_peers
from apm_cli.install.mcp.retired_destinations import (
    RetiredMcpClaims,
    heal_retired_project_mcp_destinations,
    healable_names,
    report_retired_mcp_heal,
    restore_pending_retired_claims,
    split_mcp_claims,
)
from apm_cli.integration.mcp_integrator import MCPIntegrator

if TYPE_CHECKING:
    from apm_cli.core.scope import InstallScope
    from apm_cli.core.target_detection import EffectiveTargetDecision


@dataclass(frozen=True)
class McpEntryCommand:
    """The direct MCP entry as the install's service stage sees it: MCP only."""

    project_root: Path
    scope: InstallScope | None
    logger: Any
    runtime: str | None
    exclude: str | None
    no_policy: bool
    verbose: bool
    force: bool
    install_mode: InstallMode = InstallMode.MCP
    trust_transitive_mcp: bool = False
    exec_allow_map: dict[str, dict[str, bool]] | None = None
    exec_allow_resolved: bool = False
    frozen: bool = False
    # The commit gate judged the entry before apm.yml was written.
    mcp_policy_enforced: bool = True
    mcp_entry: McpEntryRequest | None = None


@dataclass(frozen=True)
class McpEntryRequest:
    """The server a direct MCP install adds, validated before apm.yml was written."""

    dependency: Any  # MCPDependency
    prevalidated_registry_servers: Any = None


def integrate_mcp_entry(
    entry: McpEntryRequest,
    *,
    lock_path: Path,
    project_root: Path,
    user_scope: bool,
    scope: InstallScope | None,
    runtime: str | None,
    exclude: str | None,
    verbose: bool,
    explicit_target: str | list[str] | None,
    target_decision: EffectiveTargetDecision | None,
    logger: Any,
) -> int:
    """Install *entry* and record it in the lockfile beside every existing server."""
    dep = entry.dependency
    name = dep.name
    _existing_lock = LockFile.read(lock_path)
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    native_hashes = (
        DeploymentLedgerCodec.mcp_native_hashes(_existing_lock) if _existing_lock else {}
    )
    old_native_hashes = {target_name: dict(hashes) for target_name, hashes in native_hashes.items()}
    old_servers = set(_existing_lock.mcp_servers) if _existing_lock else set()
    old_configs = dict(_existing_lock.mcp_configs) if _existing_lock else {}
    old_provenance = dict(_existing_lock.mcp_config_provenance) if _existing_lock else {}
    old_target_servers = (
        {
            target_name: set(server_names)
            for target_name, server_names in _existing_lock.mcp_target_servers.items()
        }
        if _existing_lock
        else {}
    )
    target_servers_present = _existing_lock._mcp_target_servers_present if _existing_lock else True
    retired = RetiredMcpClaims({}, {})
    if _existing_lock:
        from apm_cli.install.mcp.ownership import resolve_mcp_target_servers

        old_target_servers = resolve_mcp_target_servers(
            recorded_target_servers=old_target_servers,
            ownership_present=target_servers_present,
            server_names=old_servers,
            stored_configs=old_configs,
            project_root=project_root,
            user_scope=user_scope,
        )
        # Claims naming the retired Copilot MCP files are healed by
        # proof, never compared or cleaned as .mcp.json ownership.
        retired = split_mcp_claims(
            old_target_servers,
            native_hashes,
            apm_version=_existing_lock.apm_version,
            project_root=project_root,
            user_scope=user_scope,
            stored_configs=old_configs,
        )
    requested_target_servers = {
        target_name: server_names & {name}
        for target_name, server_names in old_target_servers.items()
        if server_names & {name}
    }
    from apm_cli.integration.mcp_integrator_install import _validate_target_configs

    _validate_target_configs(
        list(requested_target_servers),
        project_root=project_root,
        user_scope=user_scope,
        console=None,
        logger=logger,
    )
    # Legacy --runtime remains a direct override. Target values
    # are projected through the shared decision below.
    conflict = None
    installed = 0
    try:
        installed = MCPIntegrator.install(
            [dep],
            runtime,
            exclude,
            verbose,
            stored_mcp_configs=old_configs,
            explicit_target=explicit_target,
            target_decision=target_decision,
            fail_on_write_error=True,
            scope=scope,
            project_root=project_root,
            user_scope=user_scope,
            managed_target_servers=requested_target_servers,
            prevalidated_registry_servers=entry.prevalidated_registry_servers,
            native_hashes=native_hashes,
        )
    except MCPNativeConflictError as exc:
        if not exc.wrote_connections:
            raise
        conflict = exc
    new_names = MCPIntegrator.get_server_names([dep])
    new_configs = MCPIntegrator.get_server_configs([dep])
    merged_names = old_servers | new_names
    merged_configs = dict(old_configs)
    merged_configs.update(new_configs)
    merged_provenance = dict(old_provenance)
    merged_provenance.pop(name, None)
    merged_target_servers = {
        target_name: set(server_names) for target_name, server_names in old_target_servers.items()
    }
    for target_name, server_names in requested_target_servers.items():
        merged_target_servers.setdefault(target_name, set()).discard(name)
        merged_target_servers.setdefault(target_name, set()).update(server_names)
    if retired.names:
        healed = healable_names(
            {runtime: names & {name} for runtime, names in retired.names.items()},
            declared={name},
            copilot_claims=requested_target_servers.get("copilot")
            if "copilot" in requested_target_servers
            else None,
        )
        report_retired_mcp_heal(
            heal_retired_project_mcp_destinations(
                healed,
                stored_configs=old_configs,
                native_hashes=retired.hashes,
                project_root=project_root,
                keep_signal_files=False,
            ),
            logger,
        )
        restore_pending_retired_claims(
            retired,
            healed,
            target_servers=merged_target_servers,
            native_hashes=native_hashes,
            project_root=project_root,
            stored_configs=old_configs,
        )
    retired_targets = {
        previous_target: {name}
        for previous_target, server_names in old_target_servers.items()
        if name in server_names and previous_target not in requested_target_servers
    }
    if retired_targets and conflict is None:
        # Cleanup can fail after a destination write. Keep both deployments
        # durably owned until removal succeeds, so retry cannot orphan either.
        MCPIntegrator.update_lockfile(
            merged_names,
            lock_path,
            mcp_configs=merged_configs,
            mcp_target_servers=merged_target_servers,
            mcp_config_provenance=merged_provenance,
            native_hashes=native_hashes,
            logger=logger,
            fail_on_write_error=True,
        )
        for previous_target, retired in retired_targets.items():
            # An entry in a shared file (.mcp.json) stays while the
            # newly selected peer owns it; only the claim retires.
            removable = retired - names_owned_by_active_peers(
                previous_target,
                retired,
                requested_target_servers,
                user_scope=user_scope,
            )
            if removable:
                MCPIntegrator.remove_stale(
                    removable,
                    runtime=previous_target,
                    project_root=project_root,
                    user_scope=user_scope,
                    scope=scope,
                    fail_on_write_error=True,
                    stored_configs=old_configs,
                    native_hashes=old_native_hashes,
                )
            merged_target_servers[previous_target].difference_update(retired)
    MCPIntegrator.update_lockfile(
        merged_names,
        lock_path,
        mcp_configs=merged_configs,
        mcp_target_servers=merged_target_servers,
        mcp_config_provenance=merged_provenance,
        native_hashes=native_hashes,
        logger=logger,
        fail_on_write_error=True,
    )
    if conflict is not None:
        raise conflict
    return installed
