"""MCP ``install`` orchestration."""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING

from apm_cli.core.null_logger import NullCommandLogger

from .lockfile_finalize import _finalize_lockfile
from .registry_split import _split_registry_self_defined
from .runtime_install import RuntimeInstallContext, _install_registry_deps, _install_self_defined
from .runtime_resolve import _resolve_runtimes

if TYPE_CHECKING:
    from apm_cli.core.scope import InstallScope


def _print_mcp_header(console, logger, dep_count: int) -> None:
    """Render the MCP section header."""
    if not console:
        logger.progress(f"Installing MCP dependencies ({dep_count})...")
        return
    try:
        from rich.text import Text

        header = Text()
        header.append("+- MCP Servers (", style="cyan")
        header.append(str(dep_count), style="cyan bold")
        header.append(")", style="cyan")
        console.print(header)
    except Exception:
        logger.progress(f"Installing MCP dependencies ({dep_count})...")


def run_mcp_install(
    mcp_deps: list,
    runtime: str | None = None,
    exclude: str | None = None,
    verbose: bool = False,
    apm_config: dict | None = None,
    stored_mcp_configs: dict | None = None,
    project_root=None,
    user_scope: bool = False,
    explicit_target: str | None = None,
    logger=None,
    diagnostics=None,
    scope: InstallScope | None = None,
) -> int:
    """Install MCP dependencies."""
    from apm_cli.core.scope import InstallScope
    from apm_cli.integration.mcp_integrator import MCPIntegrator, _get_console, _is_vscode_available

    if logger is None:
        logger = NullCommandLogger()
    if not mcp_deps:
        logger.warning("No MCP dependencies found in apm.yml")
        return 0

    if scope is InstallScope.USER:
        user_scope = True
    elif scope is InstallScope.PROJECT:
        user_scope = False

    registry_deps, self_defined_deps, registry_dep_names, registry_dep_map = (
        _split_registry_self_defined(mcp_deps)
    )
    console = _get_console()
    servers_to_update: builtins.set = builtins.set()
    successful_updates: builtins.set = builtins.set()
    stored_mcp_configs = stored_mcp_configs or {}

    _print_mcp_header(console, logger, len(mcp_deps))
    target_runtimes, apm_config = _resolve_runtimes(
        runtime=runtime,
        exclude=exclude,
        verbose=verbose,
        apm_config=apm_config,
        project_root=project_root,
        user_scope=user_scope,
        explicit_target=explicit_target,
        scope=scope,
        logger=logger,
        console=console,
        mcp_integrator_cls=MCPIntegrator,
        is_vscode_available=_is_vscode_available,
    )
    if not target_runtimes:
        return 0

    install_ctx = RuntimeInstallContext(
        mcp_integrator_cls=MCPIntegrator,
        target_runtimes=target_runtimes,
        console=console,
        logger=logger,
        verbose=verbose,
        project_root=project_root,
        user_scope=user_scope,
    )
    configured_count = _install_registry_deps(
        install_ctx,
        registry_deps=registry_deps,
        registry_dep_names=registry_dep_names,
        registry_dep_map=registry_dep_map,
        stored_mcp_configs=stored_mcp_configs,
        servers_to_update=servers_to_update,
        successful_updates=successful_updates,
    )
    configured_count += _install_self_defined(
        install_ctx,
        self_defined_deps=self_defined_deps,
        stored_mcp_configs=stored_mcp_configs,
        servers_to_update=servers_to_update,
        successful_updates=successful_updates,
    )
    _finalize_lockfile(configured_count, successful_updates, console)
    return configured_count
