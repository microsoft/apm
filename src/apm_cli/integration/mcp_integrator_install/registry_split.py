"""Registry/self-defined dependency partitioning for MCP install."""

from __future__ import annotations


def _split_registry_self_defined(mcp_deps: list) -> tuple[list, list, list[str], dict[str, object]]:
    """Split MCP dependencies into registry and self-defined groups."""
    registry_deps = [
        dep
        for dep in mcp_deps
        if isinstance(dep, str)
        or (hasattr(dep, "is_registry_resolved") and dep.is_registry_resolved)
    ]
    self_defined_deps = [
        dep for dep in mcp_deps if hasattr(dep, "is_self_defined") and dep.is_self_defined
    ]
    registry_dep_names = [dep.name if hasattr(dep, "name") else dep for dep in registry_deps]
    registry_dep_map = {dep.name: dep for dep in registry_deps if hasattr(dep, "name")}
    return registry_deps, self_defined_deps, registry_dep_names, registry_dep_map
