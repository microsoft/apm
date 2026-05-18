"""Runtime installation loops for MCP install."""

from __future__ import annotations

from dataclasses import dataclass

from apm_cli.utils.console import STATUS_SYMBOLS

from .opts import RegistryInstallRequest, RuntimeDispatchOpts, RuntimeInstallRequest


@dataclass(frozen=True)
class RuntimeInstallContext:
    """Shared context for runtime installation helpers."""

    mcp_integrator_cls: type
    target_runtimes: list[str]
    console: object
    logger: object
    verbose: bool
    project_root: object
    user_scope: bool


def _runtime_titles(target_runtimes: list[str]) -> str:
    """Return target runtime display text."""
    return ", ".join([rt.title() for rt in target_runtimes])


def _print_already_configured(names: list[str], *, label: str, console, logger) -> None:
    """Print already-configured server messages."""
    if not names:
        return
    if console:
        for name in names:
            console.print(
                f"|  [green]{STATUS_SYMBOLS['check']}[/green] {name} [dim](already configured)[/dim]"
            )
        return
    logger.success(f"{len(names)} {label} server(s) already configured")
    for name in names:
        logger.verbose_detail(f"{name} already configured, skipping")


def _install_for_each_runtime(
    ctx: RuntimeInstallContext,
    request: RuntimeInstallRequest,
) -> bool:
    """Install one MCP server across all target runtimes."""
    action_text = "Updating" if request.is_update else "Configuring"
    if ctx.console:
        detail_text = f" [dim]({request.detail})[/dim]" if request.detail else ""
        ctx.console.print(
            f"|  [cyan]{STATUS_SYMBOLS['running']}[/cyan]  {request.name}{detail_text}"
        )
        ctx.console.print(f"|     +- {action_text} for {_runtime_titles(ctx.target_runtimes)}...")
    else:
        ctx.logger.progress(
            f"{request.name}: {action_text.lower()} for {', '.join(ctx.target_runtimes)}..."
        )

    any_ok = False
    for rt in ctx.target_runtimes:
        if ctx.verbose:
            ctx.logger.verbose_detail(f"Configuring {request.name} for {rt}...")
        if ctx.mcp_integrator_cls._install_for_runtime(
            rt,
            request.install_names,
            RuntimeDispatchOpts(
                shared_env_vars=request.env_vars,
                server_info_cache=request.server_info_cache,
                shared_runtime_vars=request.runtime_vars,
                project_root=ctx.project_root,
                user_scope=ctx.user_scope,
                logger=ctx.logger,
            ),
        ):
            any_ok = True
    return any_ok


def _render_install_result(
    ctx: RuntimeInstallContext, name: str, *, any_ok: bool, is_update: bool
) -> bool:
    """Render an install result and return whether it counted as configured."""
    if any_ok:
        if ctx.console:
            label = "updated" if is_update else "configured"
            ctx.console.print(
                f"|  [green]{STATUS_SYMBOLS['check']}[/green]  {name} -> "
                f"{_runtime_titles(ctx.target_runtimes)} [dim]({label})[/dim]"
            )
        return True
    if ctx.console:
        ctx.console.print(
            f"|  [red]{STATUS_SYMBOLS['cross']}[/red]  {name}  -- failed for all runtimes"
        )
    else:
        ctx.logger.error(f"{name} -- failed for all runtimes")
    return False


def _validate_registry_servers(ctx: RuntimeInstallContext, request: RegistryInstallRequest):
    """Validate registry-backed MCP server names."""
    try:
        from apm_cli.registry.operations import MCPServerOperations
    except ImportError:
        ctx.logger.warning("Registry operations not available")
        ctx.logger.error("Cannot validate MCP servers without registry operations")
        raise RuntimeError("Registry operations module required for MCP installation")

    operations = MCPServerOperations()
    ctx.logger.mcp_lookup_heartbeat(len(request.registry_dep_names))
    if ctx.verbose:
        ctx.logger.verbose_detail(f"Validating {len(request.registry_deps)} registry servers...")
    valid_servers, invalid_servers = operations.validate_servers_exist(request.registry_dep_names)
    if invalid_servers:
        ctx.logger.error(f"Server(s) not found in registry: {', '.join(invalid_servers)}")
        ctx.logger.progress("Run 'apm mcp search <query>' to find available servers")
        raise RuntimeError(f"Cannot install {len(invalid_servers)} missing server(s)")
    return operations, valid_servers


def _build_registry_install_plan(
    ctx: RuntimeInstallContext,
    request: RegistryInstallRequest,
    *,
    operations,
    valid_servers: list[str],
) -> tuple[list[str], list[str], dict, dict | None]:
    """Return servers to install plus shared config maps."""
    servers_to_install = operations.check_servers_needing_installation(
        ctx.target_runtimes,
        valid_servers,
        project_root=ctx.project_root,
        user_scope=ctx.user_scope,
    )
    already_candidates = [dep for dep in valid_servers if dep not in servers_to_install]
    if request.stored_mcp_configs and already_candidates:
        drifted_deps = [
            request.registry_dep_map[name]
            for name in already_candidates
            if name in request.registry_dep_map
        ]
        drifted = ctx.mcp_integrator_cls._detect_mcp_config_drift(
            drifted_deps,
            request.stored_mcp_configs,
        )
        if drifted:
            request.servers_to_update.update(drifted)
            ctx.mcp_integrator_cls._append_drifted_to_install_list(servers_to_install, drifted)
    already_configured = [dep for dep in already_candidates if dep not in request.servers_to_update]
    if not servers_to_install:
        return [], already_configured, {}, None

    if ctx.verbose:
        ctx.logger.verbose_detail(f"Installing {len(servers_to_install)} servers...")
    server_info_cache = operations.batch_fetch_server_info(servers_to_install)
    for server_name in servers_to_install:
        dep = request.registry_dep_map.get(server_name)
        if dep:
            ctx.mcp_integrator_cls._apply_overlay(server_info_cache, dep)
    shared_env_vars = operations.collect_environment_variables(
        servers_to_install, server_info_cache
    )
    for server_name in servers_to_install:
        dep = request.registry_dep_map.get(server_name)
        if dep and dep.env:
            shared_env_vars.update(dep.env)
    shared_runtime_vars = operations.collect_runtime_variables(
        servers_to_install, server_info_cache
    )
    return servers_to_install, already_configured, shared_env_vars, shared_runtime_vars


def _install_registry_deps(
    ctx: RuntimeInstallContext,
    request: RegistryInstallRequest,
) -> int:
    """Install registry-backed MCP dependencies."""
    if not request.registry_dep_names:
        return 0
    operations, valid_servers = _validate_registry_servers(ctx, request)
    if not valid_servers:
        return 0

    (
        servers_to_install,
        already_configured,
        shared_env_vars,
        shared_runtime_vars,
    ) = _build_registry_install_plan(
        ctx,
        request,
        operations=operations,
        valid_servers=valid_servers,
    )
    _print_already_configured(
        already_configured,
        label="registry MCP",
        console=ctx.console,
        logger=ctx.logger,
    )
    if not servers_to_install:
        return 0

    server_info_cache = operations.batch_fetch_server_info(servers_to_install)
    configured_count = 0
    for dep in servers_to_install:
        is_update = dep in request.servers_to_update
        any_ok = _install_for_each_runtime(
            ctx,
            RuntimeInstallRequest(
                name=dep,
                install_names=[dep],
                env_vars=shared_env_vars,
                server_info_cache=server_info_cache,
                runtime_vars=shared_runtime_vars,
                is_update=is_update,
            ),
        )
        if _render_install_result(ctx, dep, any_ok=any_ok, is_update=is_update):
            configured_count += 1
            if is_update:
                request.successful_updates.add(dep)
    return configured_count


def _install_self_defined(
    ctx: RuntimeInstallContext,
    *,
    self_defined_deps: list,
    stored_mcp_configs: dict,
    servers_to_update: set,
    successful_updates: set,
) -> int:
    """Install self-defined MCP dependencies."""
    if not self_defined_deps:
        return 0
    names = [dep.name for dep in self_defined_deps]
    to_install = ctx.mcp_integrator_cls._check_self_defined_servers_needing_installation(
        names,
        ctx.target_runtimes,
        project_root=ctx.project_root,
        user_scope=ctx.user_scope,
    )
    already_candidates = [name for name in names if name not in to_install]
    if stored_mcp_configs and already_candidates:
        drifted_deps = [dep for dep in self_defined_deps if dep.name in already_candidates]
        drifted = ctx.mcp_integrator_cls._detect_mcp_config_drift(drifted_deps, stored_mcp_configs)
        if drifted:
            servers_to_update.update(drifted)
            ctx.mcp_integrator_cls._append_drifted_to_install_list(to_install, drifted)
    already_configured = [name for name in already_candidates if name not in servers_to_update]
    _print_already_configured(
        already_configured, label="self-defined", console=ctx.console, logger=ctx.logger
    )

    configured_count = 0
    for dep in self_defined_deps:
        if dep.name not in to_install:
            continue
        is_update = dep.name in servers_to_update
        any_ok = _install_for_each_runtime(
            ctx,
            RuntimeInstallRequest(
                name=dep.name,
                install_names=[dep.name],
                env_vars=dep.env or {},
                server_info_cache={dep.name: ctx.mcp_integrator_cls._build_self_defined_info(dep)},
                is_update=is_update,
                detail=f"self-defined, {dep.transport or 'stdio'}",
            ),
        )
        if _render_install_result(ctx, dep.name, any_ok=any_ok, is_update=is_update):
            configured_count += 1
            if is_update:
                successful_updates.add(dep.name)
    return configured_count
