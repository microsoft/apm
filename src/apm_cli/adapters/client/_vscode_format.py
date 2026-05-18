"""Private formatting helpers for the VS Code MCP client adapter.

These helpers are pure functions — they carry no adapter state — and
live in a separate module to keep ``vscode.py`` within the project's
500-line file-size limit.  They are re-exposed on
``VSCodeClientAdapter`` as ``staticmethod`` class attributes so that
existing call sites (``self.method(...)`` and
``VSCodeClientAdapter.method(...)``) continue to work unchanged.
"""

from __future__ import annotations

import re

from .base import _ENV_VAR_RE

# Legacy ``<VAR>`` placeholder (Copilot CLI / Codex only). VS Code does not
# resolve angle-bracket placeholders, so emitting them produces literal
# ``<VAR>`` text in headers / env values -- silently breaking auth at runtime.
_LEGACY_ANGLE_VAR_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>")


def _translate_env_vars_for_vscode(mapping):
    """Normalize ``${VAR}`` and ``${env:VAR}`` references to ``${env:VAR}``.

    VS Code's mcp.json natively resolves ``${env:VAR}`` from the host
    environment at server-start time. Bare ``${VAR}`` is *not* part of the
    mcp.json grammar, so VS Code would otherwise pass the literal text
    through (silently breaking auth headers, env vars, etc.).

    This translation is purely textual and idempotent:
    - ``${VAR}``      -> ``${env:VAR}``
    - ``${env:VAR}``  -> ``${env:VAR}`` (no change)
    - ``${input:X}``  -> ``${input:X}`` (no change; handled separately)
    - non-string values pass through

    A new dict is returned so callers may continue to use the original
    for input-variable extraction without ordering concerns.
    """
    if not mapping:
        return mapping
    return {
        k: (_ENV_VAR_RE.sub(r"${env:\1}", v) if isinstance(v, str) else v)
        for k, v in mapping.items()
    }


def _parse_pkg_args(pkg_args: list) -> list[str]:
    """Extract positional values from a ``package_arguments`` list."""
    args = []
    for arg in pkg_args:
        if isinstance(arg, dict):
            value = arg.get("value", "")
            if value:
                args.append(value)
    return args


def _parse_rt_args(rt_args: list) -> list[str]:
    """Extract required positional hints from a ``runtime_arguments`` list."""
    args = []
    for arg in rt_args:
        if isinstance(arg, dict):
            if arg.get("is_required", False) and arg.get("value_hint"):
                args.append(arg["value_hint"])
    return args


def _extract_package_args(package):
    """Extract positional arguments from a package entry.

    The MCP registry API uses ``package_arguments`` (with ``type``/``value``
    pairs).  Older or synthetic entries may use ``runtime_arguments``
    (with ``is_required``/``value_hint``).  This method normalises both
    formats into a flat list of argument strings.

    Args:
        package (dict): A single package entry.

    Returns:
        list[str]: Ordered argument strings, may be empty.
    """
    if not package:
        return []

    # Prefer package_arguments (current API format)
    pkg_args = package.get("package_arguments") or []
    if pkg_args:
        args = _parse_pkg_args(pkg_args)
        if args:
            return args

    # Fall back to runtime_arguments (legacy / synthetic format)
    rt_args = package.get("runtime_arguments") or []
    if rt_args:
        args = _parse_rt_args(rt_args)
        if args:
            return args

    return []


def _select_remote_with_url(remotes):
    """Return the first remote entry that has a non-empty URL.

    Returns:
        dict or None: The first usable remote, or None if none found.
    """
    for remote in remotes:
        url = (remote.get("url") or "").strip()
        if url:
            return remote
    return None


def _build_python_command_args(package, runtime_hint, pkg_args):
    """Build command and args for Python packages."""
    if runtime_hint == "uvx":
        command = "uvx"
    elif "python" in runtime_hint:
        command = "python3" if runtime_hint in ["python", "pip"] else runtime_hint
    else:
        command = "uvx"

    if pkg_args:
        args = pkg_args
    elif runtime_hint == "uvx" or command == "uvx":
        args = [package.get("name", "")]
    else:
        module_name = package.get("name", "").replace("mcp-server-", "").replace("-", "_")
        args = ["-m", f"mcp_server_{module_name}"]

    return command, args


def _build_package_input_vars(package, server_config):
    """Build input variables for package environment variables."""
    input_vars = []
    env_vars = package.get("environment_variables") or package.get("environmentVariables") or []
    if env_vars:
        server_config["env"] = {}
        for env_var in env_vars:
            if "name" in env_var:
                input_var_name = env_var["name"].lower().replace("_", "-")
                server_config["env"][env_var["name"]] = f"${{input:{input_var_name}}}"
                input_var_def = {
                    "type": "promptString",
                    "id": input_var_name,
                    "description": env_var.get("description", f"{env_var['name']} for MCP server"),
                    "password": True,
                }
                input_vars.append(input_var_def)
    return input_vars
