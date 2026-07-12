"""Forward migration for per-target MCP deployment ownership."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def adopt_legacy_mcp_target_servers(
    *,
    server_names: set[str],
    stored_configs: dict[str, dict],
    project_root,
    user_scope: bool,
) -> dict[str, set[str]]:
    """Adopt legacy native entries only when they exactly match their baseline."""
    from apm_cli.core.conflict_detector import MCPConflictDetector
    from apm_cli.factory import ClientFactory
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.models.dependency.mcp import MCPDependency

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
            existing = MCPConflictDetector(client).get_existing_server_configs()
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
            if existing.get(name) == expected:
                adopted.setdefault(runtime, set()).add(name)
    return adopted
