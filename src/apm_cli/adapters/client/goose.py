"""Goose (Block) MCP client adapter.

Goose reads its MCP servers from a YAML ``extensions:`` block in
``~/.config/goose/config.yaml`` (honouring ``$XDG_CONFIG_HOME``).  Goose
calls MCP servers "extensions" and uses a schema distinct from the JSON
``mcpServers`` used by Claude/Copilot:

.. code-block:: yaml

   extensions:
     server-name:
       name: server-name
       type: stdio
       cmd: npx
       args: ["-y", "@modelcontextprotocol/server-foo"]
       envs: { KEY: value }
       enabled: true
       timeout: 300

Per-server shape:
  * stdio  -> ``type: stdio`` / ``cmd`` / ``args`` / ``envs``
  * remote -> ``type: streamable_http`` / ``uri`` / ``headers``

Scope: Goose has a single home-directory config, so MCP writes are always
user-scope -- ``config.yaml`` is the same file regardless of whether the
install was triggered at project or user scope (Goose reads only
``.goosehints`` from the project tree, never a project ``config.yaml``).

Shared YAML round-trip / atomic-write / malformed-file handling lives in
:class:`YamlMcpClientAdapter`; this adapter only declares the config path
and the Goose-specific per-server schema transform.

Ref: https://goose-docs.ai/docs/getting-started/using-extensions/
"""

from __future__ import annotations

import os
from pathlib import Path

from ._yaml_config import YamlMcpClientAdapter

# Goose's default per-extension tool-response timeout (seconds).
_DEFAULT_TIMEOUT = 300


class GooseClientAdapter(YamlMcpClientAdapter):
    """MCP configuration for the Goose agent (YAML ``extensions`` schema)."""

    target_name: str = "goose"
    _display_name: str = "Goose"
    mcp_servers_key: str = "extensions"

    def _config_path(self) -> Path:
        """Resolve ``<config-home>/goose/config.yaml`` honouring ``$XDG_CONFIG_HOME``."""
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
        return base / "goose" / "config.yaml"

    def _to_native_format(self, name: str, copilot_entry: dict, *, enabled: bool = True) -> dict:
        """Convert a Copilot-format server entry to Goose's on-disk shape.

        Drops Copilot-CLI-only fields (``type: "local"``, default
        ``tools: ["*"]``, empty ``id``), renames ``command``/``env`` to Goose's
        ``cmd``/``envs``, stamps an explicit ``name``/``enabled``/``timeout``,
        and maps remote endpoints to Goose's ``streamable_http`` transport.
        Each field is emitted only when it has the expected type (string for
        ``uri``/``cmd``, dict for ``headers``/``envs``, list for ``args``), so
        a malformed entry never serializes a bad value into Goose's config.
        A non-mapping entry fails closed (raises ``ValueError``) -- the base
        adapter turns that into a skipped write rather than a crash.
        """
        if not isinstance(copilot_entry, dict):
            raise ValueError(f"MCP server config for {name!r} is not a mapping")

        url = copilot_entry.get("url")
        t = copilot_entry.get("type")
        is_remote = bool(url) or t in ("http", "sse", "streamable-http")

        out: dict = {"name": name}
        if is_remote:
            # Copilot collapses sse/streamable-http to "http"; Goose has no
            # bare "http" transport, so the modern streamable_http is used.
            out["type"] = "streamable_http"
            if isinstance(url, str) and url:
                out["uri"] = url
            headers = copilot_entry.get("headers")
            if isinstance(headers, dict) and headers:
                out["headers"] = dict(headers)
        else:
            out["type"] = "stdio"
            command = copilot_entry.get("command")
            if isinstance(command, str) and command:
                out["cmd"] = command
            args = copilot_entry.get("args")
            if isinstance(args, list) and args:
                out["args"] = list(args)
            envs = copilot_entry.get("env")
            if isinstance(envs, dict) and envs:
                out["envs"] = dict(envs)
        out["enabled"] = enabled
        out["timeout"] = _DEFAULT_TIMEOUT
        return out
