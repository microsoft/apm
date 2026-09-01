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


def plugin_root_exec_types(install_path: Any) -> tuple[str, ...]:
    """Return exec trust types implied by NATIVE-layout components at the root.

    ``scan_package_executables`` only understands APM's own ``.apm/`` layout
    (``.apm/hooks/*.json``, ``bin/``, ``apm.yml`` mcp/lsp declarations,
    ``.apm/extensions/<n>/extension.mjs``). An Agent Plugins v1 package is
    materialized in the NATIVE root layout instead, so the very components
    :data:`~apm_cli.agent_plugins.loader._IGNORED_PORTABLE_COMPONENT_PATHS`
    records as present-but-unmodelled live at the plugin ROOT, where that
    scanner never looks. Under this module's PRESENCE IS DEPLOYMENT premise --
    Copilot loads the whole directory live -- an unapproved root
    ``extensions/``, ``lsp.json`` or ``hooks/`` would otherwise execute with no
    exec approval at all.

    The mapping is driven off the loader's own constant so the two can never
    drift: any unmapped ignored component still yields a trust type, so a new
    entry there fails closed here rather than silently widening admission.
    """
    from pathlib import Path

    from apm_cli.agent_plugins.loader import _IGNORED_PORTABLE_COMPONENT_PATHS
    from apm_cli.security.executables import (
        EXEC_TYPE_CANVAS,
        EXEC_TYPE_HOOKS,
        EXEC_TYPE_LSP,
    )

    if install_path is None:
        return ()
    root = Path(install_path)
    mapping = {
        "extensions": EXEC_TYPE_CANVAS,
        "lsp.json": EXEC_TYPE_LSP,
        "hooks": EXEC_TYPE_HOOKS,
        # agents/commands/instructions carry prompt payloads Copilot loads
        # live; they are not shell-executable, but an unapproved hook payload
        # can hide beside them, so they map to the hooks gate rather than
        # silently to nothing.
        "agents": EXEC_TYPE_HOOKS,
        "commands": EXEC_TYPE_HOOKS,
        "instructions": EXEC_TYPE_HOOKS,
    }
    present: list[str] = []
    for name in _IGNORED_PORTABLE_COMPONENT_PATHS:
        try:
            if not (root / name).exists():
                continue
        except OSError:
            continue
        present.append(mapping.get(name, EXEC_TYPE_HOOKS))
    seen: dict[str, None] = {}
    for exec_type in present:
        seen.setdefault(exec_type, None)
    return tuple(seen)


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
    from apm_cli.security.executables import (
        exec_status_for_declaration,
        more_severe_exec_status,
    )

    candidate_keys = [resolve_package_key(package_info, package_name)]
    if package_name and package_name not in candidate_keys:
        candidate_keys.append(package_name)
    status = exec_status_for_declaration(trust_ctx, candidate_keys, exec_types)
    if status is not None:
        # FOLD, never assign: the exec gate already recorded the worst-case
        # status over ALL exec types found on disk (hooks, canvas, lsp, ...),
        # but the IR-derived status here sees only MCP/BIN. A partial grant
        # (e.g. mcp approved, hooks unbounded) must not downgrade a gated/denied
        # verdict into ``deployed`` -- keep the more severe of the two.
        existing = ctx.package_exec_status.get(package_name)
        ctx.package_exec_status[package_name] = more_severe_exec_status(existing, status)


def finalize_native_plugin(
    result: dict,
    package_info: Any,
    package_name: str,
    targets: Any,
    *,
    hooks_approved: bool,
    mcp_approved: bool,
    bin_approved: bool,
    canvas_approved: bool,
    lsp_approved: bool,
    ctx: InstallContext | None,
    diagnostics: DiagnosticCollector,
    logger: InstallLogger | None,
) -> dict:
    """Admit or refuse a native Agent Plugin after the shared gates ran."""
    from pathlib import Path

    from apm_cli.copilot_plugins.constants import COPILOT_TARGET_NAME
    from apm_cli.security.executables import (
        EXEC_TYPE_BIN,
        EXEC_TYPE_CANVAS,
        EXEC_TYPE_HOOKS,
        EXEC_TYPE_LSP,
        EXEC_TYPE_MCP,
        scan_package_executables,
    )

    # SECURITY: a per-dependency or package target subset that excludes copilot
    # drops native registration; the plugin is not deployable to any other
    # target, so nothing is written.
    if COPILOT_TARGET_NAME not in {getattr(t, "name", t) for t in targets}:
        return result

    ir_exec_types = agent_plugin_exec_types(package_info)
    record_native_exec_status(ctx, package_name, package_info, ir_exec_types)

    # For a natively registered plugin, PRESENCE IS DEPLOYMENT: Copilot loads the
    # WHOLE directory live from apm_modules, so an unapproved hooks/agents/canvas/
    # lsp component the IR does not model still executes. Gate on the UNION of the
    # IR exec types (mcp/bin), every exec type scan_package_executables finds in
    # APM's own .apm/ layout, and every NATIVE root-layout component the scanner
    # is blind to (see plugin_root_exec_types).
    on_disk_types: tuple[str, ...] = ()
    install_path = getattr(package_info, "install_path", None)
    if install_path is not None:
        package = getattr(package_info, "package", None)
        version = getattr(package, "version", "") or "" if package is not None else ""
        on_disk_types = scan_package_executables(
            Path(install_path), package_name, version
        ).exec_types
    present_types = (
        set(ir_exec_types) | set(on_disk_types) | set(plugin_root_exec_types(install_path))
    )

    approvals = {
        EXEC_TYPE_MCP: mcp_approved,
        EXEC_TYPE_BIN: bin_approved,
        EXEC_TYPE_HOOKS: hooks_approved,
        EXEC_TYPE_CANVAS: canvas_approved,
        EXEC_TYPE_LSP: lsp_approved,
    }
    # Fail closed: an exec type present on disk that is not in the approval map
    # is treated as unapproved.
    denied = any(not approvals.get(exec_type, False) for exec_type in present_types)
    if denied:
        label = package_name or "the plugin"
        # ONE authoritative refusal owned by the DiagnosticCollector, with the
        # actionable fix folded into the message itself (the summary line is not
        # verbose-gated, so the fix must live there, not in a dim detail line).
        diagnostics.warn(
            "Agent Plugin not registered with GitHub Copilot: its executables "
            f"are not approved. Run 'apm approve {label}' to trust and register it.",
            package=package_name,
        )
        return result

    result["native_plugin"] = True
    return result
