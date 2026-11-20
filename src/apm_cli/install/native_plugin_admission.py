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

from apm_cli.core.remediation import (
    AGENT_PLUGIN_EXECUTABLES_UNAPPROVED,
    NATIVE_PLUGIN_CACHE_DIFFERS,
    NATIVE_PLUGIN_HASH_MISSING,
)

if TYPE_CHECKING:
    from pathlib import Path

    from apm_cli.deps.lockfile import LockedDependency
    from apm_cli.install.context import InstallContext
    from apm_cli.install.logger import InstallLogger
    from apm_cli.utils.diagnostics import DiagnosticCollector


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
    )
    from apm_cli.security.trust import agent_plugin_exec_types, native_plugin_exec_types

    # SECURITY: a per-dependency or package target subset that excludes copilot
    # drops native registration; the plugin is not deployable to any other
    # target, so nothing is written.
    if COPILOT_TARGET_NAME not in {getattr(t, "name", t) for t in targets}:
        return result

    install_path = getattr(package_info, "install_path", None)
    record_native_exec_status(
        ctx, package_name, package_info, agent_plugin_exec_types(install_path)
    )

    # For a natively registered plugin, PRESENCE IS DEPLOYMENT: Copilot loads the
    # WHOLE directory live from apm_modules, so every component the trust owner
    # finds (root mcp.json, .apm/ layout, native root layout) gates registration.
    present_types = (
        set(native_plugin_exec_types(Path(install_path))) if install_path is not None else set()
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
        from apm_cli.core.scope import is_user_scope
        from apm_cli.security.trust import approve_command

        label = package_name or "the plugin"
        command = approve_command(label, user_scope=is_user_scope(getattr(ctx, "scope", None)))
        approve = AGENT_PLUGIN_EXECUTABLES_UNAPPROVED.bind(package=label).with_command(command)
        # ONE authoritative refusal owned by the DiagnosticCollector, with the
        # actionable fix folded into the message itself (the summary line is not
        # verbose-gated, so the fix must live there, not in a dim detail line).
        diagnostics.warn(
            "Agent Plugin not registered with GitHub Copilot: its executables "
            f"are not approved. {approve.sentence()}",
            package=package_name,
        )
        return result

    result["native_plugin"] = True
    return result


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
        expected_hash = compute_package_hash(replay_path, framed=lock_dep.content_hash_framed)
    else:
        expected_hash = lock_dep.content_hash
        # Cold-cache CI has already materialized the pinned unit in scratch.
        if not candidate.exists():
            candidate = source
        if not expected_hash:
            raise AgentPluginDeploymentBoundaryError(
                f"Native Agent Plugin {lock_dep.get_unique_key()} has no content hash; "
                f"{NATIVE_PLUGIN_HASH_MISSING.clause()}"
            )
    carried = () if lock_dep.is_local_source else lock_dep.carried_licenses
    if not candidate.is_dir() or not verify_locked_package(
        candidate, expected_hash, carried, framed=lock_dep.content_hash_framed
    ):
        from apm_cli.core.cache_drift import package_cache_drift

        raise AgentPluginDeploymentBoundaryError(
            package_cache_drift(
                f"Native Agent Plugin {candidate}", NATIVE_PLUGIN_CACHE_DIFFERS.clause()
            )
        )
