"""Only writer for lock-owned native plugins; copilot_plugins/settings.py converges here in #3921.

Harnesses that load a local directory marketplace in place (Claude Code today,
GitHub Copilot after #3921) read two documented keys from a settings JSON
document:

* ``extraKnownMarketplaces.<name>`` -- ``{"source": {"source": "directory",
  "path": <absolute directory>}}``;
* ``enabledPlugins["<plugin>@<name>"]`` -- ``true`` or ``false``.

The caller supplies ownership: the plugin names the previous lockfile
activated (OpenAPM req-na-004). APM never removes a key it did not produce,
keeps every user entry, and never overrides an explicit ``false``.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apm_cli.utils.atomic_io import atomic_write_text

MARKETPLACES_KEY = "extraKnownMarketplaces"
ENABLED_KEY = "enabledPlugins"


class NativeSettingsError(ValueError):
    """A settings document cannot safely carry APM's declarations."""


BINDING_CONFLICT = "binding-conflict"
BINDING_MOVED = "binding-moved"
PREVIOUS_LOCK_MISSING = "previous-lock-missing"


@dataclass(frozen=True)
class SettingsDeclaration:
    """What APM wants one settings document to declare (OpenAPM req-na-004)."""

    marketplace: str
    directory: Path
    plugins: frozenset[str]
    owned_plugins: frozenset[str] | None
    """Plugins the previous lockfile activated; ``None`` when there was no previous lock."""
    previous_binding: tuple[str, str] | None = None
    """``(name, path)`` the previous lock's entries were declared under, if any."""

    @property
    def marketplace_value(self) -> dict[str, Any]:
        return {"source": {"source": "directory", "path": str(self.directory)}}

    def plugin_key(self, plugin: str) -> str:
        return f"{plugin}@{self.marketplace}"


@dataclass
class SettingsChange:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def read_settings(path: Path) -> dict[str, Any]:
    """Read a settings document, returning ``{}`` when it does not exist."""
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeSettingsError(
            f"Cannot read {path} as JSON; APM did not change it. Repair the file and retry."
        ) from exc
    if not isinstance(document, dict):
        raise NativeSettingsError(f"{path} must contain a JSON object; APM did not change it.")
    return document


def _section(document: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    value = document.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise NativeSettingsError(f"'{key}' in {path} is not a JSON object; APM did not change it.")
    return dict(value)


def apply_declaration(
    document: dict[str, Any], declaration: SettingsDeclaration, path: Path
) -> SettingsChange:
    """Update *document* in place: add what is needed, retire only what APM produced.

    A binding that moved or was renamed since the previous lock, or a same-name
    marketplace with another source, changes nothing. Without a previous lock
    nothing is removed, and existing entries under APM's name are reported
    rather than adopted or deleted.
    """
    marketplaces = _section(document, MARKETPLACES_KEY, path)
    enabled = _section(document, ENABLED_KEY, path)
    change = SettingsChange()
    name = declaration.marketplace
    current = marketplaces.get(name)
    binding = (name, str(declaration.directory))
    moved = declaration.previous_binding is not None and declaration.previous_binding != binding
    # Another name already points at this profile root: a renamed binding.
    renamed = any(
        other != name and value == declaration.marketplace_value
        for other, value in marketplaces.items()
    )
    if moved or renamed:
        change.diagnostics.append(BINDING_MOVED)
        return change
    if current is not None and current != declaration.marketplace_value:
        change.diagnostics.append(BINDING_CONFLICT)
        return change
    owned = declaration.owned_plugins
    if owned is None and (current is not None or any(key.endswith(f"@{name}") for key in enabled)):
        change.diagnostics.append(PREVIOUS_LOCK_MISSING)
    if declaration.plugins and current is None:
        marketplaces[name] = declaration.marketplace_value
        change.added.append(f"{MARKETPLACES_KEY}.{name}")
    elif not declaration.plugins and current is not None and owned:
        del marketplaces[name]
        change.removed.append(f"{MARKETPLACES_KEY}.{name}")
    for plugin in sorted(declaration.plugins):
        key = declaration.plugin_key(plugin)
        if key not in enabled:
            enabled[key] = True
            change.added.append(f"{ENABLED_KEY}.{key}")
    for plugin in sorted((owned or frozenset()) - declaration.plugins):
        key = declaration.plugin_key(plugin)
        # An explicit false is the user's decision; only APM's true is retired.
        if enabled.get(key) is True:
            del enabled[key]
            change.removed.append(f"{ENABLED_KEY}.{key}")
    for key, section in ((MARKETPLACES_KEY, marketplaces), (ENABLED_KEY, enabled)):
        # An emptied section the user already had stays, as an empty object.
        if section or key in document:
            document[key] = section
    return change


def settings_target(path: Path) -> Path:
    """The file a settings write lands in: a symlinked document is written through."""
    return path.resolve() if path.is_symlink() else path


def preview_declaration(path: Path, declaration: SettingsDeclaration) -> SettingsChange:
    """What :func:`write_declaration` would change, without writing anything."""
    target = settings_target(path)
    return apply_declaration(copy.deepcopy(read_settings(target)), declaration, target)


def snapshot_settings(path: Path) -> bytes | None:
    """The settings document's exact bytes, or ``None`` when it does not exist."""
    target = settings_target(path)
    return target.read_bytes() if target.exists() else None


def restore_settings(path: Path, snapshot: bytes | None) -> None:
    """Put back the bytes (or the absence) :func:`snapshot_settings` recorded."""
    target = settings_target(path)
    if snapshot_settings(path) == snapshot:
        return
    if snapshot is None:
        target.unlink(missing_ok=True)
        return
    # A snapshot APM may restore was readable as UTF-8 JSON before the write.
    atomic_write_text(target, snapshot.decode("utf-8"), normalize_line_endings=False)


def write_declaration(path: Path, declaration: SettingsDeclaration) -> SettingsChange:
    """Apply *declaration* to the settings file at *path*, writing only on change."""
    target = settings_target(path)
    document = read_settings(target)
    change = apply_declaration(document, declaration, target)
    if change.changed:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, json.dumps(document, indent=2) + "\n")
    return change
