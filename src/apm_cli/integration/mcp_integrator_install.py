"""MCP ``install`` orchestration (strangler-fig extraction from ``MCPIntegrator``).

Keeps ``MCPIntegrator.install`` as a thin delegate so public API and test patch
paths stay stable while this module owns the full install flow.
"""

from __future__ import annotations

import builtins
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.utils.console import STATUS_SYMBOLS

if TYPE_CHECKING:
    from apm_cli.core.scope import InstallScope


def run_mcp_install(  # noqa: PLR0915
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
    """Install MCP dependencies.

    Args:
        mcp_deps: List of MCP dependency entries (registry strings or
            MCPDependency objects).
        runtime: Target specific runtime only.
        exclude: Exclude specific runtime from installation.
        verbose: Show detailed installation information.
        apm_config: The parsed apm.yml configuration dict (optional).
            When not provided, this function loads ``apm.yml`` from the project
            root if it exists.
        stored_mcp_configs: Previously stored MCP configs from lockfile
            for diff-aware installation.  When provided, servers whose
            manifest config has changed are re-applied automatically.
        project_root: Project root for repo-local runtime configs.
        user_scope: Whether runtime configuration is being resolved at user scope.
        explicit_target: Explicit target selected by CLI or manifest.
        scope: InstallScope (PROJECT or USER). When USER, only
            runtimes whose adapter declares ``supports_user_scope``
            are targeted; workspace-only runtimes are skipped.

    Returns:
        Number of MCP servers newly configured or updated.
    """
    # Local import: ``mcp_integrator`` must finish loading before this module
    # is first imported (``MCPIntegrator.install`` delegates here lazily).
    from apm_cli.integration.mcp_integrator import (
        MCPIntegrator,
        _get_console,
        _is_vscode_available,
    )

    if logger is None:
        logger = NullCommandLogger()
    if not mcp_deps:
        logger.warning("No MCP dependencies found in apm.yml")
        return 0

    from apm_cli.core.scope import InstallScope

    # The explicit scope enum takes precedence over the raw user_scope bool
    # so callers cannot accidentally mix user-scope runtime filtering with
    # project-scope config writes (or the inverse).
    if scope is InstallScope.USER:
        user_scope = True
    elif scope is InstallScope.PROJECT:
        user_scope = False

    # Split into registry-resolved and self-defined deps
    # Backward compat: plain strings are treated as registry deps
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

    console = _get_console()
    # Track servers that were re-applied due to config drift
    servers_to_update: builtins.set = builtins.set()
    # Track successful updates separately so the summary counts are accurate
    # even when some drift-detected servers fail to install.
    successful_updates: builtins.set = builtins.set()
    if stored_mcp_configs is None:
        stored_mcp_configs = {}

    # Start MCP section with clean header
    if console:
        try:
            from rich.text import Text

            header = Text()
            header.append("+- MCP Servers (", style="cyan")
            header.append(str(len(mcp_deps)), style="cyan bold")
            header.append(")", style="cyan")
            console.print(header)
        except Exception:
            logger.progress(f"Installing MCP dependencies ({len(mcp_deps)})...")
    else:
        logger.progress(f"Installing MCP dependencies ({len(mcp_deps)})...")

    # Runtime detection and multi-runtime installation
    if runtime:
        # Single runtime mode
        target_runtimes = [runtime]
        logger.progress(f"Targeting specific runtime: {runtime}")
    else:
        project_root_path = Path(project_root) if project_root is not None else Path.cwd()

        if apm_config is None:
            # Lazy load  -- only when the caller doesn't provide it
            try:
                apm_yml = project_root_path / "apm.yml"
                if apm_yml.exists():
                    from apm_cli.utils.yaml_io import load_yaml

                    apm_config = load_yaml(apm_yml)
            except Exception:
                apm_config = None

        # Step 1: Get all installed runtimes on the system
        try:
            from apm_cli.factory import ClientFactory
            from apm_cli.runtime.manager import RuntimeManager

            manager = RuntimeManager()
            installed_runtimes = []

            for runtime_name in [
                "copilot",
                "codex",
                "vscode",
                "cursor",
                "opencode",
                "gemini",
                "windsurf",
                "claude",
            ]:
                try:
                    if runtime_name == "vscode":
                        if _is_vscode_available(project_root=project_root_path):
                            ClientFactory.create_client(runtime_name)
                            installed_runtimes.append(runtime_name)
                    elif runtime_name == "cursor":
                        # Cursor is opt-in: only target when .cursor/ exists
                        if (project_root_path / ".cursor").is_dir():
                            ClientFactory.create_client(runtime_name)
                            installed_runtimes.append(runtime_name)
                    elif runtime_name == "opencode":
                        # OpenCode is opt-in: only target when .opencode/ exists
                        if (project_root_path / ".opencode").is_dir():
                            ClientFactory.create_client(runtime_name)
                            installed_runtimes.append(runtime_name)
                    elif runtime_name == "gemini":
                        # Gemini CLI is opt-in: only target when .gemini/ exists
                        if (project_root_path / ".gemini").is_dir():
                            ClientFactory.create_client(runtime_name)
                            installed_runtimes.append(runtime_name)
                    elif runtime_name == "windsurf":
                        # Windsurf is opt-in: only target when .windsurf/ exists
                        if (project_root_path / ".windsurf").is_dir():
                            ClientFactory.create_client(runtime_name)
                            installed_runtimes.append(runtime_name)
                    elif runtime_name == "claude":
                        # Claude Code is opt-in: target when .claude/ exists
                        # in the project (project-scope writes) OR when the
                        # `claude` binary is on PATH (user-scope writes).
                        # The PATH check is the gate that prevents the
                        # adapter from writing to ~/.claude.json on hosts
                        # where Claude Code was never installed.
                        if (project_root_path / ".claude").is_dir() or (
                            shutil.which("claude") is not None
                        ):
                            ClientFactory.create_client(runtime_name)
                            installed_runtimes.append(runtime_name)
                    else:  # noqa: PLR5501
                        if manager.is_runtime_available(runtime_name):
                            ClientFactory.create_client(runtime_name)
                            installed_runtimes.append(runtime_name)
                except (ValueError, ImportError):
                    continue
        except ImportError:
            installed_runtimes = [rt for rt in ["copilot", "codex"] if shutil.which(rt) is not None]
            # VS Code: check binary on PATH or .vscode/ directory presence
            if _is_vscode_available(project_root=project_root_path):
                installed_runtimes.append("vscode")
            # Cursor is directory-presence based, not binary-based
            if (project_root_path / ".cursor").is_dir():
                installed_runtimes.append("cursor")
            # OpenCode is directory-presence based
            if (project_root_path / ".opencode").is_dir():
                installed_runtimes.append("opencode")
            # Gemini CLI is directory-presence based
            if (project_root_path / ".gemini").is_dir():
                installed_runtimes.append("gemini")
            # Windsurf is directory-presence based
            if (project_root_path / ".windsurf").is_dir():
                installed_runtimes.append("windsurf")
            # Claude Code: directory-presence OR binary-on-PATH
            if (project_root_path / ".claude").is_dir() or (shutil.which("claude") is not None):
                installed_runtimes.append("claude")

        # Step 2: Get runtimes referenced in apm.yml scripts
        script_runtimes = MCPIntegrator._detect_runtimes(
            apm_config.get("scripts", {}) if apm_config else {}
        )

        # Step 3: Target runtimes BOTH installed AND referenced in scripts
        if script_runtimes:
            target_runtimes = [rt for rt in installed_runtimes if rt in script_runtimes]

            if verbose:
                if console:
                    console.print(f"|  [cyan]{STATUS_SYMBOLS['info']}  Runtime Detection[/cyan]")
                    console.print(f"|     +- Installed: {', '.join(installed_runtimes)}")
                    console.print(f"|     +- Used in scripts: {', '.join(script_runtimes)}")
                    if target_runtimes:
                        console.print(
                            f"|     +- Target: {', '.join(target_runtimes)} "
                            f"(available + used in scripts)"
                        )
                    console.print("|")
                else:
                    logger.verbose_detail(f"Installed runtimes: {', '.join(installed_runtimes)}")
                    logger.verbose_detail(f"Script runtimes: {', '.join(script_runtimes)}")
                    if target_runtimes:
                        logger.verbose_detail(f"Target runtimes: {', '.join(target_runtimes)}")

            if not target_runtimes:
                logger.warning("Scripts reference runtimes that are not installed")
                logger.progress("Install missing runtimes with: apm runtime setup <runtime>")
        else:
            target_runtimes = installed_runtimes
            if target_runtimes:
                if verbose:
                    logger.verbose_detail(
                        f"No scripts detected, using all installed runtimes: "
                        f"{', '.join(target_runtimes)}"
                    )
            else:
                logger.warning("No MCP-compatible runtimes installed")
                logger.progress("Install a runtime with: apm runtime setup copilot")

        # Apply exclusions
        if exclude:
            target_runtimes = [r for r in target_runtimes if r != exclude]

        # All runtimes excluded  -- nothing to configure
        if not target_runtimes and installed_runtimes:
            logger.warning(
                f"All installed runtimes excluded (--exclude {exclude}), skipping MCP configuration"
            )
            return 0

        # Fall back to VS Code only if no runtimes are installed at all
        if not target_runtimes and not installed_runtimes:
            target_runtimes = ["vscode"]
            logger.progress("No runtimes installed, using VS Code as fallback")

    # Codex MCP is project-scoped: only configure it when Codex is an
    # active project target (silent skip, same as Cursor/OpenCode/Gemini).
    # Claude Code is gated identically: a host-wide `claude` binary should
    # not opt every APM project into `.mcp.json` writes.
    target_runtimes = MCPIntegrator._gate_project_scoped_runtimes(
        target_runtimes,
        user_scope=user_scope,
        project_root=project_root,
        apm_config=apm_config,
        explicit_target=explicit_target,
    )

    # Explicit runtime/exclusion/gating can leave nothing to configure.
    if not target_runtimes:
        return 0

    # Scope filtering: at USER scope, keep only global-capable runtimes.
    # Applied after both explicit --runtime and auto-discovery paths.
    if scope is InstallScope.USER:
        from apm_cli.factory import ClientFactory as _CF

        pre_filter = list(target_runtimes)
        filtered_runtimes = []
        for rt in target_runtimes:
            try:
                client = _CF.create_client(rt)
            except ValueError:
                continue
            if client.supports_user_scope:
                filtered_runtimes.append(rt)
        target_runtimes = filtered_runtimes
        skipped = set(pre_filter) - set(target_runtimes)
        if skipped:
            msg = (
                f"Skipped workspace-only runtimes at user scope: "
                f"{', '.join(sorted(skipped))}"
                f" -- omit --global to install these"
            )
            logger.warning(msg)
        if not target_runtimes:
            logger.warning(
                "No runtimes support user-scope MCP installation (supported: copilot, codex, gemini)"
            )
            return 0

    # Use the new registry operations module for better server detection
    configured_count = 0

    # --- Registry-based deps ---
    if registry_dep_names:
        try:
            from apm_cli.registry.operations import MCPServerOperations

            operations = MCPServerOperations()

            # Early validation: check all servers exist in registry (fail-fast).
            # F4 (#1116): emit a single batch heartbeat so users see the
            # registry round-trip in progress instead of silent stall.
            logger.mcp_lookup_heartbeat(len(registry_dep_names))
            if verbose:
                logger.verbose_detail(f"Validating {len(registry_deps)} registry servers...")
            valid_servers, invalid_servers = operations.validate_servers_exist(registry_dep_names)

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
                        registry_dep_map[n]
                        for n in already_configured_candidates
                        if n in registry_dep_map
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
                        dep = registry_dep_map.get(server_name)
                        if dep:
                            MCPIntegrator._apply_overlay(server_info_cache, dep)

                    # Collect env and runtime variables
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

        except ImportError:
            logger.warning("Registry operations not available")
            logger.error("Cannot validate MCP servers without registry operations")
            raise RuntimeError("Registry operations module required for MCP installation")  # noqa: B904

    # --- Self-defined deps (registry: false) ---
    if self_defined_deps:
        self_defined_names = [dep.name for dep in self_defined_deps]
        self_defined_to_install = MCPIntegrator._check_self_defined_servers_needing_installation(
            self_defined_names,
            target_runtimes,
            project_root=project_root,
            user_scope=user_scope,
        )
        already_configured_candidates_sd = [
            name for name in self_defined_names if name not in self_defined_to_install
        ]

        # Detect config drift for "already configured" self-defined servers
        if stored_mcp_configs and already_configured_candidates_sd:
            drifted_sd_deps = [
                dep for dep in self_defined_deps if dep.name in already_configured_candidates_sd
            ]
            drifted_sd = MCPIntegrator._detect_mcp_config_drift(
                drifted_sd_deps,
                stored_mcp_configs,
            )
            if drifted_sd:
                servers_to_update.update(drifted_sd)
                MCPIntegrator._append_drifted_to_install_list(self_defined_to_install, drifted_sd)
        already_configured_self_defined = [
            name for name in already_configured_candidates_sd if name not in servers_to_update
        ]

        if already_configured_self_defined:
            if console:
                for name in already_configured_self_defined:
                    console.print(
                        f"|  [green]{STATUS_SYMBOLS['check']}[/green] {name} "
                        f"[dim](already configured)[/dim]"
                    )
            else:
                count = len(already_configured_self_defined)
                logger.success(f"{count} self-defined server(s) already configured")
                for name in already_configured_self_defined:
                    logger.verbose_detail(f"{name} already configured, skipping")

        for dep in self_defined_deps:
            if dep.name not in self_defined_to_install:
                continue

            is_update = dep.name in servers_to_update
            synthetic_info = MCPIntegrator._build_self_defined_info(dep)
            self_defined_cache = {dep.name: synthetic_info}
            self_defined_env = dep.env or {}

            transport_label = dep.transport or "stdio"
            action_text = "Updating" if is_update else "Configuring"
            if console:
                console.print(
                    f"|  [cyan]{STATUS_SYMBOLS['running']}[/cyan]  {dep.name} "
                    f"[dim](self-defined, {transport_label})[/dim]"
                )
                console.print(
                    f"|     +- {action_text} for "
                    f"{', '.join([rt.title() for rt in target_runtimes])}..."
                )
            else:
                logger.progress(
                    f"{dep.name}: {action_text.lower()} for {', '.join(target_runtimes)}..."
                )

            any_ok = False
            for rt in target_runtimes:
                if verbose:
                    logger.verbose_detail(f"Configuring {dep.name} for {rt}...")
                if MCPIntegrator._install_for_runtime(
                    rt,
                    [dep.name],
                    self_defined_env,
                    self_defined_cache,
                    project_root=project_root,
                    user_scope=user_scope,
                    logger=logger,
                ):
                    any_ok = True

            if any_ok:
                if console:
                    label = "updated" if is_update else "configured"
                    console.print(
                        f"|  [green]{STATUS_SYMBOLS['check']}[/green]  {dep.name} -> "
                        f"{', '.join([rt.title() for rt in target_runtimes])}"
                        f" [dim]({label})[/dim]"
                    )
                configured_count += 1
                if is_update:
                    successful_updates.add(dep.name)
            elif console:
                console.print(
                    f"|  [red]{STATUS_SYMBOLS['cross']}[/red]  {dep.name}  "
                    "-- failed for all runtimes"
                )
            else:
                logger.error(f"{dep.name} -- failed for all runtimes")

    # Close the panel
    if console:
        if configured_count > 0:
            # Use successful_updates (not servers_to_update) for accurate counts.
            # servers_to_update = all drift-detected servers (some may have failed).
            # successful_updates = servers that were re-applied AND succeeded.
            update_count = builtins.len(successful_updates)
            new_count = configured_count - update_count
            parts = []
            if new_count > 0:
                parts.append(f"configured {new_count} server{'s' if new_count != 1 else ''}")
            if update_count > 0:
                parts.append(f"updated {update_count} server{'s' if update_count != 1 else ''}")
            console.print(
                f"[green]{STATUS_SYMBOLS['success']} {', '.join(parts).capitalize()}[/green]"
            )
        else:
            console.print(f"[green]{STATUS_SYMBOLS['success']} All servers up to date[/green]")

    return configured_count
