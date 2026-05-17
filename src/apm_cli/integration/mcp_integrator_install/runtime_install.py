"""Runtime installation loops for MCP install."""

from __future__ import annotations

from dataclasses import dataclass

from apm_cli.utils.console import STATUS_SYMBOLS


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
    *,
    name: str,
    install_names: list[str],
    env_vars: dict,
    server_info_cache: dict,
    runtime_vars: dict | None = None,
    is_update: bool = False,
    detail: str | None = None,
) -> bool:
    """Install one MCP server across all target runtimes."""
    action_text = "Updating" if is_update else "Configuring"
    if ctx.console:
        detail_text = f" [dim]({detail})[/dim]" if detail else ""
        ctx.console.print(f"|  [cyan]{STATUS_SYMBOLS['running']}[/cyan]  {name}{detail_text}")
        ctx.console.print(f"|     +- {action_text} for {_runtime_titles(ctx.target_runtimes)}...")
    else:
        ctx.logger.progress(
            f"{name}: {action_text.lower()} for {', '.join(ctx.target_runtimes)}..."
        )

    any_ok = False
    for rt in ctx.target_runtimes:
        if ctx.verbose:
            ctx.logger.verbose_detail(f"Configuring {name} for {rt}...")
        kwargs = {
            "project_root": ctx.project_root,
            "user_scope": ctx.user_scope,
            "logger": ctx.logger,
        }
        if runtime_vars is not None:
            kwargs["runtime_vars"] = runtime_vars
        if ctx.mcp_integrator_cls._install_for_runtime(
            rt,
            install_names,
            env_vars,
            server_info_cache,
            **kwargs,
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


def _install_registry_deps(
    ctx: RuntimeInstallContext,
    *,
    registry_deps: list,
    registry_dep_names: list[str],
    registry_dep_map: dict[str, object],
    stored_mcp_configs: dict,
    servers_to_update: set,
    successful_updates: set,
) -> int:
    """Install registry-backed MCP dependencies."""
    if not registry_dep_names:
        return 0
    try:
        from apm_cli.registry.operations import MCPServerOperations
    except ImportError:
        ctx.logger.warning("Registry operations not available")
        ctx.logger.error("Cannot validate MCP servers without registry operations")
        raise RuntimeError("Registry operations module required for MCP installation")  # noqa: B904

    operations = MCPServerOperations()
    ctx.logger.mcp_lookup_heartbeat(len(registry_dep_names))
    if ctx.verbose:
        ctx.logger.verbose_detail(f"Validating {len(registry_deps)} registry servers...")
    valid_servers, invalid_servers = operations.validate_servers_exist(registry_dep_names)
    if invalid_servers:
        ctx.logger.error(f"Server(s) not found in registry: {', '.join(invalid_servers)}")
        ctx.logger.progress("Run 'apm mcp search <query>' to find available servers")
        raise RuntimeError(f"Cannot install {len(invalid_servers)} missing server(s)")
    if not valid_servers:
        return 0

    servers_to_install = operations.check_servers_needing_installation(
        ctx.target_runtimes,
        valid_servers,
        project_root=ctx.project_root,
        user_scope=ctx.user_scope,
    )
    already_candidates = [dep for dep in valid_servers if dep not in servers_to_install]
    if stored_mcp_configs and already_candidates:
        drifted_deps = [registry_dep_map[n] for n in already_candidates if n in registry_dep_map]
        drifted = ctx.mcp_integrator_cls._detect_mcp_config_drift(drifted_deps, stored_mcp_configs)
        if drifted:
            servers_to_update.update(drifted)
            ctx.mcp_integrator_cls._append_drifted_to_install_list(servers_to_install, drifted)
    already_configured = [dep for dep in already_candidates if dep not in servers_to_update]
    if not servers_to_install:
        _print_already_configured(
            already_configured, label="registry MCP", console=ctx.console, logger=ctx.logger
        )
        return 0
    _print_already_configured(
        already_configured, label="registry MCP", console=ctx.console, logger=ctx.logger
    )

    if ctx.verbose:
        ctx.logger.verbose_detail(f"Installing {len(servers_to_install)} servers...")
    server_info_cache = operations.batch_fetch_server_info(servers_to_install)
    for server_name in servers_to_install:
        dep = registry_dep_map.get(server_name)
        if dep:
            ctx.mcp_integrator_cls._apply_overlay(server_info_cache, dep)
    shared_env_vars = operations.collect_environment_variables(
        servers_to_install, server_info_cache
    )
    for server_name in servers_to_install:
        dep = registry_dep_map.get(server_name)
        if dep and dep.env:
            shared_env_vars.update(dep.env)
    shared_runtime_vars = operations.collect_runtime_variables(
        servers_to_install, server_info_cache
    )

    configured_count = 0
    for dep in servers_to_install:
        is_update = dep in servers_to_update
        any_ok = _install_for_each_runtime(
            ctx,
            name=dep,
            install_names=[dep],
            env_vars=shared_env_vars,
            server_info_cache=server_info_cache,
            runtime_vars=shared_runtime_vars,
            is_update=is_update,
        )
        if _render_install_result(ctx, dep, any_ok=any_ok, is_update=is_update):
            configured_count += 1
            if is_update:
                successful_updates.add(dep)
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
            name=dep.name,
            install_names=[dep.name],
            env_vars=dep.env or {},
            server_info_cache={dep.name: ctx.mcp_integrator_cls._build_self_defined_info(dep)},
            is_update=is_update,
            detail=f"self-defined, {dep.transport or 'stdio'}",
        )
        if _render_install_result(ctx, dep.name, any_ok=any_ok, is_update=is_update):
            configured_count += 1
            if is_update:
                successful_updates.add(dep.name)
    return configured_count
