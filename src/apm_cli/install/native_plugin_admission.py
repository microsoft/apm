"""Native Agent Plugin admission after the shared install gates.

Extracted from ``services.py`` to stay within the LOC budget. These helpers
decide whether a verified Agent Plugin may be handed to GitHub Copilot as a
live natively loaded plugin, once ``integrate_package_primitives`` has already
run the canonical target filter, executable trust gate, and pre-deploy scan.

Copilot loads the whole plugin unit live, so an Agent Plugin's ``mcp_servers``
(arbitrary local execution) and bin executables cannot be partially deployed:
if the trust gate did not clear them, the plugin is not registered natively at
all. The lockfile ``exec_status`` is recorded either way so the audit never
treats the package as trusted-by-default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext
    from apm_cli.install.logger import InstallLogger
    from apm_cli.utils.diagnostics import DiagnosticCollector


def agent_plugin_exec_types(package_info: Any) -> tuple[str, ...]:
    """Return the executable trust types an Agent Plugin's IR declares."""
    from apm_cli.security.executables import EXEC_TYPE_BIN, EXEC_TYPE_MCP

    package = getattr(package_info, "package", None)
    plugin = getattr(package, "agent_plugin", None) if package is not None else None
    components = getattr(plugin, "components", None)
    servers = getattr(components, "mcp_servers", ()) if components is not None else ()
    types: list[str] = []
    if servers:
        types.append(EXEC_TYPE_MCP)
    if any(getattr(server, "executables", ()) for server in servers):
        types.append(EXEC_TYPE_BIN)
    return tuple(types)


def record_native_exec_status(
    ctx: InstallContext | None,
    package_name: str,
    package_info: Any,
    exec_types: tuple[str, ...],
) -> None:
    """Record the lockfile ``exec_status`` for a native plugin's exec surface."""
    if ctx is None or not exec_types:
        return
    trust_ctx = getattr(ctx, "exec_trust_ctx", None)
    if trust_ctx is None:
        return
    from apm_cli.install.exec_gate import resolve_package_key
    from apm_cli.security.executables import exec_status_for_declaration

    candidate_keys = [resolve_package_key(package_info, package_name)]
    if package_name and package_name not in candidate_keys:
        candidate_keys.append(package_name)
    status = exec_status_for_declaration(trust_ctx, candidate_keys, exec_types)
    if status is not None:
        ctx.package_exec_status[package_name] = status


def finalize_native_plugin(
    result: dict,
    package_info: Any,
    package_name: str,
    targets: Any,
    *,
    mcp_approved: bool,
    bin_approved: bool,
    ctx: InstallContext | None,
    diagnostics: DiagnosticCollector,
    logger: InstallLogger | None,
) -> dict:
    """Admit or refuse a native Agent Plugin after the shared gates ran."""
    from apm_cli.copilot_plugins.constants import COPILOT_TARGET_NAME
    from apm_cli.security.executables import EXEC_TYPE_BIN, EXEC_TYPE_MCP

    # SECURITY: a per-dependency or package target subset that excludes copilot
    # drops native registration; the plugin is not deployable to any other
    # target, so nothing is written.
    if COPILOT_TARGET_NAME not in {getattr(t, "name", t) for t in targets}:
        return result

    exec_types = agent_plugin_exec_types(package_info)
    record_native_exec_status(ctx, package_name, package_info, exec_types)

    denied = (EXEC_TYPE_MCP in exec_types and not mcp_approved) or (
        EXEC_TYPE_BIN in exec_types and not bin_approved
    )
    if denied:
        label = package_name or "the plugin"
        diagnostics.warn(
            "Agent Plugin not registered with GitHub Copilot: its executables are not approved",
            package=package_name,
            detail=f"run 'apm approve {label}' to trust and register it",
        )
        if logger is not None:
            logger.tree_item(
                f"  |-- Agent Plugin skipped (executables not approved). "
                f"Run 'apm approve {label}' to register it with Copilot."
            )
        return result

    result["native_plugin"] = True
    return result
