"""Runtime resolution for MCP install."""

from __future__ import annotations

import shutil
from pathlib import Path

from apm_cli.core.scope import InstallScope
from apm_cli.utils.console import STATUS_SYMBOLS


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
        return (project_root_path / ".claude").is_dir() or shutil.which("claude") is not None
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
        installed = [rt for rt in ["copilot", "codex"] if shutil.which(rt) is not None]
        installed.extend(
            rt
            for rt in runtime_names[2:]
            if _runtime_project_gate(rt, project_root_path, is_vscode_available)
        )
        return installed


def _log_runtime_detection(
    verbose: bool, console, logger, installed: list[str], scripts: list[str], targets: list[str]
) -> None:
    """Log runtime detection details when verbose output is enabled."""
    if not verbose:
        return
    if console:
        console.print(f"|  [cyan]{STATUS_SYMBOLS['info']}  Runtime Detection[/cyan]")
        console.print(f"|     +- Installed: {', '.join(installed)}")
        console.print(f"|     +- Used in scripts: {', '.join(scripts)}")
        if targets:
            console.print(f"|     +- Target: {', '.join(targets)} (available + used in scripts)")
        console.print("|")
        return
    logger.verbose_detail(f"Installed runtimes: {', '.join(installed)}")
    logger.verbose_detail(f"Script runtimes: {', '.join(scripts)}")
    if targets:
        logger.verbose_detail(f"Target runtimes: {', '.join(targets)}")


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


def _resolve_runtimes(
    *,
    runtime: str | None,
    exclude: str | None,
    verbose: bool,
    apm_config: dict | None,
    project_root,
    user_scope: bool,
    explicit_target: str | None,
    scope: InstallScope | None,
    logger,
    console,
    mcp_integrator_cls,
    is_vscode_available,
) -> tuple[list[str], dict | None]:
    """Resolve target runtimes and return them with the effective config."""
    if runtime:
        logger.progress(f"Targeting specific runtime: {runtime}")
        target_runtimes = [runtime]
    else:
        project_root_path = Path(project_root) if project_root is not None else Path.cwd()
        apm_config = _load_apm_config_if_needed(apm_config, project_root_path)
        installed_runtimes = _detect_installed_runtimes(project_root_path, is_vscode_available)
        script_runtimes = mcp_integrator_cls._detect_runtimes(
            apm_config.get("scripts", {}) if apm_config else {}
        )
        if script_runtimes:
            target_runtimes = [rt for rt in installed_runtimes if rt in script_runtimes]
            _log_runtime_detection(
                verbose, console, logger, installed_runtimes, script_runtimes, target_runtimes
            )
            if not target_runtimes:
                logger.warning("Scripts reference runtimes that are not installed")
                logger.progress("Install missing runtimes with: apm runtime setup <runtime>")
        else:
            target_runtimes = installed_runtimes
            if target_runtimes and verbose:
                logger.verbose_detail(
                    f"No scripts detected, using all installed runtimes: {', '.join(target_runtimes)}"
                )
            elif not target_runtimes:
                logger.warning("No MCP-compatible runtimes installed")
                logger.progress("Install a runtime with: apm runtime setup copilot")
        if exclude:
            target_runtimes = [r for r in target_runtimes if r != exclude]
        if not target_runtimes and installed_runtimes:
            logger.warning(
                f"All installed runtimes excluded (--exclude {exclude}), skipping MCP configuration"
            )
            return [], apm_config
        if not target_runtimes and not installed_runtimes:
            target_runtimes = ["vscode"]
            logger.progress("No runtimes installed, using VS Code as fallback")

    target_runtimes = mcp_integrator_cls._gate_project_scoped_runtimes(
        target_runtimes,
        user_scope=user_scope,
        project_root=project_root,
        apm_config=apm_config,
        explicit_target=explicit_target,
    )
    if scope is InstallScope.USER and target_runtimes:
        target_runtimes = _filter_user_scope_runtimes(target_runtimes, logger)
    return target_runtimes, apm_config
