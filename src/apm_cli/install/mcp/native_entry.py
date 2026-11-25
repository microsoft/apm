"""Whether a runtime's native config already holds a declared MCP entry.

Read-only probes the frozen lock replay asks before it claims a server; split
from ``install/mcp/ownership.py``, which owns the comparison they delegate to.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def native_mcp_entry_present(
    runtime: str,
    name: str,
    *,
    stored_config: dict | None,
    project_root: Any,
    user_scope: bool,
) -> bool:
    """Whether *runtime*'s native config already holds an entry for *name*."""
    from apm_cli.core.conflict_detector import MCPConflictDetector
    from apm_cli.factory import ClientFactory

    try:
        client = ClientFactory.create_client(
            runtime, project_root=project_root, user_scope=user_scope
        )
        existing = MCPConflictDetector(client).get_existing_server_configs()
    except Exception:
        logger.debug("Could not read %s MCP config", runtime, exc_info=True)
        return True  # unreadable: never treat as safely absent
    from apm_cli.install.mcp.ownership import _native_config_key

    return _native_config_key(client, name, stored_config=stored_config) in existing


def native_mcp_entry_matches_declaration(
    runtime: str,
    name: str,
    *,
    stored_config: dict | None,
    project_root: Any,
    user_scope: bool,
) -> bool:
    """Whether a user's own entry for *name* already equals its self-defined declaration.

    Such an entry needs no write and stays the user's, so replaying the lock
    has nothing to claim for it. Registry declarations need their payload to
    render and never match here.
    """
    from apm_cli.factory import ClientFactory
    from apm_cli.install.mcp.ownership import (
        NativeMcpConnectionState,
        compare_native_mcp_connection,
    )
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.models.dependency.mcp import MCPDependency

    if not isinstance(stored_config, dict) or stored_config.get("registry") is not False:
        return False
    try:
        dependency = MCPDependency.from_dict(stored_config)
        client = ClientFactory.create_client(
            runtime, project_root=project_root, user_scope=user_scope
        )
        state = compare_native_mcp_connection(
            client,
            name,
            requested_info=MCPIntegrator._build_self_defined_info(dependency),
            stored_config=stored_config,
            managed=False,
        )
    except (TypeError, ValueError):
        logger.debug("Could not compare %s MCP entry %s", runtime, name, exc_info=True)
        return False
    return state is NativeMcpConnectionState.MATCHING_FOREIGN


def differing_unrecorded_native_mcp_entries(
    lockfile: Any,
    target_runtimes: list[str],
    *,
    project_root: Any,
    user_scope: bool,
) -> list[tuple[str, str]]:
    """Return ``(server, runtime)`` pairs ``install --frozen`` refuses for a differing entry.

    A self-defined server whose ownership the lock does not record on a
    runtime that compares native connections, while that runtime already
    holds a same-name entry that differs from the declaration. ``apm audit
    --ci`` reads this so it reaches the same verdict as ``--frozen`` (#4477).
    """
    from apm_cli.install.mcp.ownership import (
        migrate_legacy_project_target_servers,
        native_comparison_clients,
    )

    ownership = {
        runtime: set(names) for runtime, names in (lockfile.mcp_target_servers or {}).items()
    }
    migrate_legacy_project_target_servers(
        ownership, active_runtimes=set(target_runtimes), user_scope=user_scope
    )
    self_defined = {
        name
        for name, stored in (lockfile.mcp_configs or {}).items()
        if isinstance(stored, dict) and stored.get("registry") is False
    }
    if not any(self_defined - ownership.get(runtime, set()) for runtime in target_runtimes):
        # Nothing to compare: building the clients would touch the HTTP cache.
        return []
    clients = native_comparison_clients(
        list(target_runtimes), project_root=project_root, user_scope=user_scope
    )
    pairs: list[tuple[str, str]] = []
    for runtime in sorted(clients):
        recorded = ownership.get(runtime, set())
        for name, stored in sorted((lockfile.mcp_configs or {}).items()):
            if name in recorded or not isinstance(stored, dict):
                continue
            if stored.get("registry") is not False:
                continue
            if not native_mcp_entry_present(
                runtime,
                name,
                stored_config=stored,
                project_root=project_root,
                user_scope=user_scope,
            ):
                continue
            if native_mcp_entry_matches_declaration(
                runtime,
                name,
                stored_config=stored,
                project_root=project_root,
                user_scope=user_scope,
            ):
                continue
            pairs.append((name, runtime))
    return pairs


def edited_recorded_native_mcp_entries(
    lockfile: Any,
    target_runtimes: list[str],
    *,
    project_root: Any,
    user_scope: bool,
    plugin_roots: Mapping[str, str] | None = None,
) -> list[tuple[str, str, str]]:
    """Return ``(server, runtime, config)`` for recorded native MCP entries edited since.

    A self-defined server the lock records for a runtime, with a recorded
    fingerprint, whose native entry no longer proves APM's write: the
    comparator's CONFLICT, the same verdict uninstall refuses on and
    ``install`` preserves (#4757). Its declaration is in the lock, so every
    shape APM may have written is verifiable read-only. Registry servers need
    their registry payload to recognize a ``--frozen`` heal and are not
    compared. *plugin_roots* binds each declaration's ``${CLAUDE_PLUGIN_ROOT}``
    to its current package root, as install does, so an entry ``--frozen``
    re-rendered at a moved root (keeping the old fingerprint) is not an edit.
    Entries without a recorded fingerprint, absent or unreadable entries are
    not reported. Read-only.
    """
    from apm_cli.core.conflict_detector import MCPConflictDetector
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.install.mcp.ownership import (
        NativeMcpConnectionState,
        compare_native_mcp_connection,
        native_comparison_clients,
    )

    stored_configs = lockfile.mcp_configs or {}
    self_defined = {
        name
        for name, stored in stored_configs.items()
        if isinstance(stored, dict) and stored.get("registry") is False
    }
    hashes = DeploymentLedgerCodec.mcp_native_hashes(lockfile)
    recorded = {
        runtime: set(names) & set(hashes.get(runtime, {})) & self_defined
        for runtime, names in (lockfile.mcp_target_servers or {}).items()
        if runtime in target_runtimes
    }
    if not any(recorded.values()):
        # Nothing to compare: building the clients would touch the HTTP cache.
        return []
    clients = native_comparison_clients(
        sorted(runtime for runtime, names in recorded.items() if names),
        project_root=project_root,
        user_scope=user_scope,
    )
    found: dict[tuple[str, str], str] = {}
    for runtime, client in sorted(clients.items()):
        try:
            existing = MCPConflictDetector(client).get_existing_server_configs()
            edited = [
                name
                for name in sorted(recorded[runtime])
                if compare_native_mcp_connection(
                    client,
                    name,
                    requested_info=_bound_render_info(
                        stored_configs.get(name), (plugin_roots or {}).get(name)
                    ),
                    stored_config=stored_configs.get(name),
                    managed=True,
                    existing_configs=existing,
                    native_hash=hashes[runtime][name],
                )
                is NativeMcpConnectionState.CONFLICT
            ]
            config = Path(client.get_config_path()).absolute().as_posix()
        except Exception:
            logger.debug("Could not compare recorded %s MCP entries", runtime, exc_info=True)
            continue
        for name in edited:
            # Runtimes sharing one native file (Claude and Copilot .mcp.json) report it once.
            found.setdefault((name, config), runtime)
    return [(name, runtime, config) for (name, config), runtime in sorted(found.items())]


def _bound_render_info(stored: Any, plugin_root: str | None) -> dict | None:
    """The locked self-defined declaration rendered at its current package root."""
    if not plugin_root or not isinstance(stored, dict):
        return None
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.models.dependency.mcp import MCPDependency

    try:
        return MCPIntegrator._build_self_defined_info(
            MCPDependency.from_dict(stored), plugin_root=plugin_root
        )
    except (TypeError, ValueError):
        logger.debug("Unusable locked declaration %s", stored.get("name"), exc_info=True)
        return None
