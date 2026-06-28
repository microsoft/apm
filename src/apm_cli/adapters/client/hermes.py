"""Hermes agent MCP client adapter.

Hermes (Nous Research) reads MCP servers from a YAML ``mcp_servers:`` block
in ``~/.hermes/config.yaml`` (snake_case key -- distinct from the JSON
``mcpServers`` schema used by Claude/Copilot).  ``$HERMES_HOME`` overrides
the home directory (default ``~/.hermes``).

Scope: Hermes has a single home-directory config, so MCP writes are always
user-scope -- ``config.yaml`` is the same file regardless of whether the
install was triggered at project or user scope.  Unrelated top-level config
keys (model provider, telegram settings, ...) are preserved on every write;
a malformed existing file is left untouched rather than overwritten.

Per-server shape:
  * stdio  -> ``command`` / ``args`` / ``env`` (+ ``enabled``)
  * http   -> ``url`` / ``headers`` (+ ``enabled``)

Shared YAML round-trip / atomic-write / malformed-file handling lives in
:class:`YamlMcpClientAdapter`; this adapter only declares the config path
and the Hermes-specific per-server schema transform.
"""

from __future__ import annotations

from pathlib import Path

from ._yaml_config import YamlMcpClientAdapter


class HermesClientAdapter(YamlMcpClientAdapter):
    """MCP configuration for the Hermes agent (YAML ``mcp_servers`` schema).

    Registry formatting reuses :class:`CopilotClientAdapter`, then entries are
    converted to Hermes' on-disk shape via :meth:`_to_hermes_format`.
    """

    target_name: str = "hermes"
    _display_name: str = "Hermes"
    mcp_servers_key: str = "mcp_servers"

    def _config_path(self) -> Path:
        """Resolve ``<hermes-home>/config.yaml`` honouring ``$HERMES_HOME``."""
        from ...integration.targets import resolve_hermes_root

        return resolve_hermes_root() / "config.yaml"

    def _to_native_format(self, name: str, copilot_entry: dict, *, enabled: bool = True) -> dict:
        """Adapt the shared per-server hook to Hermes' static transform."""
        return self._to_hermes_format(copilot_entry, enabled=enabled)

    @staticmethod
    def _to_hermes_format(copilot_entry: dict, *, enabled: bool = True) -> dict:
        """Convert a Copilot-format server entry to Hermes' on-disk shape.

        Drops Copilot-CLI-only fields (``type: "local"``, default
        ``tools: ["*"]``, empty ``id``) and stamps an explicit ``enabled``.
        Required transport fields (``url`` for remote, ``command`` for stdio)
        are only emitted when truthy so a malformed entry never serializes as
        ``url: null`` / ``command: null`` into Hermes' config.
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
        return out
