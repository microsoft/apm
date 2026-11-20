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
