"""Generate native MCP cleanup authority through the real write producer."""

from pathlib import Path

from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.dependency.mcp import MCPDependency


def install_managed_mcp(
    project_root: Path, runtime: str, name: str, *, user_scope: bool = False
) -> tuple[dict, dict[str, dict[str, str]]]:
    """Return the declaration and receipt for an actual inert native write."""
    dependency = MCPDependency(
        name=name, registry=False, transport="stdio", command="echo", args=["fixture"]
    )
    written: set[str] = set()
    hashes: dict[str, str] = {}
    assert MCPIntegrator._install_for_runtime(
        runtime,
        [name],
        server_info_cache={name: MCPIntegrator._build_self_defined_info(dependency)},
        project_root=project_root,
        user_scope=user_scope,
        installed_servers=written,
        written_native_hashes=hashes,
    )
    assert written == {name}
    assert set(hashes) == {name}
    return {name: dependency.to_dict()}, {runtime: hashes}
