"""ai-assist agent MCP client adapter.

ai-assist reads MCP servers from a YAML ``servers:`` block in
``<config_dir>/mcp_servers.yaml``.  ``$AI_ASSIST_CONFIG_DIR`` overrides
the config directory (default ``~/.ai-assist``).

Scope: ai-assist has a single config directory, so MCP writes are always
user-scope.  The dedicated ``mcp_servers.yaml`` file contains only MCP
server definitions.

Per-server shape:
  * stdio  -> ``command`` / ``args`` / ``env`` (+ ``enabled``, ``readonly_tools``, ``pagination``)
  * http   -> ``url`` / ``transport`` (+ ``enabled``, ``readonly_tools``, ``pagination``)

YAML serialization goes through ``utils.yaml_io``; the document is written
atomically with ``0o600`` perms via ``utils.atomic_io`` because
``mcp_servers.yaml`` may carry literal credentials in env values.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import yaml

from ...utils.atomic_io import atomic_write_text
from ...utils.console import _rich_error, _rich_success
from ...utils.yaml_io import load_yaml, yaml_to_str
from .copilot import CopilotClientAdapter

_CONFIG_FILE_MODE = 0o600


class _MalformedAiAssistConfig(Exception):
    """Raised when ``mcp_servers.yaml`` exists but is not a YAML mapping.

    Signals write paths to refuse the overwrite so the user's existing
    configuration is never discarded.
    """


class AiAssistClientAdapter(CopilotClientAdapter):
    """MCP configuration for the ai-assist agent (YAML ``servers`` schema).

    Registry formatting reuses :class:`CopilotClientAdapter`, then entries are
    converted to ai-assist's on-disk shape via :meth:`_to_ai_assist_format`.
    """

    supports_user_scope: bool = True
    target_name: str = "ai-assist"
    mcp_servers_key: str = "servers"

    # ai-assist does os.path.expandvars on env values at runtime, so ${VAR}
    # placeholders should be preserved rather than resolved at install time.
    _supports_runtime_env_substitution: bool = True

    def _config_path(self) -> Path:
        """Resolve ``<config_dir>/mcp_servers.yaml`` honouring ``$AI_ASSIST_CONFIG_DIR``."""
        from ...integration.targets import resolve_ai_assist_root

        return resolve_ai_assist_root() / "mcp_servers.yaml"

    @staticmethod
    def _to_ai_assist_format(copilot_entry: dict, *, enabled: bool = True) -> dict:
        """Convert a Copilot-format server entry to ai-assist's on-disk shape.

        Drops Copilot-CLI-only fields (``type: "local"``, default
        ``tools: ["*"]``, empty ``id``).  Preserves ai-assist-specific
        fields (``readonly_tools``, ``pagination``, ``transport``).
        Required transport fields (``url`` for remote, ``command`` for stdio)
        are only emitted when truthy.
        """
        if not isinstance(copilot_entry, dict):
            return copilot_entry

        url = copilot_entry.get("url")
        t = copilot_entry.get("type")
        is_remote = bool(url) or t in ("http", "sse", "streamable-http")

        out: dict = {}
        if is_remote:
            if url:
                out["url"] = url
            transport = copilot_entry.get("transport")
            if not transport and t in ("sse", "streamable-http"):
                transport = "streamablehttp" if t == "streamable-http" else t
            if transport:
                out["transport"] = transport
            headers = copilot_entry.get("headers")
            if headers:
                out["headers"] = headers
        else:
            command = copilot_entry.get("command")
            if command:
                out["command"] = command
            args = copilot_entry.get("args")
            if args:
                out["args"] = list(args)
            env = copilot_entry.get("env")
            if env:
                out["env"] = dict(env)

        out["enabled"] = enabled

        readonly_tools = copilot_entry.get("readonly_tools")
        if readonly_tools:
            out["readonly_tools"] = list(readonly_tools)
        pagination = copilot_entry.get("pagination")
        if pagination:
            out["pagination"] = dict(pagination)

        return out

    def get_config_path(self):
        """Path to the ai-assist MCP config file."""
        return str(self._config_path())

    def _load_document(self) -> dict:
        """Load the full ``mcp_servers.yaml`` document.

        Returns ``{}`` when the file is absent or empty.  Raises
        :class:`_MalformedAiAssistConfig` when the file exists but is not a
        YAML mapping so write paths can refuse to overwrite.
        """
        path = self._config_path()
        if not path.is_file():
            return {}
        try:
            data = load_yaml(path)
        except (OSError, yaml.YAMLError) as exc:
            raise _MalformedAiAssistConfig(str(path)) from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise _MalformedAiAssistConfig(str(path))
        return data

    def get_current_config(self):
        """Return ``{"servers": {...}}`` for the on-disk config."""
        try:
            data = self._load_document()
        except _MalformedAiAssistConfig:
            return {self.mcp_servers_key: {}}
        servers = data.get(self.mcp_servers_key)
        return {self.mcp_servers_key: dict(servers) if isinstance(servers, dict) else {}}

    def update_config(self, config_updates, enabled=True):
        """Merge *config_updates* into the ``servers:`` block.

        Per-server entries are replaced on key conflict; unrelated keys are
        preserved.  The file is written atomically with ``0o600`` permissions.
        A malformed existing file is left untouched (returns ``False``).
        """
        path = self._config_path()
        try:
            data = self._load_document()
        except _MalformedAiAssistConfig:
            _rich_error(
                f"{path} is malformed YAML; refusing to overwrite. "
                "Fix or remove the file manually, then retry."
            )
            return False
        try:
            servers = data.get(self.mcp_servers_key)
            if not isinstance(servers, dict):
                servers = {}
            for name, cfg in config_updates.items():
                servers[name] = self._to_ai_assist_format(cfg, enabled=enabled)
            data[self.mcp_servers_key] = servers
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, yaml_to_str(data), new_file_mode=_CONFIG_FILE_MODE)
            with contextlib.suppress(OSError, NotImplementedError):
                os.chmod(path, _CONFIG_FILE_MODE)
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
                _rich_error(f"Failed to write MCP config for '{config_key}' to ai-assist")
                return False

            _rich_success(f"Successfully configured MCP server '{config_key}' for ai-assist")
            return True
        except Exception:
            _rich_error("Error configuring MCP server")
            return False
