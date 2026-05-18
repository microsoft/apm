"""Registry environment-variable helpers extracted from operations.py."""

from __future__ import annotations

import os

from ..core.token_manager import GitHubTokenManager


def _collect_runtime_vars_from_servers(
    server_references: list[str],
    server_info_cache: dict[str, dict | None],
    all_required_vars: dict[str, dict],
) -> None:
    """Collect runtime variables from all server references."""
    for server_ref in server_references:
        try:
            server_info = server_info_cache.get(server_ref)
            if not server_info:
                continue

            packages = server_info.get("packages", [])
            for package in packages:
                if isinstance(package, dict):
                    _extract_runtime_args_variables(package, all_required_vars)

        except Exception:
            continue


def _extract_runtime_args_variables(package: dict, all_required_vars: dict[str, dict]) -> None:
    """Extract runtime variables from a package's runtime_arguments."""
    runtime_arguments = package.get("runtime_arguments", [])
    for arg in runtime_arguments:
        if isinstance(arg, dict) and "variables" in arg:
            variables = arg.get("variables", {})
            for var_name, var_info in variables.items():
                if isinstance(var_info, dict):
                    all_required_vars[var_name] = {
                        "description": var_info.get("description", ""),
                        "required": var_info.get("is_required", True),
                    }


def _collect_env_vars_from_servers(
    server_references: list[str],
    server_info_cache: dict[str, dict | None],
    all_required_vars: dict[str, dict],
) -> None:
    """Collect environment variables from all server references."""
    for server_ref in server_references:
        try:
            server_info = server_info_cache.get(server_ref)
            if not server_info:
                continue

            _extract_docker_env_vars(server_info, server_ref, all_required_vars)
            _extract_package_env_vars(server_info, all_required_vars)

        except Exception:
            continue


def _extract_docker_env_vars(
    server_info: dict,
    server_ref: str,
    all_required_vars: dict[str, dict],
) -> None:
    """Extract environment variables from Docker args."""
    if "docker" in server_info and "args" in server_info["docker"]:
        docker_args = server_info["docker"]["args"]
        if isinstance(docker_args, list):
            for arg in docker_args:
                if isinstance(arg, str) and arg.startswith("${") and arg.endswith("}"):
                    var_name = arg[2:-1]
                    if var_name not in all_required_vars:
                        all_required_vars[var_name] = {
                            "description": f"Environment variable for {server_info.get('name', server_ref)}",
                            "required": True,
                        }


def _extract_package_env_vars(server_info: dict, all_required_vars: dict[str, dict]) -> None:
    """Extract environment variables from package definitions."""
    packages = server_info.get("packages", [])
    for package in packages:
        if isinstance(package, dict):
            env_vars = package.get("environmentVariables", []) or package.get(
                "environment_variables", []
            )
            for env_var in env_vars:
                if isinstance(env_var, dict) and "name" in env_var:
                    var_name = env_var["name"]
                    all_required_vars[var_name] = {
                        "description": env_var.get("description", ""),
                        "required": env_var.get("required", True),
                    }


def _do_prompt_for_environment_variables(required_vars: dict[str, dict]) -> dict[str, str]:
    """Prompt user for environment variables (non-interactive or interactive)."""
    is_e2e_tests = os.getenv("APM_E2E_TESTS", "").lower() in ("1", "true", "yes")
    is_ci_environment = any(
        os.getenv(var) for var in ["CI", "GITHUB_ACTIONS", "TRAVIS", "JENKINS_URL", "BUILDKITE"]
    )

    if is_e2e_tests or is_ci_environment:
        return _collect_vars_non_interactive(required_vars, is_e2e_tests)

    try:
        from rich.console import Console
        from rich.prompt import Prompt

        return _collect_vars_with_rich(required_vars, Console(), Prompt)
    except ImportError:
        import click

        return _collect_vars_with_click(required_vars, click)


def _collect_vars_non_interactive(
    required_vars: dict[str, dict], is_e2e_tests: bool
) -> dict[str, str]:
    """Collect variables in non-interactive mode (E2E tests or CI)."""
    env_vars = {}

    for var_name in sorted(required_vars.keys()):
        existing_value = os.getenv(var_name)

        if existing_value:
            env_vars[var_name] = existing_value
        elif var_name == "GITHUB_DYNAMIC_TOOLSETS":
            env_vars[var_name] = "1"
        elif "token" in var_name.lower() or "key" in var_name.lower():
            env_vars[var_name] = _get_token_for_var(var_name)
        else:
            env_vars[var_name] = ""

    print("E2E test mode detected" if is_e2e_tests else "CI environment detected")
    return env_vars


def _get_token_for_var(var_name: str) -> str:
    """Get appropriate token for a variable based on its name."""
    _tm = GitHubTokenManager()
    if "ado" in var_name.lower():
        return _tm.get_token_for_purpose("ado_modules") or ""
    if "copilot" in var_name.lower():
        return _tm.get_token_for_purpose("copilot") or ""
    return _tm.get_token_for_purpose("modules") or ""


def _collect_vars_with_rich(
    required_vars: dict[str, dict], console, prompt_class
) -> dict[str, str]:
    """Collect variables using Rich prompts."""
    env_vars = {}
    console.print("Environment variables needed:", style="cyan")

    for var_name in sorted(required_vars.keys()):
        var_info = required_vars[var_name]
        existing_value = os.getenv(var_name)

        if existing_value:
            console.print(f"  [+] {var_name}: [dim]using existing value[/dim]")
            env_vars[var_name] = existing_value
        else:
            value = _prompt_for_var_rich(var_name, var_info, prompt_class)
            env_vars[var_name] = value

    console.print()
    return env_vars


def _prompt_for_var_rich(var_name: str, var_info: dict, prompt_class) -> str:
    """Prompt for a single variable using Rich."""
    description = var_info.get("description", "")
    required = var_info.get("required", True)
    is_sensitive = any(
        keyword in var_name.lower() for keyword in ["password", "secret", "key", "token", "api"]
    )

    prompt_text = f"  {var_name}"
    if description:
        prompt_text += f" ({description})"

    if required:
        return prompt_class.ask(prompt_text, password=is_sensitive)
    return prompt_class.ask(prompt_text, default="", password=is_sensitive)


def _collect_vars_with_click(required_vars: dict[str, dict], click) -> dict[str, str]:
    """Collect variables using Click prompts (fallback)."""
    env_vars = {}
    click.echo("Environment variables needed:")

    for var_name in sorted(required_vars.keys()):
        var_info = required_vars[var_name]
        existing_value = os.getenv(var_name)

        if existing_value:
            click.echo(f"  [+] {var_name}: using existing value")
            env_vars[var_name] = existing_value
        else:
            value = _prompt_for_var_click(var_name, var_info, click)
            env_vars[var_name] = value

    click.echo()
    return env_vars


def _prompt_for_var_click(var_name: str, var_info: dict, click) -> str:
    """Prompt for a single variable using Click."""
    description = var_info.get("description", "")
    is_sensitive = any(
        keyword in var_name.lower() for keyword in ["password", "secret", "key", "token", "api"]
    )

    prompt_text = f"  {var_name}"
    if description:
        prompt_text += f" ({description})"

    return click.prompt(prompt_text, hide_input=is_sensitive, default="", show_default=False)


def _MCPServerOperations_extract_ids_from_mcp_servers(config: dict) -> set[str]:
    """Extract IDs from mcpServers config (copilot/claude)."""
    ids: set[str] = set()
    for server_config in config.get("mcpServers", {}).values():
        if isinstance(server_config, dict) and (sid := server_config.get("id")):
            ids.add(sid)
    return ids


def _MCPServerOperations_extract_ids_from_codex_config(config: dict) -> set[str]:
    """Extract IDs from Codex mcp_servers config."""
    ids: set[str] = set()
    for server_config in config.get("mcp_servers", {}).values():
        if isinstance(server_config, dict) and (sid := server_config.get("id")):
            ids.add(sid)
    return ids


def _MCPServerOperations_extract_ids_from_vscode_config(config: dict) -> set[str]:
    """Extract IDs from VS Code servers config."""
    ids: set[str] = set()
    for key in ("servers", "mcpServers"):
        for server_config in config.get(key, {}).values():
            if isinstance(server_config, dict):
                sid = (
                    server_config.get("id")
                    or server_config.get("serverId")
                    or server_config.get("server_id")
                )
                if sid:
                    ids.add(sid)
    return ids
