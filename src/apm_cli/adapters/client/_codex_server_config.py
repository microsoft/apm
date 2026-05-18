"""Codex server-config helpers extracted from ``CodexClientAdapter``.

Extracted to keep ``adapters.client.codex`` under 400 LOC.
All functions take ``adapter`` (a ``CodexClientAdapter`` instance) as
their first argument and are called only from the corresponding
delegate one-liners on the class.
"""

from __future__ import annotations


def _codex_npm_config(
    package_name,
    runtime_hint,
    processed_runtime_args,
    processed_package_args,
    resolved_env,
):
    """Return npm-specific config fields for Codex TOML format."""
    updates: dict = {"command": runtime_hint or "npx"}
    all_args = processed_runtime_args + processed_package_args
    if all_args:
        has_pkg = any(a == package_name or a.startswith(f"{package_name}@") for a in all_args)
        if has_pkg:
            updates["args"] = all_args
        else:
            extra_args = [a for a in all_args if a != "-y"]
            updates["args"] = ["-y", package_name] + extra_args  # noqa: RUF005
    else:
        updates["args"] = ["-y", package_name]
    if resolved_env:
        updates["env"] = resolved_env
    return updates


def _codex_docker_config(adapter, processed_runtime_args, processed_package_args, resolved_env):
    """Return docker-specific config fields for Codex TOML format."""
    updates: dict = {
        "command": "docker",
        "args": adapter._ensure_docker_env_flags(
            processed_runtime_args + processed_package_args, resolved_env
        ),
    }
    if resolved_env:
        updates["env"] = resolved_env
    return updates


def _codex_pypi_config(
    package_name,
    runtime_hint,
    processed_runtime_args,
    processed_package_args,
    resolved_env,
):
    """Return pypi-specific config fields for Codex TOML format."""
    updates: dict = {
        "command": runtime_hint or "uvx",
        "args": [package_name] + processed_runtime_args + processed_package_args,  # noqa: RUF005
    }
    if resolved_env:
        updates["env"] = resolved_env
    return updates


def _codex_homebrew_config(
    adapter,
    package_name,
    processed_runtime_args,
    processed_package_args,
    resolved_env,
):
    """Return homebrew-specific config fields for Codex TOML format."""
    cmd = package_name.split("/")[-1] if "/" in package_name else package_name
    updates: dict = {
        "command": cmd,
        "args": processed_runtime_args + processed_package_args,
    }
    if resolved_env:
        updates["env"] = resolved_env
    return updates


def _codex_generic_config(
    package_name,
    runtime_hint,
    processed_runtime_args,
    processed_package_args,
    resolved_env,
):
    """Return generic-registry config fields for Codex TOML format."""
    updates: dict = {
        "command": runtime_hint or package_name,
        "args": processed_runtime_args + processed_package_args,
    }
    if resolved_env:
        updates["env"] = resolved_env
    return updates


def _format_server_config(adapter, server_info, env_overrides=None, runtime_vars=None):
    """Format server information into Codex CLI MCP configuration format.

    Args:
        adapter: ``CodexClientAdapter`` instance providing helper methods.
        server_info (dict): Server information from registry.
        env_overrides (dict, optional): Pre-collected environment variable overrides.
        runtime_vars (dict, optional): Runtime variable values.

    Returns:
        dict: Formatted server configuration for Codex CLI.
    """
    config = {
        "command": "unknown",
        "args": [],
        "env": {},
        "id": server_info.get("id", ""),
    }

    raw = server_info.get("_raw_stdio")
    if raw:
        config["command"] = raw["command"]
        config["args"] = [adapter.normalize_project_arg(arg) for arg in raw["args"]]
        if raw.get("env"):
            config["env"] = raw["env"]
            adapter._warn_input_variables(raw["env"], server_info.get("name", ""), "Codex CLI")
        return config

    packages = server_info.get("packages", [])

    if not packages:
        raise ValueError(
            f"MCP server has no package information available in registry. "
            f"This appears to be a temporary registry issue or the server is remote-only. "
            f"Server: {server_info.get('name', 'unknown')}"
        )

    if packages:
        package = adapter._select_best_package(packages)

        if package:
            registry_name = adapter._infer_registry_name(package)
            package_name = package.get("name", "")
            runtime_hint = package.get("runtime_hint", "")
            runtime_arguments = package.get("runtime_arguments", [])
            package_arguments = package.get("package_arguments", [])
            env_vars = package.get("environment_variables", [])

            resolved_env = adapter._process_environment_variables(env_vars, env_overrides)

            processed_runtime_args = adapter._process_arguments(
                runtime_arguments, resolved_env, runtime_vars
            )
            processed_package_args = adapter._process_arguments(
                package_arguments, resolved_env, runtime_vars
            )

            if registry_name == "npm":
                config.update(
                    _codex_npm_config(
                        package_name,
                        runtime_hint,
                        processed_runtime_args,
                        processed_package_args,
                        resolved_env,
                    )
                )
            elif registry_name == "docker":
                config.update(
                    _codex_docker_config(
                        adapter,
                        processed_runtime_args,
                        processed_package_args,
                        resolved_env,
                    )
                )
            elif registry_name == "pypi":
                config.update(
                    _codex_pypi_config(
                        package_name,
                        runtime_hint,
                        processed_runtime_args,
                        processed_package_args,
                        resolved_env,
                    )
                )
            elif registry_name == "homebrew":
                config.update(
                    _codex_homebrew_config(
                        adapter,
                        package_name,
                        processed_runtime_args,
                        processed_package_args,
                        resolved_env,
                    )
                )
            else:
                config.update(
                    _codex_generic_config(
                        package_name,
                        runtime_hint,
                        processed_runtime_args,
                        processed_package_args,
                        resolved_env,
                    )
                )

    return config
