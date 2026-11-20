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
    from pathlib import Path

    from apm_cli.deps.lockfile import LockedDependency
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
    """Exec trust types of NATIVE-layout root components (see the trust owner)."""
    from apm_cli.security.trust import native_root_exec_types

    return native_root_exec_types(install_path)


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
    from pathlib import Path

    from apm_cli.install.exec_gate import resolve_package_key
    from apm_cli.security.executables import more_severe_exec_status
    from apm_cli.security.trust import package_exec_status

    candidate_keys = [resolve_package_key(package_info, package_name)]
    if package_name and package_name not in candidate_keys:
        candidate_keys.append(package_name)
    install_path = getattr(package_info, "install_path", None)
    status = package_exec_status(
        trust_ctx,
        tuple(candidate_keys),
        Path(install_path) if install_path is not None else None,
        exec_types,
        native=True,
    )
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
    # WHOLE deployed copy of the plugin root, so an unapproved hooks/agents/canvas/
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
    queue_native_agent_plugin(ctx, package_name, package_info, diagnostics)
    return result


def queue_native_agent_plugin(
    ctx: InstallContext | None,
    package_name: str,
    package_info: Any,
    diagnostics: DiagnosticCollector,
) -> None:
    """Hand an admitted Agent Plugin to the shared native owner (#3921).

    Copilot loads it from the deployed profile root like any other native
    plugin; the lock records its root and digest. A plugin whose name is not
    a safe path segment, or whose root holds a symlink, is refused with one
    diagnostic and never declared (req-na-002).
    """
    if ctx is None:
        return
    from pathlib import Path

    from apm_cli.claude_plugins.admission import (
        ClaudeNativePlugin,
        ClaudeNativePluginError,
        fingerprint_native_claude_source,
        queue_native_claude,
    )
    from apm_cli.core.scope import is_user_scope
    from apm_cli.native_plugins.record import NativePluginRecordError, is_safe_plugin_name
    from apm_cli.native_plugins.scopes import COPILOT

    package = getattr(package_info, "package", None)
    plugin = getattr(package, "agent_plugin", None)
    name = getattr(getattr(plugin, "identity", None), "name", None)
    root = Path(package_info.install_path)
    try:
        if not is_safe_plugin_name(name):
            raise NativePluginRecordError(f"its name {name!r} is not a safe plugin name")
        fingerprint = fingerprint_native_claude_source(
            root, portable=not is_user_scope(getattr(ctx, "scope", None))
        )
    except (ClaudeNativePluginError, NativePluginRecordError) as exc:
        diagnostics.warn(
            f"Agent Plugin not activated for GitHub Copilot: {exc}. Fix the plugin, or "
            "exclude copilot from this dependency's targets.",
            package=package_name,
        )
        return
    queue_native_claude(
        ctx,
        package_name,
        ClaudeNativePlugin(
            package_name, name, root.resolve(), fingerprint, harnesses=frozenset({COPILOT})
        ),
        diagnostics,
    )


def verify_native_plugin_cache(
    lock_dep: LockedDependency,
    source: Path,
    replay_path: Path,
    live_modules: Path,
    scratch_root: Path,
) -> None:
    """Compare an admitted native unit without inventing deployed-file claims.

    Local replay images are written at ``replay_path`` by install's own
    local package image owner (``materialize_local_package``), so in-package
    symlinks already carry exactly the materialized bytes. Remote units use
    the existing locked package digest; a pin marker alone cannot prove their
    file contents.
    """
    from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError
    from apm_cli.deps.carried_license import verify_locked_package
    from apm_cli.utils.content_hash import compute_package_hash
    from apm_cli.utils.path_security import ensure_path_within

    candidate = lock_dep.to_dependency_ref().get_install_path(live_modules)
    ensure_path_within(candidate, live_modules)
    if lock_dep.is_local_source:
        ensure_path_within(replay_path, scratch_root)
        expected_hash = compute_package_hash(replay_path)
    else:
        expected_hash = lock_dep.content_hash
        # Cold-cache CI has already materialized the pinned unit in scratch.
        if not candidate.exists():
            candidate = source
        if not expected_hash:
            raise AgentPluginDeploymentBoundaryError(
                f"Native Agent Plugin {lock_dep.get_unique_key()} has no content hash; "
                "run 'apm install' to regenerate its integrity baseline"
            )
    carried = () if lock_dep.is_local_source else lock_dep.carried_licenses
    if not candidate.is_dir() or not verify_locked_package(candidate, expected_hash, carried):
        raise AgentPluginDeploymentBoundaryError(
            f"Native Agent Plugin cache differs from its replay baseline: {candidate}; "
            "run 'apm install' to restore the package"
        )
