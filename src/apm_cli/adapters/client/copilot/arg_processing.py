"""GitHub Copilot CLI implementation of MCP client adapter.

This adapter implements the Copilot CLI-specific handling of MCP server configuration,
targeting the global ~/.copilot/mcp-config.json file as specified in the MCP installation
architecture specification.
"""

from __future__ import annotations

import re

from ....utils.github_host import is_github_hostname
from ..base import _ENV_VAR_RE
from .class_ import _extract_legacy_angle_vars, _translate_env_placeholder

_COPILOT_ENV_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>|" + _ENV_VAR_RE.pattern)
_LEGACY_ANGLE_VAR_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>")


def _process_single_arg(self, arg, resolved_env, runtime_vars):
    """Process one argument dict or string; return a list of result strings."""
    processed = []
    if isinstance(arg, dict):
        arg_type = arg.get("type", "")
        if arg_type == "positional":
            value = arg.get("value", arg.get("default", ""))
            if value:
                processed.append(
                    self._resolve_variable_placeholders(str(value), resolved_env, runtime_vars)
                )
        elif arg_type == "named":
            name = arg.get("name", "")
            value = arg.get("value", arg.get("default", ""))
            if name:
                processed.append(name)
                if value and value != name and not value.startswith("-"):
                    processed.append(
                        self._resolve_variable_placeholders(str(value), resolved_env, runtime_vars)
                    )
    elif isinstance(arg, str):
        processed.append(self._resolve_variable_placeholders(arg, resolved_env, runtime_vars))
    return processed


def _process_arguments(self, arguments, resolved_env=None, runtime_vars=None):
    """Process argument objects to extract simple string values with environment and runtime variable resolution.

    Args:
        arguments (list): List of argument objects from registry.
        resolved_env (dict): Resolved environment variables.
        runtime_vars (dict): Resolved runtime variables.

    Returns:
        list: List of processed argument strings.
    """
    if resolved_env is None:
        resolved_env = {}
    if runtime_vars is None:
        runtime_vars = {}

    processed = []
    for arg in arguments:
        processed.extend(_process_single_arg(self, arg, resolved_env, runtime_vars))
    return processed


def _resolve_variable_placeholders(self, value, resolved_env, runtime_vars):
    """Resolve runtime template variables and translate or resolve env-var
    placeholders in argument strings.

    Behaviour depends on ``self._supports_runtime_env_substitution``:

    - True (Copilot CLI default): env-var placeholders (``<VAR>``,
      ``${VAR}``, ``${env:VAR}``) are translated to ``${VAR}`` for
      runtime substitution by Copilot CLI. APM template variables
      (``{runtime_var}``) are still resolved at install time because
      they are an APM-internal concept Copilot cannot interpret.

    - False (legacy / sibling-adapter behaviour): legacy ``<VAR>``
      placeholders are resolved against ``resolved_env`` (the dict of
      literal env-var values), and ``{runtime_var}`` against
      ``runtime_vars``. Newer ``${VAR}`` / ``${env:VAR}`` syntaxes are
      left as-is for backward compatibility.

    Args:
        value (str): Value that may contain placeholders.
        resolved_env (dict): Dictionary of resolved env vars (legacy
            mode) or placeholder strings (translate mode).
        runtime_vars (dict): Dictionary of resolved runtime variables.

    Returns:
        str: Processed value with placeholders translated or resolved.
    """
    import re

    if not value:
        return value

    processed = str(value)

    if self._supports_runtime_env_substitution:
        # Track legacy <VAR> offenders before translating them away.
        self._last_legacy_angle_vars.update(_extract_legacy_angle_vars(processed))
        # Translate all three env-var placeholder syntaxes to ${VAR}.
        processed = _translate_env_placeholder(processed)
    else:
        # Replace <TOKEN_NAME> with actual values from resolved_env (for Docker env vars)
        env_pattern = r"<([A-Z_][A-Z0-9_]*)>"

        def replace_env_var(match):
            env_name = match.group(1)
            return resolved_env.get(env_name, match.group(0))  # Return original if not found

        processed = re.sub(env_pattern, replace_env_var, processed)

    # Replace {runtime_var} with actual values from runtime_vars (for NPM args).
    # Negative lookbehind on `$` so we never re-substitute inside an already-translated
    # ${VAR} env placeholder (the brace is part of a Copilot CLI runtime substitution,
    # not an APM template variable).
    if runtime_vars:
        runtime_pattern = r"(?<!\$)\{([a-zA-Z_][a-zA-Z0-9_]*)\}"

        def replace_runtime_var(match):
            var_name = match.group(1)
            return runtime_vars.get(var_name, match.group(0))

        processed = re.sub(runtime_pattern, replace_runtime_var, processed)

    return processed


def _resolve_env_placeholders(self, value, resolved_env):
    """Legacy method for backward compatibility. Use _resolve_variable_placeholders instead."""
    return self._resolve_variable_placeholders(value, resolved_env, {})


def _select_remote_with_url(remotes):
    """Return the first remote entry that has a non-empty URL.

    Args:
        remotes (list): Candidate remote entries from the registry.

    Returns:
        dict or None: The first usable remote, or None if none qualify.
    """
    for remote in remotes:
        url = (remote.get("url") or "").strip()
        if url:
            return remote
    return None


def _select_best_package(self, packages):
    """Select the best package for installation from available packages.

    Prioritizes packages in order: npm, docker, pypi, homebrew, others.
    Uses ``_infer_registry_name`` so selection works even when the
    registry API returns empty ``registry_name``.

    Args:
        packages (list): List of package dictionaries.

    Returns:
        dict: Best package to use, or None if no suitable package found.
    """
    priority_order = ["npm", "docker", "pypi", "homebrew"]

    for target in priority_order:
        for package in packages:
            if self._infer_registry_name(package) == target:
                return package

    # If no priority package found, return the first one
    return packages[0] if packages else None


def _is_github_server(self, server_name, url):
    """Securely determine if a server is a GitHub MCP server.

    Uses proper URL parsing and hostname validation to prevent token
    injection via poisoned registry entries. Both the server name and
    the URL hostname must match the GitHub allowlists before a GitHub
    token is injected.

    Args:
        server_name (str): Name of the MCP server.
        url (str): URL of the remote endpoint.

    Returns:
        bool: True if this is a legitimate GitHub MCP server, False otherwise.
    """
    from urllib.parse import urlparse

    github_server_names = [
        "github-mcp-server",
        "github",
        "github-mcp",
        "github-copilot-mcp-server",
    ]

    def _is_github_mcp_hostname(hostname: str) -> bool:
        """Check if *hostname* belongs to GitHub (cloud, enterprise, or Copilot API)."""
        if is_github_hostname(hostname):
            return True
        h = hostname.lower()
        # Subdomains of github.com (e.g. api.github.com)
        if h.endswith(".github.com"):
            return True
        # Copilot API hosts (e.g. api.githubcopilot.com, api.business.githubcopilot.com)
        return h == "githubcopilot.com" or h.endswith(".githubcopilot.com")

    name_matches = bool(
        server_name and server_name.lower() in [n.lower() for n in github_server_names]
    )

    # Parse and validate hostname from URL
    hostname = None
    if url:
        try:
            parsed_url = urlparse(url)
            # Reject non-HTTPS URLs to prevent cleartext token leakage
            if parsed_url.scheme and parsed_url.scheme.lower() != "https":
                return False
            hostname = parsed_url.hostname
        except Exception:
            return False

    host_matches = bool(hostname and _is_github_mcp_hostname(hostname))

    return name_matches and host_matches
