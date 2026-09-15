"""IBM Bob MCP client adapter.

Bob reads project MCP configuration from ``.bob/mcp.json`` and global
configuration from ``~/.bob/mcp.json``.  Both files use a top-level
``mcpServers`` object.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from ...utils.atomic_io import atomic_write_text
from ...utils.console import _rich_error, _rich_success
from .copilot import CopilotClientAdapter

logger = logging.getLogger(__name__)


class BobClientAdapter(CopilotClientAdapter):
    """MCP configuration adapter for IBM Bob."""

    supports_user_scope: bool = True
    _client_label: str = "IBM Bob"
    target_name: str = "bob"
    mcp_servers_key: str = "mcpServers"

    # Bob's documented schema does not promise runtime environment-variable
    # interpolation. Resolve placeholders before writing until that contract
    # is documented by IBM.
    _supports_runtime_env_substitution: bool = False

    def get_config_path(self) -> str:
        """Return Bob's project- or user-scope MCP config path."""
        root = Path.home() if self.user_scope else self.project_root
        return str(root / ".bob" / "mcp.json")

    def get_current_config(self) -> dict[str, Any]:
        """Read the current Bob MCP config, tolerating missing/bad JSON."""
        config_path = Path(self.get_config_path())
        if not config_path.exists():
            return {}
        try:
            with open(config_path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read %s: %s", config_path, exc)
            return {}

    def update_config(self, config_updates: dict[str, dict[str, Any]]) -> bool:
        """Merge server entries into Bob's ``mcpServers`` object."""
        config_path = Path(self.get_config_path())
        current_config = self.get_current_config()
        if not isinstance(current_config.get(self.mcp_servers_key), dict):
            current_config[self.mcp_servers_key] = {}
        current_config[self.mcp_servers_key].update(config_updates)

        config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            config_path,
            json.dumps(current_config, indent=2) + "\n",
            new_file_mode=0o600,
        )
        os.chmod(config_path, 0o600)
        return True

    @staticmethod
    def _header_mapping(remote: dict[str, Any]) -> dict[str, str]:
        """Normalize registry header representations to a string mapping."""
        headers = remote.get("headers", {})
        if isinstance(headers, list):
            return {
                str(header["name"]): str(header["value"])
                for header in headers
                if isinstance(header, dict) and "name" in header and "value" in header
            }
        if isinstance(headers, dict):
            return {str(name): str(value) for name, value in headers.items()}
        return {}

    @staticmethod
    def _copy_bob_extensions(config: dict[str, Any], server_info: dict[str, Any]) -> None:
        """Carry fields that are part of Bob's documented server schema."""
        for key in ("alwaysAllow", "disabled"):
            if key in server_info and server_info[key] is not None:
                config[key] = server_info[key]

    def _format_server_config(
        self,
        server_info: dict[str, Any],
        env_overrides: dict[str, str] | None = None,
        runtime_vars: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Format registry or self-defined server data for IBM Bob."""
        if runtime_vars is None:
            runtime_vars = {}

        raw = server_info.get("_raw_stdio")
        if raw:
            config: dict[str, Any] = {"command": raw["command"]}
            if raw.get("cwd") is not None:
                config["cwd"] = raw["cwd"]
            resolved_env: dict[str, Any] = {}
            if raw.get("env"):
                resolved_env = self._resolve_environment_variables(
                    raw["env"], env_overrides=env_overrides
                )
                config["env"] = resolved_env
                self._warn_input_variables(raw["env"], server_info.get("name", ""), "IBM Bob")
            config["args"] = [
                self._resolve_variable_placeholders(arg, resolved_env, runtime_vars)
                if isinstance(arg, str)
                else arg
                for arg in raw.get("args") or []
            ]
            self._copy_bob_extensions(config, server_info)
            self._merge_extra(config, server_info)
            return config

        remotes = server_info.get("remotes", [])
        if remotes:
            remote = self._select_remote_with_url(remotes) or remotes[0]
            transport = (remote.get("transport_type") or "http").strip()
            if transport not in ("sse", "http", "streamable-http"):
                raise ValueError(
                    f"Unsupported remote transport '{transport}' for IBM Bob. "
                    f"Server: {server_info.get('name', 'unknown')}. "
                    "Supported transports: http, sse, streamable-http."
                )

            config = {"url": (remote.get("url") or "").strip()}
            if transport in ("http", "streamable-http"):
                config["type"] = "streamable-http"
            headers = {
                name: self._resolve_env_variable(name, value, env_overrides)
                for name, value in self._header_mapping(remote).items()
                if name
            }
            if headers:
                config["headers"] = headers
                self._warn_input_variables(headers, server_info.get("name", ""), "IBM Bob")
            self._copy_bob_extensions(config, server_info)
            self._merge_extra(config, server_info)
            return config

        packages = server_info.get("packages", [])
        if not packages:
            raise ValueError(
                "MCP server has incomplete configuration in registry - "
                "no package information or remote endpoints available. "
                f"Server: {server_info.get('name', 'unknown')}"
            )

        config = {}
        package = self._select_and_dispatch_best_package(
            config, packages, env_overrides, runtime_vars
        )
        if not package:
            raise ValueError(
                f"No supported package type found for IBM Bob. "
                f"Server: {server_info.get('name', 'unknown')}."
            )
        self._copy_bob_extensions(config, server_info)
        self._merge_extra(config, server_info)
        return config

    def configure_mcp_server(
        self,
        server_url: str,
        server_name: str | None = None,
        enabled: bool = True,
        env_overrides: dict[str, str] | None = None,
        server_info_cache: dict[str, Any] | None = None,
        runtime_vars: dict[str, str] | None = None,
    ) -> bool:
        """Configure one MCP server in Bob's native JSON file."""
        if not server_url:
            _rich_error("server_url cannot be empty", symbol="error")
            return False

        config_key = self._determine_config_key(server_url, server_name)
        try:
            server_info = self._fetch_server_info(server_url, server_info_cache)
            if server_info is None:
                return False
            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)
            if not enabled:
                server_config["disabled"] = True
            self.update_config({config_key: server_config})
            _rich_success(
                f"Configured MCP server '{config_key}' for IBM Bob",
                symbol="success",
            )
            return True
        except Exception as exc:
            logger.debug("IBM Bob MCP configuration failed: %s", exc)
            _rich_error(
                f"Failed to configure MCP server '{config_key}' for IBM Bob",
                symbol="error",
            )
            return False
