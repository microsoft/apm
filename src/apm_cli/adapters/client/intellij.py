"""JetBrains GitHub Copilot implementation of the MCP client adapter.

The GitHub Copilot plugin reads its user-scope MCP configuration from:

  Windows  : %LOCALAPPDATA%\\github-copilot\\intellij\\mcp.json
  macOS    : $XDG_CONFIG_HOME/github-copilot/intellij/mcp.json
  Linux    : $XDG_CONFIG_HOME/github-copilot/intellij/mcp.json

``XDG_CONFIG_HOME`` defaults to ``~/.config``. Older APM releases selected
data-state locations on macOS and Linux; this module retains those paths only
for provenance-gated forward migration.

The configuration file uses a top-level ``"servers"`` key.

Ref: https://github.com/JetBrains/intellij-community/blob/master/plugins/
mcp-server/src/com/intellij/mcpserver/clients/impl/GitHubCopilotIdePluginClient.kt
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import sys
from pathlib import Path

from apm_cli.utils.atomic_io import atomic_write_text

from ...utils.path_security import PathTraversalError, ensure_path_within
from .copilot import CopilotClientAdapter

_CONFIG_SUFFIX = ("github-copilot", "intellij")


class IntelliJConfigError(RuntimeError):
    """Raised when JetBrains Copilot config cannot be read or written safely."""


def _absolute_environment_root(name: str) -> Path:
    """Return one required absolute platform path from the environment."""
    value = os.environ.get(name, "")
    path_module = ntpath if sys.platform == "win32" else posixpath
    if not value or not path_module.isabs(value):
        raise PathTraversalError(
            f"{name} is unset or not an absolute path; cannot locate the "
            "JetBrains Copilot configuration directory."
        )
    return Path(value)


def _xdg_root(name: str, default: Path) -> Path:
    """Resolve an optional absolute XDG root, falling back to ``default``."""
    value = os.environ.get(name, "")
    if not value:
        return default
    if not posixpath.isabs(value):
        raise PathTraversalError(
            f"{name} is set to a non-absolute path; cannot locate the "
            "JetBrains Copilot configuration directory."
        )
    return Path(value)


def _intellij_config_root() -> Path:
    """Return the canonical root for JetBrains Copilot config state."""
    if sys.platform == "win32":
        return _absolute_environment_root("LOCALAPPDATA")
    return _xdg_root("XDG_CONFIG_HOME", Path.home() / ".config")


def _intellij_config_dir() -> Path:
    """Return the canonical JetBrains Copilot MCP configuration directory."""
    return _intellij_config_root().joinpath(*_CONFIG_SUFFIX)


def _legacy_intellij_config_root() -> Path | None:
    """Return the data-state root used by older APM versions."""
    if sys.platform == "win32":
        return None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return _xdg_root("XDG_DATA_HOME", Path.home() / ".local" / "share")


def _legacy_intellij_config_dir() -> Path | None:
    """Return the obsolete APM-selected directory, if it differs by platform."""
    root = _legacy_intellij_config_root()
    if root is None:
        return None
    return root.joinpath(*_CONFIG_SUFFIX)


def _config_without_servers(
    config: dict,
    names: set[str],
    servers_key: str,
) -> tuple[dict, set[str]]:
    updated = dict(config)
    servers = dict(updated.get(servers_key) or {})
    removed = names & servers.keys()
    for name in removed:
        del servers[name]
    updated[servers_key] = servers
    return updated, set(removed)


class IntelliJClientAdapter(CopilotClientAdapter):
    """MCP client adapter for GitHub Copilot inside JetBrains IDEs."""

    supports_user_scope: bool = True
    _client_label: str = "JetBrains Copilot"
    target_name: str = "intellij"
    mcp_servers_key: str = "servers"
    _supports_runtime_env_substitution: bool = True

    def _format_runtime_env_placeholder(self, name: str) -> str:
        """Return JetBrains Copilot's native env-var placeholder syntax."""
        return "${env:" + name + "}"

    def get_config_path(self) -> str:
        """Return the canonical JetBrains Copilot ``mcp.json`` path."""
        root = _intellij_config_root()
        config_dir = root.joinpath(*_CONFIG_SUFFIX)
        # XDG roots may live outside HOME. This containment check guards the
        # fixed suffix against symlink escapes from the selected platform root.
        ensure_path_within(config_dir, root)
        config_path = config_dir / "mcp.json"
        ensure_path_within(config_path, root)
        return str(config_path)

    def get_legacy_config_path(self) -> str | None:
        """Return the obsolete APM path used before the config-state fix."""
        root = _legacy_intellij_config_root()
        if root is None:
            return None
        config_dir = root.joinpath(*_CONFIG_SUFFIX)
        ensure_path_within(config_dir, root)
        legacy_path = config_dir / "mcp.json"
        ensure_path_within(legacy_path, root)
        if legacy_path.resolve() == Path(self.get_config_path()).resolve():
            return None
        return str(legacy_path)

    def _read_config(self, path: Path) -> dict:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError) as exc:
            raise IntelliJConfigError(
                f"Cannot read JetBrains Copilot MCP config at '{path}'. "
                "Check its permissions and UTF-8 encoding, then rerun apm install."
            ) from exc
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise IntelliJConfigError(
                f"JetBrains Copilot MCP config at '{path}' is malformed JSON ({exc}). "
                "Fix the file, then rerun apm install."
            ) from exc
        if not isinstance(config, dict):
            raise IntelliJConfigError(
                f"JetBrains Copilot MCP config at '{path}' must contain a JSON object. "
                "Fix the file, then rerun apm install."
            )
        servers = config.get(self.mcp_servers_key)
        if self.mcp_servers_key in config and not isinstance(servers, dict):
            raise IntelliJConfigError(
                f"JetBrains Copilot MCP config at '{path}' has a non-object "
                "'servers' value. Fix the file, then rerun apm install."
            )
        return config

    def _validate_owned_config_path(self, path: Path) -> None:
        """Reject writes outside the adapter-owned canonical and legacy files."""
        candidate = os.path.normcase(os.path.abspath(path))
        canonical = os.path.normcase(os.path.abspath(self.get_config_path()))
        if candidate == canonical:
            return
        legacy_path = self.get_legacy_config_path()
        if legacy_path is not None and candidate == os.path.normcase(os.path.abspath(legacy_path)):
            return
        raise IntelliJConfigError(
            f"Refusing to write JetBrains Copilot MCP config outside adapter-owned paths: '{path}'."
        )

    def _write_config(self, path: Path, config: dict) -> None:
        try:
            self._validate_owned_config_path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(config, indent=2) + "\n")
        except OSError as exc:
            raise IntelliJConfigError(
                f"Cannot write JetBrains Copilot MCP config at '{path}'. "
                "Check permissions and free space, then rerun apm install."
            ) from exc

    def get_current_config(self) -> dict:
        """Read canonical config, failing closed on malformed or unreadable data."""
        return self._read_config(Path(self.get_config_path()))

    def get_legacy_current_config(self) -> dict:
        """Read obsolete config for conservative ownership adoption."""
        legacy_path = self.get_legacy_config_path()
        if legacy_path is None:
            return {}
        return self._read_config(Path(legacy_path))

    def update_config(self, config_updates: dict) -> None:
        """Merge server updates into canonical config with an atomic write."""
        current = self.get_current_config()
        servers = dict(current.get(self.mcp_servers_key) or {})
        servers.update(config_updates)
        updated = dict(current)
        updated[self.mcp_servers_key] = servers
        self._write_config(Path(self.get_config_path()), updated)

    def migrate_legacy_managed_servers(self, owned_names: set[str]) -> set[str]:
        """Project only provenance-owned legacy entries into canonical config."""
        if not owned_names:
            return set()
        legacy_path_value = self.get_legacy_config_path()
        if legacy_path_value is None:
            return set()
        canonical_path = Path(self.get_config_path())
        legacy_path = Path(legacy_path_value)
        if not legacy_path.exists():
            return set()

        canonical = self._read_config(canonical_path)
        legacy = self._read_config(legacy_path)
        legacy_servers = dict(legacy.get(self.mcp_servers_key) or {})
        migrated = set(legacy_servers)
        migrated.intersection_update(owned_names)
        if not migrated:
            return set()

        canonical_servers = dict(canonical.get(self.mcp_servers_key) or {})
        for name in sorted(migrated):
            if name not in canonical_servers:
                canonical_servers[name] = legacy_servers[name]
        canonical_updated = dict(canonical)
        canonical_updated[self.mcp_servers_key] = canonical_servers
        legacy_updated, legacy_removed = _config_without_servers(
            legacy,
            set(migrated),
            self.mcp_servers_key,
        )

        if canonical_updated != canonical:
            self._write_config(canonical_path, canonical_updated)
        if legacy_removed:
            self._write_config(legacy_path, legacy_updated)
        return set(migrated)

    def remove_managed_servers(self, owned_names: set[str]) -> set[str]:
        """Remove only provenance-owned names from canonical and obsolete config."""
        names = set(owned_names)
        canonical_path = Path(self.get_config_path())
        legacy_path_value = self.get_legacy_config_path()
        legacy_path = Path(legacy_path_value) if legacy_path_value is not None else None

        canonical = self._read_config(canonical_path)
        legacy = self._read_config(legacy_path) if legacy_path is not None else {}
        canonical_updated, canonical_removed = _config_without_servers(
            canonical,
            names,
            self.mcp_servers_key,
        )
        legacy_updated, legacy_removed = _config_without_servers(
            legacy,
            names,
            self.mcp_servers_key,
        )

        if canonical_removed:
            self._write_config(canonical_path, canonical_updated)
        if legacy_path is not None and legacy_removed:
            self._write_config(legacy_path, legacy_updated)
        return canonical_removed | legacy_removed
