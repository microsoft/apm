"""Whether a runtime's native config already holds a declared MCP entry.

Read-only probes the frozen lock replay asks before it claims a server; split
from ``install/mcp/ownership.py``, which owns the comparison they delegate to.
"""

from __future__ import annotations

import logging
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
