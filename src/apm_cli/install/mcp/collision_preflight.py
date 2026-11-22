"""Project native MCP collisions before an install writes anything (#4268).

The commit gate (install/commit_gate.py) calls :func:`project_native_mcp_conflicts`
before the first deployed byte. It asks the canonical comparator
(:func:`~apm_cli.install.mcp.ownership.compare_native_mcp_connection`) about
every self-defined server the install will write, and reports a
``CONFLICT`` with the same diagnostic the MCP write path renders. The
projection only flags pairs whose answer cannot depend on ownership state:
servers the lockfile never recorded, compared as unmanaged. Anything else
(recorded claims, registry servers whose render needs the registry) is left
to the MCP write path, which keeps the same comparison as its backstop.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def recorded_mcp_names(lockfile: Any) -> frozenset[str]:
    """Every MCP server name *lockfile* records ownership or a declaration for."""
    if lockfile is None:
        return frozenset()
    names = set(lockfile.mcp_servers or ()) | set(lockfile.mcp_configs or {})
    for servers in (lockfile.mcp_target_servers or {}).values():
        names.update(servers)
    return frozenset(names)


def project_native_mcp_conflicts(
    dependencies: Iterable[Any],
    target_runtimes: list[str],
    *,
    recorded: frozenset[str],
    project_root: Any,
    user_scope: bool,
    logger: Any,
) -> list[str]:
    """Report each ``name (runtime)`` collision the MCP write would hit, without writing."""
    from apm_cli.adapters.client._remote_shapes import unsupported_remote_transport
    from apm_cli.install.mcp.native_runtimes import native_runtimes_of
    from apm_cli.install.mcp.ownership import (
        NativeMcpConnectionState,
        compare_native_mcp_connection,
        native_comparison_clients,
    )
    from apm_cli.integration.mcp_integrator import MCPIntegrator
    from apm_cli.integration.mcp_integrator_install import (
        _codex_expanded_env_names,
        _report_native_conflict,
        mcp_dependency_runtimes,
    )

    clients = native_comparison_clients(
        target_runtimes, project_root=project_root, user_scope=user_scope
    )
    conflicts: list[str] = []
    for dep in dependencies:
        if not getattr(dep, "is_self_defined", False) or dep.name in recorded:
            continue
        info = MCPIntegrator._build_self_defined_info(dep)
        for runtime in mcp_dependency_runtimes(dep, target_runtimes):
            client = clients.get(runtime)
            if (
                client is None
                or runtime in native_runtimes_of(dep)
                or unsupported_remote_transport(client, info) is not None
            ):
                continue
            state = compare_native_mcp_connection(
                client,
                dep.name,
                requested_info=info,
                stored_config=None,
                managed=False,
                declaration_changed=False,
            )
            if state is NativeMcpConnectionState.CONFLICT:
                _report_native_conflict(
                    dep.name,
                    runtime,
                    client,
                    logger,
                    conflicts,
                    expanded_env=_codex_expanded_env_names(client, dep.name, info),
                    render=lambda client=client, info=info: client.render_server_config(info),
                )
    return conflicts
