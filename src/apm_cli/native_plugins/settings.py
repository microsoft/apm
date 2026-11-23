"""APM's only writer of documented native-plugin settings keys (Claude Code and Copilot).

Harnesses that load a local directory marketplace in place (Claude Code and
GitHub Copilot) read two documented keys from a settings JSON document:

* ``extraKnownMarketplaces.<name>`` -- ``{"source": {"source": "directory",
  "path": <directory>}}``, absolute at user scope and ``./.agents/plugins``
  (relative to the project root) at project scope;
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
    path: str
    """Directory source exactly as written: absolute, or relative at project scope."""
    plugins: frozenset[str]
    owned_plugins: frozenset[str] | None
    """Plugins the previous lockfile activated; ``None`` when there was no previous lock."""
    previous_binding: tuple[str, str] | None = None
    """``(name, path)`` the previous lock's entries were declared under, if any."""
    suppressed: frozenset[str] = frozenset()
    """Plugins to switch off here because another harness's file this one also
    reads declares them (cross-target suppression, written as ``false``)."""
    managed: frozenset[str] = frozenset()
    """Plugins whose enable value here APM owns whatever it is: those it declares
    to the harness whose file this one also reads. Their ``false`` is APM's
    suppression, flipped or retired as their targets change."""
    catalog: frozenset[str] = frozenset()
    """Plugins APM's catalog still deploys: a user's own ``true`` for one, in a
    harness APM no longer declares it to, keeps the binding it loads through."""

    @property
    def marketplace_value(self) -> dict[str, Any]:
        return {"source": {"source": "directory", "path": self.path}}

    def plugin_key(self, plugin: str) -> str:
        return f"{plugin}@{self.marketplace}"


@dataclass
class SettingsChange:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    flipped: list[tuple[str, bool]] = field(default_factory=list)
    """``(key, new value)`` for an existing enable value APM changed (a managed key)."""

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
        from apm_cli.core.remediation import NATIVE_SETTINGS_INVALID

        raise NativeSettingsError(
            f"Cannot read {path} as JSON; APM did not change it. "
            + NATIVE_SETTINGS_INVALID.sentence()
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
    binding = (name, declaration.path)
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
    # Without a previous lock nothing existing is APM's to flip or retire.
    managed = declaration.managed if owned is not None else frozenset()
    for plugin in sorted(declaration.plugins):
        key = declaration.plugin_key(plugin)
        if key not in enabled or (plugin in managed and enabled[key] is False):
            if key in enabled:
                change.flipped.append((key, True))
            enabled[key] = True
            change.added.append(f"{ENABLED_KEY}.{key}")
    for plugin in sorted(declaration.suppressed - declaration.plugins):
        key = declaration.plugin_key(plugin)
        if key not in enabled or (plugin in managed and enabled[key] is True):
            if key in enabled:
                change.flipped.append((key, False))
            enabled[key] = False
            change.added.append(f"{ENABLED_KEY}.{key}")
    wanted = declaration.plugins | declaration.suppressed
    for plugin in sorted((owned or frozenset()) - wanted):
        key = declaration.plugin_key(plugin)
        # An explicit false is the user's decision; only APM's true, or APM's
        # own suppression, is retired.
        if enabled.get(key) is True or (plugin in managed and enabled.get(key) is False):
            del enabled[key]
            change.removed.append(f"{ENABLED_KEY}.{key}")
    # The binding goes once APM declares nothing here, unless a user's own
    # enable of a plugin the catalog still deploys needs it (#4874).
    if not declaration.plugins and current is not None and owned:
        if not any(
            enabled.get(declaration.plugin_key(plugin)) is True for plugin in declaration.catalog
        ):
            del marketplaces[name]
            change.removed.append(f"{MARKETPLACES_KEY}.{name}")
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


def write_settings_document(path: Path, document: dict[str, Any]) -> None:
    """Write a whole settings document APM already changed in memory."""
    target = settings_target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, json.dumps(document, indent=2) + "\n")


def write_declaration(path: Path, declaration: SettingsDeclaration) -> SettingsChange:
    """Apply *declaration* to the settings file at *path*, writing only on change."""
    target = settings_target(path)
    document = read_settings(target)
    change = apply_declaration(document, declaration, target)
    if change.changed:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, json.dumps(document, indent=2) + "\n")
    return change
