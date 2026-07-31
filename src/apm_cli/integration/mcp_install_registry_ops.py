"""Registry-group MCP install execution (strangler-fig sibling of ``mcp_integrator_install``).

Owns the per-registry-group install loop lifted out of
``mcp_integrator_install`` for the module line budget. ``MCPIntegrator`` is
imported lazily inside the function so the heavily-patched
``MCPIntegrator.<name>`` seam is resolved at call time, unchanged.
"""

from __future__ import annotations

import builtins
from typing import Any

from apm_cli.integration._shared import _RegistryDepGroup
from apm_cli.utils.console import STATUS_SYMBOLS


def _record_managed_server(
    managed_target_servers: dict[str, builtins.set[str]] | None,
    runtime: str,
    server_name: str,
) -> None:
    """Record a server only after APM successfully writes its target config."""
    if managed_target_servers is not None:
        managed_target_servers.setdefault(runtime, set()).add(server_name)


def _install_registry_group(
    operations: Any,
    group: _RegistryDepGroup,
    target_runtimes: list,
    stored_mcp_configs: dict,
    servers_to_update: builtins.set,
    successful_updates: builtins.set,
    project_root: Any,
    user_scope: bool,
    verbose: bool,
    console: Any,
    logger: Any,
    managed_target_servers: dict[str, builtins.set[str]] | None,
) -> int:
    """Process one group of registry deps through a single ``MCPServerOperations`` instance.

    All deps in ``group.deps`` share the same target registry (either the
    default or a per-dep override URL).  ``servers_to_update`` and
    ``successful_updates`` are mutated in-place; the function returns the
    number of servers newly configured or updated in this group.
    """
    # Lazy import: only available after MCPIntegrator finishes loading.
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    group_dep_names = group.names
    group_dep_map = group.dep_map
    group_deps = group.deps

    configured_count = 0

    # Early validation: check all servers exist in registry (fail-fast).
    # F4 (#1116): emit a single batch heartbeat so users see the
    # registry round-trip in progress instead of silent stall.
    logger.mcp_lookup_heartbeat(len(group_dep_names))
    if verbose:
        logger.verbose_detail(f"Validating {len(group_deps)} registry servers...")
    valid_servers, invalid_servers = operations.validate_servers_exist(group_dep_names)

    if invalid_servers:
        logger.error(f"Server(s) not found in registry: {', '.join(invalid_servers)}")
        logger.progress("Run 'apm mcp search <query>' to find available servers")
        raise RuntimeError(f"Cannot install {len(invalid_servers)} missing server(s)")

    if valid_servers:
        servers_to_install = operations.check_servers_needing_installation(
            target_runtimes,
            valid_servers,
            project_root=project_root,
            user_scope=user_scope,
        )
        already_configured_candidates = [
            dep for dep in valid_servers if dep not in servers_to_install
        ]

        # Detect config drift for "already configured" servers
        if stored_mcp_configs and already_configured_candidates:
            drifted_reg_deps = [
                group_dep_map[n] for n in already_configured_candidates if n in group_dep_map
            ]
            drifted = MCPIntegrator._detect_mcp_config_drift(
                drifted_reg_deps,
                stored_mcp_configs,
            )
            if drifted:
                servers_to_update.update(drifted)
                MCPIntegrator._append_drifted_to_install_list(servers_to_install, drifted)
        already_configured_servers = [
            dep for dep in already_configured_candidates if dep not in servers_to_update
        ]

        if not servers_to_install:
            if console:
                for dep in already_configured_servers:
                    console.print(
                        f"|  [green]{STATUS_SYMBOLS['check']}[/green] {dep} "
                        f"[dim](already configured)[/dim]"
                    )
            else:
                logger.success("All registry MCP servers already configured")
        else:
            if already_configured_servers:
                if console:
                    for dep in already_configured_servers:
                        console.print(
                            f"|  [green]{STATUS_SYMBOLS['check']}[/green] {dep} "
                            f"[dim](already configured)[/dim]"
                        )
                else:
                    logger.verbose_detail(
                        "Already configured registry MCP servers: "
                        f"{', '.join(already_configured_servers)}"
                    )

            # Batch fetch server info once
            if verbose:
                logger.verbose_detail(f"Installing {len(servers_to_install)} servers...")
            server_info_cache = operations.batch_fetch_server_info(servers_to_install)

            # Apply overlays
            for server_name in servers_to_install:
                dep = group_dep_map.get(server_name)
                if dep:
                    MCPIntegrator._apply_overlay(server_info_cache, dep)

            # Collect env and runtime variables
            shared_env_vars = operations.collect_environment_variables(
                servers_to_install, server_info_cache
            )
            for server_name in servers_to_install:
                dep = group_dep_map.get(server_name)
                if dep and dep.env:
                    shared_env_vars.update(dep.env)
            shared_runtime_vars = operations.collect_runtime_variables(
                servers_to_install, server_info_cache
            )

            # Install for each target runtime
            for dep in servers_to_install:
                is_update = dep in servers_to_update
                action_text = "Updating" if is_update else "Configuring"
                if console:
                    console.print(f"|  [cyan]{STATUS_SYMBOLS['running']}[/cyan]  {dep}")
                    console.print(
                        f"|     +- {action_text} for "
                        f"{', '.join([rt.title() for rt in target_runtimes])}..."
                    )
                else:
                    logger.progress(
                        f"{dep}: {action_text.lower()} for {', '.join(target_runtimes)}..."
                    )

                any_ok = False
                for rt in target_runtimes:
                    if verbose:
                        logger.verbose_detail(f"Configuring {rt}...")
                    if MCPIntegrator._install_for_runtime(
                        rt,
                        [dep],
                        shared_env_vars,
                        server_info_cache,
                        shared_runtime_vars,
                        project_root=project_root,
                        user_scope=user_scope,
                        logger=logger,
                    ):
                        any_ok = True
                        _record_managed_server(managed_target_servers, rt, dep)

                if any_ok:
                    if console:
                        label = "updated" if is_update else "configured"
                        console.print(
                            f"|  [green]{STATUS_SYMBOLS['check']}[/green]  {dep} -> "
                            f"{', '.join([rt.title() for rt in target_runtimes])}"
                            f" [dim]({label})[/dim]"
                        )
                    configured_count += 1
                    if is_update:
                        successful_updates.add(dep)
                elif console:
                    console.print(
                        f"|  [red]{STATUS_SYMBOLS['cross']}[/red]  {dep}  "
                        "-- failed for all runtimes"
                    )
                else:
                    logger.error(f"{dep} -- failed for all runtimes")

    return configured_count
