"""Native MCP ownership baselines, safe cleanup, and legacy migration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class NativeMcpConnectionState(Enum):
    """Native connection comparison; equality alone never establishes ownership."""

    ABSENT = "absent"
    MATCHING_MANAGED = "matching-managed"
    MATCHING_FOREIGN = "matching-foreign"
    UPDATE_MANAGED = "update-managed"
    CONFLICT = "conflict"


def compare_native_mcp_connection(
    client: Any,
    name: str,
    *,
    requested_info: dict | None,
    stored_config: dict | None,
    managed: bool,
    existing_configs: Mapping[str, Any] | None = None,
) -> NativeMcpConnectionState:
    """Compare a connection with its requested and previously recorded native shapes."""
    from apm_cli.core.conflict_detector import MCPConflictDetector
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.models.dependency.mcp import MCPDependency

    existing = (
        MCPConflictDetector(client).get_existing_server_configs()
        if existing_configs is None
        else existing_configs
    )
    key = client._determine_config_key(name, None)
    if key not in existing:
        return NativeMcpConnectionState.ABSENT
    native = existing[key]
    requested = client.render_server_config(requested_info) if requested_info is not None else None
    comparison = native
    matches_baseline = False
    if managed and isinstance(stored_config, dict):
        try:
            dependency = MCPDependency.from_dict(stored_config)
            if dependency.is_self_defined:
                expected = client.render_server_config(
                    MCPIntegrator._build_self_defined_info(dependency)
                )
                if requested is not None:
                    comparison = client.project_managed_server_config(
                        native, baseline=expected, requested=requested
                    )
                matches_baseline = client.matches_managed_server_config(comparison, expected)
        except (TypeError, ValueError):
            logger.debug("Unusable MCP baseline for %s on %s", name, client.target_name)
    if requested is not None:
        requested_native = comparison if matches_baseline else native
        if client.matches_managed_server_config(requested_native, requested):
            return (
                NativeMcpConnectionState.MATCHING_MANAGED
                if matches_baseline
                else NativeMcpConnectionState.MATCHING_FOREIGN
            )
    return (
        NativeMcpConnectionState.UPDATE_MANAGED
        if matches_baseline
        else NativeMcpConnectionState.CONFLICT
    )


def require_removable_mcp_connections(
    client: Any,
    names: set[str],
    stored_configs: dict[str, dict],
) -> None:
    """Refuse removal when recorded names do not prove unchanged native content."""
    from apm_cli.core.conflict_detector import MCPConflictDetector
    from apm_cli.install.errors import RequiredIntegrationError

    existing = MCPConflictDetector(client).get_existing_server_configs() if names else {}
    conflicts = [
        name
        for name in sorted(names)
        if compare_native_mcp_connection(
            client,
            name,
            requested_info=None,
            stored_config=stored_configs.get(name),
            managed=True,
            existing_configs=existing,
        )
        is NativeMcpConnectionState.CONFLICT
    ]
    if conflicts:
        raise RequiredIntegrationError(
            f"Preserved MCP connection(s) {', '.join(conflicts)} in "
            f"{client.target_name} config ({client.get_config_path()}): "
            "the native connection differs from its managed baseline or the baseline "
            "cannot be verified. Reconcile these connections manually (rename or remove "
            "only the entries you intend to retire), then retry the same command. "
            "--force does not take ownership of user connections."
        )


def resolve_mcp_target_servers(
    *,
    recorded_target_servers: dict[str, set[str]],
    ownership_present: bool,
    server_names: set[str],
    stored_configs: dict[str, dict],
    project_root: Path | None,
    user_scope: bool,
) -> dict[str, set[str]]:
    """Return recorded ownership, adopting exact legacy baselines only when absent."""
    target_servers = {runtime: set(servers) for runtime, servers in recorded_target_servers.items()}
    if target_servers or ownership_present:
        return target_servers
    return adopt_legacy_mcp_target_servers(
        server_names=server_names,
        stored_configs=stored_configs,
        project_root=project_root,
        user_scope=user_scope,
    )


def migrate_legacy_project_target_servers(
    target_servers: dict[str, set[str]],
    *,
    active_runtimes: set[str],
    user_scope: bool,
) -> None:
    """Move legacy project VS Code ownership to the Copilot runtime key."""
    if user_scope or "copilot" not in active_runtimes or "vscode" in active_runtimes:
        return
    legacy_servers = target_servers.pop("vscode", set())
    if legacy_servers:
        target_servers.setdefault("copilot", set()).update(legacy_servers)


def adopt_legacy_mcp_target_servers(
    *,
    server_names: set[str],
    stored_configs: dict[str, dict],
    project_root: Path | None,
    user_scope: bool,
) -> dict[str, set[str]]:
    """Adopt legacy native entries only when they exactly match their baseline."""
    from apm_cli.core.conflict_detector import MCPConflictDetector
    from apm_cli.factory import ClientFactory
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.models.dependency.mcp import MCPDependency, opencode_enabled_matches

    baselines: dict[str, Any] = {}
    for name in sorted(server_names):
        raw = stored_configs.get(name)
        if not isinstance(raw, dict):
            continue
        try:
            dependency = MCPDependency.from_dict(raw)
        except (TypeError, ValueError):
            continue
        if not dependency.is_self_defined:
            continue
        baselines[name] = dependency

    if not baselines:
        return {}

    adopted: dict[str, set[str]] = {}
    for runtime in ClientFactory.supported_clients():
        try:
            client = ClientFactory.create_client(
                runtime,
                project_root=project_root,
                user_scope=user_scope,
            )
            existing_configs = [MCPConflictDetector(client).get_existing_server_configs()]
            legacy_reader = getattr(client, "get_legacy_current_config", None)
            if callable(legacy_reader):
                legacy_config = legacy_reader()
                legacy_servers = legacy_config.get(client.mcp_servers_key)
                if isinstance(legacy_servers, dict):
                    existing_configs.append(legacy_servers)
        except Exception:
            logger.debug("Could not inspect legacy MCP target %s", runtime, exc_info=True)
            continue

        for name, dependency in baselines.items():
            try:
                expected = client.render_server_config(
                    MCPIntegrator._build_self_defined_info(dependency)
                )
            except Exception:
                logger.debug(
                    "Could not render legacy MCP baseline %s for %s",
                    name,
                    runtime,
                    exc_info=True,
                )
                continue
            native_baselines = [expected]
            candidates = existing_configs
            if runtime == "codex":
                from apm_cli.adapters.client.codex import CodexClientAdapter

                # Earlier Codex renders added an empty id to self-defined
                # entries. A nonempty ID, including its new comment encoding,
                # still disqualifies a native entry from this exact baseline.
                native_baselines.append({**expected, "id": ""})
                candidates = [
                    existing
                    for existing in candidates
                    if isinstance(existing.get(name), dict)
                    and not CodexClientAdapter.get_registry_id(existing[name])
                ]
            if any(
                existing.get(name) in native_baselines
                and (runtime != "opencode" or opencode_enabled_matches(existing[name], expected))
                for existing in candidates
            ):
                adopted.setdefault(runtime, set()).add(name)
    return adopted
