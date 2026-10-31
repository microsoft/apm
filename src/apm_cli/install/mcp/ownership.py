"""Native MCP ownership baselines, safe cleanup, and legacy migration."""

from __future__ import annotations

import hashlib
import json
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


def mcp_native_hash(native: dict) -> str:
    """Fingerprint a written native entry without retaining its resolved secrets."""
    encoded = json.dumps(native, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compare_native_mcp_connection(
    client: Any,
    name: str,
    *,
    requested_info: dict | None,
    stored_config: dict | None,
    managed: bool,
    existing_configs: Mapping[str, Any] | None = None,
    managed_fields: set[str] | None = None,
    native_hash: str | None = None,
    registry_request: bool = False,
    env_overrides: dict | None = None,
    runtime_vars: dict | None = None,
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
    requested = None
    if requested_info is not None:
        requested = (
            client.render_server_config(
                requested_info, env_overrides=env_overrides, runtime_vars=runtime_vars
            )
            if registry_request
            else client.render_server_config(requested_info)
        )
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
                matches_baseline = client.matches_managed_server_config(
                    comparison, expected
                ) or client.matches_legacy_managed_server_config(native, expected)
                if matches_baseline and managed_fields is not None:
                    managed_fields.update(expected)
            elif native_hash is not None and isinstance(native, dict):
                comparable = client.managed_server_config(native)
                matches_baseline = mcp_native_hash(comparable) == native_hash
                if matches_baseline and managed_fields is not None:
                    managed_fields.update(comparable)
        except (TypeError, ValueError):
            logger.debug("Unusable MCP baseline for %s on %s", name, client.target_name)
    if requested is not None:
        requested_native = comparison if matches_baseline else native
        matches_request = (
            isinstance(requested_native, dict)
            and client.managed_server_config(requested_native) == requested
            if registry_request
            else client.matches_managed_server_config(requested_native, requested)
        )
        if matches_request:
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
    native_hashes: dict[str, str] | None = None,
) -> set[str]:
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
            native_hash=(native_hashes or {}).get(name),
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
    return {client._determine_config_key(name, None) for name in names}


def retain_deferred_mcp_claims(
    current: dict[str, set[str]],
    previous: dict[str, set[str]],
    requested_names: set[str],
) -> None:
    """Retain untouched claims without restoring revoked current-name ownership."""
    for target, names in previous.items():
        deferred = names - requested_names if target in current else names
        current.setdefault(target, set()).update(deferred)


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
