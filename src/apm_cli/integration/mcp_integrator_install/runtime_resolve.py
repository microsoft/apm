"""Runtime resolution for MCP install."""

from __future__ import annotations

from pathlib import Path

from apm_cli.core.scope import InstallScope
from apm_cli.runtime.utils import find_runtime_binary
from apm_cli.utils.console import STATUS_SYMBOLS

from .opts import RuntimeDetectionOpts, _ResolveRuntimesOpts


def _load_apm_config_if_needed(apm_config: dict | None, project_root_path: Path) -> dict | None:
    """Load apm.yml only when the caller did not provide parsed config."""
    if apm_config is not None:
        return apm_config
    try:
        apm_yml = project_root_path / "apm.yml"
        if apm_yml.exists():
            from apm_cli.utils.yaml_io import load_yaml

            return load_yaml(apm_yml)
    except Exception:
        return None
    return None


def _runtime_project_gate(runtime_name: str, project_root_path: Path, is_vscode_available) -> bool:
    """Return True when a runtime is present enough for auto-targeting."""
    marker_dirs = {
        "cursor": ".cursor",
        "opencode": ".opencode",
        "gemini": ".gemini",
        "windsurf": ".windsurf",
    }
    if runtime_name == "vscode":
        return is_vscode_available(project_root=project_root_path)
    if runtime_name == "claude":
        return (project_root_path / ".claude").is_dir() or find_runtime_binary("claude") is not None
    if runtime_name in marker_dirs:
        return (project_root_path / marker_dirs[runtime_name]).is_dir()
    return True


def _detect_installed_runtimes(project_root_path: Path, is_vscode_available) -> list[str]:
    """Detect runtimes available for MCP configuration."""
    runtime_names = [
        "copilot",
        "codex",
        "vscode",
        "cursor",
        "opencode",
        "gemini",
        "windsurf",
        "claude",
    ]
    try:
        from apm_cli.factory import ClientFactory
        from apm_cli.runtime.manager import RuntimeManager

        manager = RuntimeManager()
        installed_runtimes = []
        for runtime_name in runtime_names:
            try:
                if not _runtime_project_gate(runtime_name, project_root_path, is_vscode_available):
                    continue
                if runtime_name in {
                    "vscode",
                    "cursor",
                    "opencode",
                    "gemini",
                    "windsurf",
                    "claude",
                } or manager.is_runtime_available(runtime_name):
                    ClientFactory.create_client(runtime_name)
                    installed_runtimes.append(runtime_name)
            except (ValueError, ImportError):
                continue
        return installed_runtimes
    except ImportError:
        installed = [rt for rt in ["copilot", "codex"] if find_runtime_binary(rt) is not None]
        installed.extend(
            rt
            for rt in runtime_names[2:]
            if _runtime_project_gate(rt, project_root_path, is_vscode_available)
        )
        return installed


def _log_runtime_detection(opts: RuntimeDetectionOpts) -> None:
    """Log runtime detection details when verbose output is enabled."""
    if not opts.verbose:
        return
    if opts.console:
        opts.console.print(f"|  [cyan]{STATUS_SYMBOLS['info']}  Runtime Detection[/cyan]")
        opts.console.print(f"|     +- Installed: {', '.join(opts.installed)}")
        opts.console.print(f"|     +- Used in scripts: {', '.join(opts.scripts)}")
        if opts.targets:
            opts.console.print(
                f"|     +- Target: {', '.join(opts.targets)} (available + used in scripts)"
            )
        opts.console.print("|")
        return
    opts.logger.verbose_detail(f"Installed runtimes: {', '.join(opts.installed)}")
    opts.logger.verbose_detail(f"Script runtimes: {', '.join(opts.scripts)}")
    if opts.targets:
        opts.logger.verbose_detail(f"Target runtimes: {', '.join(opts.targets)}")


def _filter_user_scope_runtimes(target_runtimes: list[str], logger) -> list[str]:
    """Keep only runtimes that support user-scope MCP installation."""
    from apm_cli.factory import ClientFactory

    filtered_runtimes = []
    for rt in target_runtimes:
        try:
            client = ClientFactory.create_client(rt)
        except ValueError:
            continue
        if client.supports_user_scope:
            filtered_runtimes.append(rt)
    skipped = set(target_runtimes) - set(filtered_runtimes)
    if skipped:
        logger.warning(
            "Skipped workspace-only runtimes at user scope: "
            f"{', '.join(sorted(skipped))} -- omit --global to install these"
        )
    if not filtered_runtimes:
        logger.warning(
            "No runtimes support user-scope MCP installation (supported: copilot, codex, gemini)"
        )
    return filtered_runtimes


def _auto_detect_runtimes(ctx: _ResolveRuntimesOpts) -> tuple[list[str], dict | None]:
    """Resolve runtimes when the caller did not request a specific one."""
    project_root_path = Path(ctx.project_root) if ctx.project_root is not None else Path.cwd()
    apm_config = ctx.apm_config
    if ctx.project_root is not None:
        apm_config = _load_apm_config_if_needed(apm_config, project_root_path)
    installed_runtimes = _detect_installed_runtimes(project_root_path, ctx.is_vscode_available)
    script_runtimes = ctx.mcp_integrator_cls._detect_runtimes(
        apm_config.get("scripts", {}) if apm_config else {}
    )
    if script_runtimes:
        target_runtimes = [rt for rt in installed_runtimes if rt in script_runtimes]
        _log_runtime_detection(
            RuntimeDetectionOpts(
                verbose=ctx.verbose,
                console=ctx.console,
                logger=ctx.logger,
                installed=installed_runtimes,
                scripts=script_runtimes,
                targets=target_runtimes,
            )
        )
        if not target_runtimes:
            ctx.logger.warning("Scripts reference runtimes that are not installed")
            ctx.logger.progress("Install missing runtimes with: apm runtime setup <runtime>")
    else:
        target_runtimes = installed_runtimes
        if target_runtimes and ctx.verbose:
            ctx.logger.verbose_detail(
                f"No scripts detected, using all installed runtimes: {', '.join(target_runtimes)}"
            )
        elif not target_runtimes:
            ctx.logger.warning("No MCP-compatible runtimes installed")
            ctx.logger.progress("Install a runtime with: apm runtime setup copilot")
    if ctx.exclude:
        target_runtimes = [runtime for runtime in target_runtimes if runtime != ctx.exclude]
    if not target_runtimes and installed_runtimes:
        ctx.logger.warning(
            f"All installed runtimes excluded (--exclude {ctx.exclude}), skipping MCP configuration"
        )
        return [], apm_config
    if not target_runtimes and not installed_runtimes:
        ctx.logger.progress("No runtimes installed, using VS Code as fallback")
        return ["vscode"], apm_config
    return target_runtimes, apm_config


def _resolve_runtimes(ctx: _ResolveRuntimesOpts) -> tuple[list[str], dict | None]:
    """Resolve target runtimes and return them with the effective config."""
    if ctx.runtime:
        ctx.logger.progress(f"Targeting specific runtime: {ctx.runtime}")
        target_runtimes = [ctx.runtime]
        apm_config = ctx.apm_config
    else:
        target_runtimes, apm_config = _auto_detect_runtimes(ctx)
        if not target_runtimes and apm_config is not None:
            return [], apm_config

    gate_explicit_target = ctx.explicit_target
    if gate_explicit_target is None and ctx.runtime == "vscode":
        gate_explicit_target = ctx.runtime
    target_runtimes = ctx.mcp_integrator_cls._gate_project_scoped_runtimes(
        target_runtimes,
        user_scope=ctx.user_scope,
        project_root=ctx.project_root,
        apm_config=apm_config,
        explicit_target=gate_explicit_target,
    )
    if ctx.scope is InstallScope.USER and target_runtimes:
        target_runtimes = _filter_user_scope_runtimes(target_runtimes, ctx.logger)
    return target_runtimes, apm_config
