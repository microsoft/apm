"""Shared base for MCP client adapters backed by a YAML config document.

Some agent runtimes (Hermes, Goose) store their MCP servers in a single
YAML file with one top-level mapping of server-name -> config, rather than
the JSON ``mcpServers`` schema used by Claude/Copilot.  This base captures
the boilerplate common to those adapters -- safe round-trip load, sibling
preservation, atomic ``0o600`` write, malformed-file refusal, and the
``configure_mcp_server`` registry-fetch flow -- so each concrete adapter
only declares:

  * :attr:`mcp_servers_key`     -- top-level key holding the servers mapping
  * :attr:`target_name`         -- canonical target id
  * :attr:`_display_name`       -- human-facing name used in messages
  * :meth:`_config_path`        -- the YAML file location
  * :meth:`_to_native_format`   -- per-server schema transform

Registry formatting (package/remote resolution, env-var handling) is
inherited from :class:`CopilotClientAdapter`; the per-runtime
:meth:`_to_native_format` hook converts each Copilot-format entry to the
runtime's on-disk shape.
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

# Credential-bearing config file mode: owner read/write only. These config
# files hold literal MCP env values plus native model-provider keys, so they
# must never be group/world-readable (parity with claude/codex/gemini/cursor).
_CONFIG_FILE_MODE = 0o600


class _MalformedYamlConfig(Exception):
    """Raised when the YAML config exists but is not a mapping.

    Signals write paths to refuse the overwrite so a user's native runtime
    config (model-provider keys, unrelated servers) is never discarded.
    """


class YamlMcpClientAdapter(CopilotClientAdapter):
    """Base for MCP adapters whose on-disk config is a YAML servers mapping."""

    supports_user_scope: bool = True

    # These YAML configs do NOT support runtime env-var substitution; the
    # value in the env block must be a literal string, so install-time
    # resolution is kept (see #1152 supply-chain analysis).
    _supports_runtime_env_substitution: bool = False

    # Human-facing name used in console messages; subclasses may override to
    # preserve a specific casing (e.g. "Hermes") distinct from ``target_name``.
    _display_name: str = ""

    def _config_path(self) -> Path:
        """Return the YAML config file path. Must be overridden."""
        raise NotImplementedError

    def _to_native_format(self, name: str, copilot_entry: dict, *, enabled: bool = True) -> dict:
        """Convert a Copilot-format entry to the runtime's shape. Override."""
        raise NotImplementedError

    @property
    def _label(self) -> str:
        """Human-facing runtime name for messages."""
        return self._display_name or self.target_name

    def get_config_path(self):
        """Path to the runtime's YAML config file."""
        return str(self._config_path())

    def _load_document(self) -> dict:
        """Load the full config document (preserving siblings).

        Returns ``{}`` when the file is absent or empty.  Raises
        :class:`_MalformedYamlConfig` when the file exists but is not a YAML
        mapping (parse error or non-dict root) so write paths can refuse to
        overwrite and silently discard the user's native config.
        """
        path = self._config_path()
        if not path.is_file():
            return {}
        try:
            data = load_yaml(path)
        except (OSError, yaml.YAMLError) as exc:
            raise _MalformedYamlConfig(str(path)) from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise _MalformedYamlConfig(str(path))
        return data

    def get_current_config(self):
        """Return ``{<mcp_servers_key>: {...}}`` for the on-disk config."""
        try:
            data = self._load_document()
        except _MalformedYamlConfig:
            return {self.mcp_servers_key: {}}
        servers = data.get(self.mcp_servers_key)
        return {self.mcp_servers_key: dict(servers) if isinstance(servers, dict) else {}}

    def update_config(self, config_updates, enabled=True):
        """Merge *config_updates* into the servers mapping.

        Entries are normalized via :meth:`_to_native_format`.  Per-server
        entries are replaced on key conflict; unrelated servers and all other
        top-level config keys are preserved.  The file is written atomically
        with ``0o600`` permissions so the credential-bearing config is never
        left group/world-readable.  A malformed existing file is left
        untouched (returns ``False``) rather than overwritten.
        """
        path = self._config_path()
        try:
            data = self._load_document()
        except _MalformedYamlConfig:
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
                servers[name] = self._to_native_format(name, cfg, enabled=enabled)
            data[self.mcp_servers_key] = servers
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, yaml_to_str(data), new_file_mode=_CONFIG_FILE_MODE)
            # Tighten perms even when the file pre-existed with a looser mode
            # (atomic_write_text only applies new_file_mode on first create).
            with contextlib.suppress(OSError, NotImplementedError):
                os.chmod(path, _CONFIG_FILE_MODE)
            return True
        except OSError:
            return False
        except (TypeError, ValueError):
            # A per-server transform (_to_native_format) rejected malformed
            # registry/config data. Fail closed like any other write failure
            # rather than crashing the install. Do not interpolate the
            # exception -- inputs may carry embedded credentials.
            _rich_error(f"Could not serialize MCP config for {self._label}; skipping write.")
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
                _rich_error(f"Failed to write MCP config for '{config_key}' to {self._label}")
                return False

            _rich_success(f"Successfully configured MCP server '{config_key}' for {self._label}")
            return True
        except Exception:
            # Do not interpolate the exception message: registry URLs and
            # other inputs may carry embedded credentials.
            _rich_error("Error configuring MCP server")
            return False
