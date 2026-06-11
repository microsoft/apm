"""Kiro IDE implementation of MCP client adapter.

Kiro reads MCP configuration from ``.kiro/settings/mcp.json`` at project
scope and ``~/.kiro/settings/mcp.json`` at user scope. The schema uses a
``mcpServers`` object whose server entries support stdio ``command`` /
``args`` / ``env`` and remote ``url`` / ``headers`` entries. Kiro resolves
``${VAR}`` environment placeholders at runtime, so this adapter preserves
placeholders instead of writing host secrets to disk.
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


class KiroClientAdapter(CopilotClientAdapter):
    """Kiro IDE MCP client adapter."""

    supports_user_scope: bool = True
    _client_label: str = "Kiro"
    target_name: str = "kiro"
    mcp_servers_key: str = "mcpServers"
    _supports_runtime_env_substitution: bool = True

    def _get_kiro_root(self) -> Path:
        """Return the ``.kiro`` directory for the active scope."""
        if self.user_scope:
            return Path.home() / ".kiro"
        return self.project_root / ".kiro"

    def get_config_path(self) -> str:
        """Return the Kiro MCP config path for the active scope."""
        return str(self._get_kiro_root() / "settings" / "mcp.json")

    def get_current_config(self) -> dict[str, Any]:
        """Read the current Kiro MCP config."""
        config_path = Path(self.get_config_path())
        if not config_path.exists():
            return {}
        try:
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read %s: %s", config_path, exc)
            return {}

    def update_config(self, config_updates: dict[str, dict[str, Any]]) -> bool | None:
        """Merge *config_updates* into Kiro's ``mcpServers`` object.

        Project scope is opt-in: the workspace must already contain ``.kiro/``.
        User scope creates ``~/.kiro/settings/`` on demand.
        """
        kiro_root = self._get_kiro_root()
        if not self.user_scope and not kiro_root.is_dir():
            logger.debug("Skipping Kiro project-scope write -- %s does not exist", kiro_root)
            return None

        config_path = Path(self.get_config_path())
        current_config = self.get_current_config()
        if not isinstance(current_config.get(self.mcp_servers_key), dict):
            current_config[self.mcp_servers_key] = {}
        current_config[self.mcp_servers_key].update(config_updates)

        config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            config_path,
            json.dumps(current_config, indent=2),
            new_file_mode=0o600,
        )
        os.chmod(config_path, 0o600)
        return True

    @staticmethod
    def _header_mapping(remote: dict[str, Any]) -> dict[str, str]:
        """Return registry remote headers as string key-value pairs."""
        headers = remote.get("headers", {})
        if isinstance(headers, list):
            return {
                str(h["name"]): str(h["value"])
                for h in headers
                if isinstance(h, dict) and "name" in h and "value" in h
            }
        if isinstance(headers, dict):
            return {str(name): str(value) for name, value in headers.items()}
        return {}

    @staticmethod
    def _copy_kiro_extensions(config: dict[str, Any], server_info: dict[str, Any]) -> None:
        """Carry Kiro-specific MCP fields when the registry supplies them."""
        for key in ("autoApprove", "disabledTools", "disabled"):
            if key in server_info and server_info[key] is not None:
                config[key] = server_info[key]

    def _format_server_config(
        self,
        server_info: dict[str, Any],
        env_overrides: dict[str, str] | None = None,
        runtime_vars: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Format registry or self-defined server info for Kiro."""
        if runtime_vars is None:
            runtime_vars = {}

        raw = server_info.get("_raw_stdio")
        if raw:
            config: dict[str, Any] = {"command": raw["command"]}
            resolved_env_for_args: dict[str, Any] = {}
            if raw.get("env"):
                resolved_env_for_args = self._resolve_environment_variables(
                    raw["env"], env_overrides=env_overrides
                )
                config["env"] = resolved_env_for_args
                self._warn_input_variables(raw["env"], server_info.get("name", ""), "Kiro")
            config["args"] = [
                self._resolve_variable_placeholders(arg, resolved_env_for_args, runtime_vars)
                if isinstance(arg, str)
                else arg
                for arg in raw.get("args") or []
            ]
            self._copy_kiro_extensions(config, server_info)
            return config

        remotes = server_info.get("remotes", [])
        if remotes:
            remote = self._select_remote_with_url(remotes) or remotes[0]
            transport = (remote.get("transport_type") or "").strip()
            if not transport:
                transport = "http"
            elif transport not in ("sse", "http", "streamable-http"):
                raise ValueError(
                    f"Unsupported remote transport '{transport}' for Kiro. "
                    f"Server: {server_info.get('name', 'unknown')}. "
                    "Supported transports: http, sse, streamable-http."
                )

            config = {"url": (remote.get("url") or "").strip()}
            headers = {
                name: self._resolve_env_variable(name, value, env_overrides)
                if isinstance(value, str)
                else value
                for name, value in self._header_mapping(remote).items()
                if name
            }
            if headers:
                config["headers"] = headers
                self._warn_input_variables(headers, server_info.get("name", ""), "Kiro")
            self._copy_kiro_extensions(config, server_info)
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
            config,
            packages,
            env_overrides,
            runtime_vars,
        )
        if not package:
            raise ValueError(
                f"No supported package type found for Kiro. "
                f"Server: {server_info.get('name', 'unknown')}."
            )
        self._copy_kiro_extensions(config, server_info)
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
        """Configure an MCP server in Kiro's MCP config."""
        if not server_url:
            _rich_error("server_url cannot be empty", symbol="error")
            return False

        if not self.user_scope and not self._get_kiro_root().is_dir():
            logger.debug(
                "Kiro opt-in gate: %s absent, skipping configure_mcp_server",
                self._get_kiro_root(),
            )
            return True

        config_key = self._determine_config_key(server_url, server_name)

        try:
            server_info = self._fetch_server_info(server_url, server_info_cache)
            if server_info is None:
                return False

            self._last_env_placeholder_keys = set()
            self._last_legacy_angle_vars = set()

            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)
            if not enabled:
                server_config["disabled"] = True
            self.update_config({config_key: server_config})

            _rich_success(f"Configured MCP server '{config_key}' for Kiro", symbol="success")
            return True
        except Exception as exc:
            logger.debug("Kiro MCP configuration failed: %s", exc)
            _rich_error(f"Failed to configure MCP server '{config_key}' for Kiro", symbol="error")
            return False
