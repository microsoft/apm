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

Per-server shape (https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference):
  * stdio  -> ``command`` / ``args`` / ``env`` / ``cwd``
  * remote -> ``url`` / ``headers`` / ``transport: sse`` (SSE only; the
    remote keys come from the ``hermes`` row in ``_remote_shapes``)
  * non-denylisted ``extra`` passthrough keys (``timeout``, ``auth``, ...),
    then ``enabled``.

YAML serialization goes through ``utils.yaml_io`` (lint forbids raw
``yaml.dump``); the document is written atomically with ``0o600`` perms via
``utils.atomic_io`` because ``config.yaml`` carries literal credentials.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import yaml

from ...core.command_logger import library_logger
from ...utils.atomic_io import atomic_write_text
from ...utils.yaml_io import load_yaml, yaml_to_str
from ._remote_shapes import remote_shape_row
from .copilot import CopilotClientAdapter

# Credential-bearing config file mode: owner read/write only. Hermes' config.yaml
# holds literal MCP env values plus native model-provider keys / messaging tokens,
# so it must never be group/world-readable (parity with claude/codex/gemini/cursor).
_CONFIG_FILE_MODE = 0o600

# Modeled keys in on-disk order. ``cwd`` is read by Hermes' stdio launcher
# (NousResearch/hermes-agent tools/mcp_tool_transport.py ``_stdio_launch``).
_MODELED_KEYS = ("command", "args", "env", "cwd", "url", "headers", "transport")
# Emitted only when non-empty, so a malformed entry never writes ``url: null``.
_OMIT_WHEN_EMPTY = frozenset({"command", "args", "env", "url", "headers"})
# Copilot CLI fields the shared formatter adds that Hermes does not read the
# same way (``tools`` is a list there but a filter mapping in Hermes).
_COPILOT_ONLY_KEYS = frozenset({"type", "tools", "id"})
# The only keys APM <= 0.36 kept (an allowlist that dropped cwd, transport and
# every passthrough key); used to recognize and heal those earlier writes.
_V036_REMOTE_KEYS = ("url", "headers")
_V036_STDIO_KEYS = ("command", "args", "env")


class _MalformedHermesConfig(Exception):
    """Raised when ``config.yaml`` exists but is not a YAML mapping.

    Signals write paths to refuse the overwrite so a user's native Hermes
    credentials (model-provider keys, Telegram tokens) are never discarded.
    """


class HermesClientAdapter(CopilotClientAdapter):
    """MCP configuration for the Hermes agent (YAML ``mcp_servers`` schema).

    Registry formatting reuses :class:`CopilotClientAdapter`, then entries are
    converted to Hermes' on-disk shape via :meth:`_to_hermes_format`.
    """

    remote_shape = remote_shape_row("hermes")

    supports_user_scope: bool = True
    target_name: str = "hermes"
    mcp_servers_key: str = "mcp_servers"

    # Hermes' config.yaml does NOT support runtime env-var substitution; the
    # value in ``env`` must be a literal string, so install-time resolution
    # is kept (mirrors Claude -- see #1152 supply-chain analysis).
    _supports_runtime_env_substitution: bool = False

    def _config_path(self) -> Path:
        """Resolve ``<hermes-home>/config.yaml`` honouring ``$HERMES_HOME``."""
        from ...integration.targets import resolve_user_root

        # ``resolve`` collapses ``..`` so ``mkdir(parents=True)`` cannot create
        # unintended intermediate directories.
        return resolve_user_root("hermes").resolve(strict=False) / "config.yaml"

    @staticmethod
    def _to_hermes_format(copilot_entry: dict, *, enabled: bool = True) -> dict:
        """Convert a Copilot-format server entry to Hermes' on-disk shape.

        Keeps the modeled keys (``command``/``args``/``env``/``cwd`` for
        stdio; ``url``/``headers``/``transport`` for remotes) and every
        passthrough key the shared formatter merged through the ``_extra``
        denylist (``timeout``, ``auth``, ...). Drops Copilot-CLI-only fields
        (``type``, ``tools``, ``id``) and stamps an explicit ``enabled`` last.
        Idempotent, so an entry already in Hermes' shape converts unchanged.
        """
        if not isinstance(copilot_entry, dict):
            return copilot_entry

        out: dict = {}
        for key in _MODELED_KEYS:
            value = copilot_entry.get(key)
            if value is None or (key in _OMIT_WHEN_EMPTY and not value):
                continue
            if key == "args":
                value = list(value)
            elif key in ("env", "headers"):
                value = dict(value)
            out[key] = value
        skipped = {*_MODELED_KEYS, *_COPILOT_ONLY_KEYS, "enabled"}
        for key, value in copilot_entry.items():
            if key not in skipped:
                out[key] = value
        out["enabled"] = enabled
        return out

    @staticmethod
    def _v036_render(entry: dict) -> dict:
        """The entry APM <= 0.36 wrote for the same declaration (lossy allowlist)."""
        keys = _V036_REMOTE_KEYS if entry.get("url") else _V036_STDIO_KEYS
        out = {key: entry[key] for key in keys if entry.get(key)}
        out["enabled"] = entry.get("enabled", True)
        return out

    def _format_server_config(self, server_info, env_overrides=None, runtime_vars=None):
        """Render the native Hermes entry, so ownership compares on-disk content."""
        config = super()._format_server_config(server_info, env_overrides, runtime_vars)
        return self._to_hermes_format(config) if isinstance(config, dict) else config

    def legacy_managed_renders(
        self,
        server_info: dict,
        *,
        env_overrides: dict | None = None,
        runtime_vars: dict | None = None,
    ) -> tuple[dict, ...]:
        """Add the 0.36 allowlist render, which also dropped stdio ``cwd`` and extras."""
        kwargs = {"env_overrides": env_overrides, "runtime_vars": runtime_vars}
        renders = list(super().legacy_managed_renders(server_info, **kwargs))
        try:
            current = self.render_server_config(server_info, **kwargs)
        except ValueError:
            return tuple(renders)
        legacy = self._v036_render(current)
        if legacy != current and legacy not in renders:
            renders.append(legacy)
        return tuple(renders)

    def get_config_path(self):
        """Path to the Hermes config file (``<hermes-home>/config.yaml``)."""
        return str(self._config_path())

    def _load_document(self) -> dict:
        """Load the full ``config.yaml`` document (preserving siblings).

        Returns ``{}`` when the file is absent or empty.  Raises
        :class:`_MalformedHermesConfig` when the file exists but is not a YAML
        mapping (parse error or non-dict root) so write paths can refuse to
        overwrite and silently discard the user's native Hermes credentials.
        """
        path = self._config_path()
        if not path.is_file():
            return {}
        try:
            data = load_yaml(path)
        except (OSError, yaml.YAMLError) as exc:
            raise _MalformedHermesConfig(str(path)) from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise _MalformedHermesConfig(str(path))
        return data

    def get_current_config(self):
        """Return ``{"mcp_servers": {...}}`` for the on-disk config."""
        try:
            data = self._load_document()
        except _MalformedHermesConfig:
            return {self.mcp_servers_key: {}}
        servers = data.get(self.mcp_servers_key)
        return {self.mcp_servers_key: dict(servers) if isinstance(servers, dict) else {}}

    def update_config(self, config_updates, enabled=True):
        """Merge *config_updates* into the ``mcp_servers:`` block.

        Entries are normalized to Hermes' shape.  Per-server entries are
        replaced on key conflict; unrelated servers and all other top-level
        config keys are preserved.  The file is written atomically with
        ``0o600`` permissions so the credential-bearing config is never left
        group/world-readable.  A malformed existing ``config.yaml`` is left
        untouched (returns ``False``) rather than overwritten.
        """
        path = self._config_path()
        try:
            data = self._load_document()
        except _MalformedHermesConfig:
            library_logger().error(
                f"{path} is malformed YAML; refusing to overwrite. "
                "Fix or remove the file manually, then retry."
            )
            return False
        try:
            servers = data.get(self.mcp_servers_key)
            if not isinstance(servers, dict):
                servers = {}
            for name, cfg in config_updates.items():
                servers[name] = self._to_hermes_format(cfg, enabled=enabled)
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
            library_logger().error("server_url cannot be empty")
            return False

        try:
            server_info = self._fetch_server_info(server_url, server_info_cache)
            if server_info is None:
                return False

            config_key = self.determine_config_key(server_url, server_name, server_info)
            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)
            ok = self.update_config({config_key: server_config}, enabled=enabled)
            if not ok:
                library_logger().error(f"Failed to write MCP config for '{config_key}' to Hermes")
                return False

            # The integrator's install tree reports success once (#3994).
            return True
        except Exception:
            # Do not interpolate the exception message: registry URLs and
            # other inputs may carry embedded credentials.
            library_logger().error("Error configuring MCP server")
            return False
