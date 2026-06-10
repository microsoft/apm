"""Hermes agent MCP client adapter.

Hermes (Nous Research) reads MCP servers from a YAML ``mcp_servers:`` block
in ``~/.hermes/config.yaml`` (snake_case key -- distinct from the JSON
``mcpServers`` schema used by Claude/Copilot).  ``$HERMES_HOME`` overrides
the home directory (default ``~/.hermes``).

Scope: Hermes has a single home-directory config, so MCP writes are always
user-scope -- ``config.yaml`` is the same file regardless of whether the
install was triggered at project or user scope.  Unrelated top-level config
keys (model provider, telegram settings, ...) are preserved on every write.

Per-server shape:
  * stdio  -> ``command`` / ``args`` / ``env`` (+ ``enabled``)
  * http   -> ``url`` / ``headers`` (+ ``enabled``)

All YAML I/O goes through ``utils.yaml_io`` (lint forbids raw ``yaml.dump``).
"""

from __future__ import annotations

from pathlib import Path

from ...utils.console import _rich_error, _rich_success, _rich_warning
from ...utils.yaml_io import dump_yaml, load_yaml
from .copilot import CopilotClientAdapter


class HermesClientAdapter(CopilotClientAdapter):
    """MCP configuration for the Hermes agent (YAML ``mcp_servers`` schema).

    Registry formatting reuses :class:`CopilotClientAdapter`, then entries are
    converted to Hermes' on-disk shape via :meth:`_to_hermes_format`.
    """

    supports_user_scope: bool = True
    target_name: str = "hermes"
    mcp_servers_key: str = "mcp_servers"

    # Hermes' config.yaml does NOT support runtime env-var substitution; the
    # value in ``env`` must be a literal string, so install-time resolution
    # is kept (mirrors Claude -- see #1152 supply-chain analysis).
    _supports_runtime_env_substitution: bool = False

    def _config_path(self) -> Path:
        """Resolve ``<hermes-home>/config.yaml`` honouring ``$HERMES_HOME``."""
        from ...integration.targets import resolve_hermes_root

        return resolve_hermes_root() / "config.yaml"

    @staticmethod
    def _to_hermes_format(copilot_entry: dict, *, enabled: bool = True) -> dict:
        """Convert a Copilot-format server entry to Hermes' on-disk shape.

        Drops Copilot-CLI-only fields (``type: "local"``, default
        ``tools: ["*"]``, empty ``id``) and stamps an explicit ``enabled``.
        """
        if not isinstance(copilot_entry, dict):
            return copilot_entry

        url = copilot_entry.get("url")
        t = copilot_entry.get("type")
        is_remote = bool(url) or t in ("http", "sse", "streamable-http")

        out: dict = {}
        if is_remote:
            out["url"] = url
            headers = copilot_entry.get("headers")
            if headers:
                out["headers"] = headers
        else:
            out["command"] = copilot_entry.get("command")
            args = copilot_entry.get("args")
            if args:
                out["args"] = list(args)
            env = copilot_entry.get("env")
            if env:
                out["env"] = dict(env)
        out["enabled"] = enabled
        return out

    def get_config_path(self):
        """Path to the Hermes config file (``<hermes-home>/config.yaml``)."""
        return str(self._config_path())

    def _load_document(self) -> dict:
        """Load the full ``config.yaml`` document (preserving siblings)."""
        path = self._config_path()
        if not path.is_file():
            return {}
        try:
            data = load_yaml(path)
        except (OSError, ValueError):
            _rich_warning(f"Existing {path} is not valid YAML; rewriting from scratch")
            return {}
        return data if isinstance(data, dict) else {}

    def get_current_config(self):
        """Return ``{"mcp_servers": {...}}`` for the on-disk config."""
        data = self._load_document()
        servers = data.get(self.mcp_servers_key)
        return {self.mcp_servers_key: dict(servers) if isinstance(servers, dict) else {}}

    def update_config(self, config_updates, enabled=True):
        """Merge *config_updates* into the ``mcp_servers:`` block.

        Entries are normalized to Hermes' shape.  Per-server entries are
        replaced on key conflict; unrelated servers and all other top-level
        config keys are preserved.
        """
        path = self._config_path()
        try:
            data = self._load_document()
            servers = data.get(self.mcp_servers_key)
            if not isinstance(servers, dict):
                servers = {}
            for name, cfg in config_updates.items():
                servers[name] = self._to_hermes_format(cfg, enabled=enabled)
            data[self.mcp_servers_key] = servers
            path.parent.mkdir(parents=True, exist_ok=True)
            dump_yaml(data, path)
            return True
        except OSError:
            return False

    def configure_mcp_server(
        self,
        server_url,
        server_name=None,
        enabled=True,
        env_overrides=None,
        server_info_cache=None,
        runtime_vars=None,
    ):
        if not server_url:
            _rich_error("server_url cannot be empty")
            return False

        try:
            server_info = self._fetch_server_info(server_url, server_info_cache)
            if server_info is None:
                return False

            config_key = self._determine_config_key(server_url, server_name)
            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)
            ok = self.update_config({config_key: server_config}, enabled=enabled)
            if not ok:
                _rich_error(f"Failed to write MCP config for '{config_key}' to Hermes")
                return False

            _rich_success(f"Successfully configured MCP server '{config_key}' for Hermes")
            return True
        except Exception:
            # Do not interpolate the exception message: registry URLs and
            # other inputs may carry embedded credentials.
            _rich_error("Error configuring MCP server")
            return False
