"""VSCode server-config helpers extracted from ``VSCodeClientAdapter``.

Extracted to keep ``adapters.client.vscode`` under 400 LOC.
All functions take ``adapter`` (a ``VSCodeClientAdapter`` instance) as
their first argument and are called only from the corresponding
delegate one-liners on the class.
"""

from __future__ import annotations

from .base import _INPUT_VAR_RE


def _format_server_config(adapter, server_info):
    """Format server details into VSCode mcp.json compatible format.

    Args:
        adapter: ``VSCodeClientAdapter`` instance providing helper methods.
        server_info (dict): Server information from registry.

    Returns:
        tuple: (server_config, input_vars) where:
            - server_config is the formatted server configuration for mcp.json
            - input_vars is a list of input variable definitions
    """
    raw = server_info.get("_raw_stdio")
    if raw:
        return _format_raw_stdio_config(adapter, server_info, raw)

    if server_info.get("packages"):
        return _format_package_config(adapter, server_info)

    return _format_remote_config(adapter, server_info)


def _format_raw_stdio_config(adapter, server_info, raw):
    """Format raw stdio configuration."""
    server_config = {
        "type": "stdio",
        "command": raw["command"],
        "args": raw["args"],
    }
    input_vars = []
    if raw.get("env"):
        adapter._warn_on_legacy_angle_vars(raw["env"], server_info.get("name", "unknown"), "env")
        env_translated = adapter._translate_env_vars_for_vscode(raw["env"])
        server_config["env"] = env_translated
        input_vars.extend(
            _extract_input_variables(adapter, env_translated, server_info.get("name", ""))
        )
    return server_config, input_vars


def _format_package_config(adapter, server_info):
    """Format package-based server configuration."""
    package = _select_best_package(adapter, server_info["packages"])
    if package is None:
        return _handle_incomplete_config(adapter, server_info)

    runtime_hint = package.get("runtime_hint", "")
    registry_name = adapter._infer_registry_name(package)
    pkg_args = adapter._extract_package_args(package)

    server_config = _build_package_server_config(
        adapter, package, runtime_hint, registry_name, pkg_args
    )
    if not server_config:
        return _handle_incomplete_config(adapter, server_info)

    input_vars = adapter._build_package_input_vars(package, server_config)
    return server_config, input_vars


def _build_package_server_config(adapter, package, runtime_hint, registry_name, pkg_args):
    """Build server configuration for a package."""
    if runtime_hint == "npx" or registry_name == "npm":
        package_name = package.get("name")
        extra_args = [a for a in pkg_args if a != package_name] if pkg_args else []
        return {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", package_name, *extra_args],
        }

    if runtime_hint == "docker" or registry_name == "docker":
        args = pkg_args if pkg_args else ["run", "-i", "--rm", package.get("name")]
        return {"type": "stdio", "command": "docker", "args": args}

    if (
        runtime_hint in ["uvx", "pip", "python"]
        or "python" in runtime_hint
        or registry_name == "pypi"
    ):
        command, args = adapter._build_python_command_args(package, runtime_hint, pkg_args)
        return {"type": "stdio", "command": command, "args": args}

    if package and runtime_hint:
        args = pkg_args if pkg_args else [package.get("name", "")]
        return {"type": "stdio", "command": runtime_hint, "args": args}

    return {}


def _format_remote_config(adapter, server_info):
    """Format remote server configuration."""
    if "sse_endpoint" in server_info:
        server_config = {
            "type": "sse",
            "url": server_info["sse_endpoint"],
            "headers": server_info.get("sse_headers", {}),
        }
        return server_config, []

    if server_info.get("remotes"):
        return _format_remote_endpoint_config(adapter, server_info)

    return _handle_incomplete_config(adapter, server_info)


def _format_remote_endpoint_config(adapter, server_info):
    """Format configuration for remote endpoint."""
    remote = adapter._select_remote_with_url(server_info["remotes"])
    if not remote:
        return _handle_incomplete_config(adapter, server_info)

    transport = (remote.get("transport_type") or "").strip()
    if not transport:
        transport = "http"
    elif transport not in ("sse", "http", "streamable-http"):
        raise ValueError(
            f"Unsupported remote transport '{transport}' for VS Code. "
            f"Server: {server_info.get('name', 'unknown')}. "
            f"Supported transports: http, sse, streamable-http."
        )

    headers = remote.get("headers", {})
    if isinstance(headers, list):
        headers = {h["name"]: h["value"] for h in headers if "name" in h and "value" in h}

    adapter._warn_on_legacy_angle_vars(headers, server_info.get("name", "unknown"), "headers")
    headers = adapter._translate_env_vars_for_vscode(headers)

    server_config = {
        "type": transport,
        "url": remote["url"].strip(),
        "headers": headers,
    }
    input_vars = _extract_input_variables(adapter, headers, server_info.get("name", ""))
    return server_config, input_vars


def _handle_incomplete_config(adapter, server_info):
    """Handle error case for incomplete server configuration."""
    packages = server_info.get("packages", [])
    if packages:
        inferred = [adapter._infer_registry_name(p) or p.get("name", "unknown") for p in packages]
        raise ValueError(
            f"No supported transport for VS Code runtime. "
            f"Server '{server_info.get('name', 'unknown')}' provides stdio packages "
            f"({', '.join(inferred)}) but none could be mapped to a VS Code configuration. "
            f"Supported package types: npm, pypi, docker."
        )
    raise ValueError(
        f"MCP server has incomplete configuration in registry - no package information or remote endpoints available. "
        f"Server: {server_info.get('name', 'unknown')}"
    )


def _extract_input_variables(adapter, mapping, server_name):
    """Scan dict values for ${input:...} references and return input variable definitions.

    Args:
        adapter: ``VSCodeClientAdapter`` instance (unused; included for API symmetry).
        mapping (dict): Header or env dict whose values may contain
            ``${input:<id>}`` placeholders.
        server_name (str): Server name used in the description field.

    Returns:
        list[dict]: Input variable definitions (``promptString``, ``password: true``).
            Duplicates within *mapping* are already deduplicated.
    """
    seen: set = set()
    result: list = []
    for value in (mapping or {}).values():
        if not isinstance(value, str):
            continue
        for match in _INPUT_VAR_RE.finditer(value):
            var_id = match.group(1)
            if var_id in seen:
                continue
            seen.add(var_id)
            result.append(
                {
                    "type": "promptString",
                    "id": var_id,
                    "description": f"{var_id} for MCP server {server_name}",
                    "password": True,
                }
            )
    return result


def _select_best_package(adapter, packages):
    """Select the best package for VS Code installation from available packages.

    Prioritizes packages in order: npm, pypi, docker, then others.
    Uses ``adapter._infer_registry_name`` so selection works even when the
    API returns an empty ``registry_name``.

    Args:
        adapter: ``VSCodeClientAdapter`` instance providing ``_infer_registry_name``.
        packages (list): List of package dictionaries.

    Returns:
        dict: Best package to use, or None if no suitable package found.
    """
    priority_order = ["npm", "pypi", "docker"]

    for target in priority_order:
        for package in packages:
            if adapter._infer_registry_name(package) == target:
                return package

    for package in packages:
        if package.get("runtime_hint"):
            return package

    return packages[0] if packages else None
