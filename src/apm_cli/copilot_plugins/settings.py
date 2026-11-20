"""Reading APM's legacy Copilot registration so it can be retired (#3921).

The earlier route wrote two namespaced facts, ``extraKnownMarketplaces["apm"]``
and ``enabledPlugins["<plugin>@apm"]``, and proved ownership with a ledger
beside its generated catalog. These helpers read that ledger and remove only
what it proves; new declarations go through ``native_plugins/settings.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.scope import InstallScope, is_user_scope
from .constants import (
    APM_MARKETPLACE_NAME,
    ENABLED_PLUGINS_KEY,
    EXTRA_MARKETPLACES_KEY,
    PROJECT_SETTINGS_LOCAL_RELATIVE,
    PROJECT_SETTINGS_SHARED_RELATIVE,
    USER_SETTINGS_FILENAME,
)


class CopilotSettingsCollisionError(ValueError):
    """Raised when a settings key APM must own is held by someone else."""

    detail: str | None = None
    """Verbose-only diagnostic (e.g. a raw JSON decoder message)."""


@dataclass(frozen=True, slots=True)
class RegistrationLedger:
    """APM's record of what it owns in one Copilot settings document."""

    settings_path: Path | None = None
    marketplace_owned: bool = False
    enabled_plugins: tuple[str, ...] = ()
    marketplace_path: str | None = None
    plugin_owners: dict[str, str] = field(default_factory=dict)
    unreadable: bool = False

    def owner_of(self, key: str) -> str | None:
        """Return the dependency key that last owned *key*, if recorded."""
        return self.plugin_owners.get(key)


@dataclass
class SettingsMergeResult:
    """Outcome of one APM-owned settings merge or removal."""

    settings_path: Path
    document: dict[str, Any]
    changed: bool = False
    removed_keys: list[str] = field(default_factory=list)
    added_keys: list[str] = field(default_factory=list)


def copilot_home() -> Path:
    """Return the Copilot CLI configuration home, honoring ``COPILOT_HOME``."""
    from apm_cli.integration.targets import resolve_user_root

    return resolve_user_root("copilot")


def resolve_settings_path(
    scope: InstallScope | Any,
    project_root: Path,
    ledger: RegistrationLedger,
) -> Path:
    """Return the settings document APM activates plugins in.

    Global scope always uses the user's Copilot settings. Project scope
    prefers the machine-local ``settings.local.json`` so a shared repository
    file is never silently dirtied; an existing APM registration recorded in
    another documented project surface is honored instead.
    """
    if is_user_scope(scope):
        return copilot_home() / USER_SETTINGS_FILENAME
    recorded = ledger.settings_path
    if recorded is not None:
        candidate = recorded if recorded.is_absolute() else project_root / recorded
        if candidate.name in {
            Path(PROJECT_SETTINGS_LOCAL_RELATIVE).name,
            Path(PROJECT_SETTINGS_SHARED_RELATIVE).name,
        }:
            return candidate
    return project_root / PROJECT_SETTINGS_LOCAL_RELATIVE


def read_settings_document(path: Path) -> dict[str, Any]:
    """Read one settings document, returning ``{}`` when it does not exist."""
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CopilotSettingsCollisionError(
            f"Cannot read {path} to register the Agent Plugin: {exc}"
        ) from exc
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        error = CopilotSettingsCollisionError(
            f"{path} is not valid JSON, so APM could not register the Agent "
            "Plugin and no packages were installed. Fix or delete the file, "
            "then re-run 'apm install'."
        )
        error.detail = str(exc)
        raise error from exc
    if not isinstance(loaded, dict):
        raise CopilotSettingsCollisionError(
            f"Cannot merge APM plugin registration into {path}: "
            "expected a JSON object at the document root"
        )
    return loaded


def render_settings_document(document: dict[str, Any]) -> str:
    """Render a settings document with stable formatting."""
    return json.dumps(document, indent=2, ensure_ascii=True) + "\n"


def read_ledger(path: Path) -> RegistrationLedger:
    """Read the APM ownership ledger, returning an empty ledger when absent.

    A missing ledger is fail-closed on merge (APM owns nothing) and safe on
    removal (nothing to retire). A ledger that EXISTS but cannot be parsed is
    different: silently treating it as empty would fail OPEN on removal, so it
    is flagged ``unreadable`` and callers refuse destructive operations.
    """
    if not path.is_file():
        return RegistrationLedger()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return RegistrationLedger(unreadable=True)
    if not isinstance(loaded, dict):
        return RegistrationLedger(unreadable=True)
    settings_path = loaded.get("settingsPath")
    enabled = loaded.get("enabledPlugins")
    owners_raw = loaded.get("pluginOwners")
    owners = (
        {k: v for k, v in owners_raw.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(owners_raw, dict)
        else {}
    )
    return RegistrationLedger(
        settings_path=Path(settings_path) if isinstance(settings_path, str) else None,
        marketplace_owned=bool(loaded.get("marketplaceOwned", False)),
        enabled_plugins=tuple(value for value in (enabled or ()) if isinstance(value, str)),
        marketplace_path=(
            loaded.get("marketplacePath")
            if isinstance(loaded.get("marketplacePath"), str)
            else None
        ),
        plugin_owners=owners,
    )


def _mapping_at(document: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    """Return the mutable mapping stored at *key*, rejecting other shapes."""
    value = document.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CopilotSettingsCollisionError(
            f"Cannot merge APM plugin registration into {path}: '{key}' is not a JSON object"
        )
    return dict(value)


def remove_registration(
    *,
    document: dict[str, Any],
    settings_path: Path,
    ledger: RegistrationLedger,
) -> SettingsMergeResult:
    """Remove only APM-owned entries, leaving user-authored settings intact."""
    merged = dict(document)
    marketplaces = _mapping_at(merged, EXTRA_MARKETPLACES_KEY, settings_path)
    enabled = _mapping_at(merged, ENABLED_PLUGINS_KEY, settings_path)
    result = SettingsMergeResult(settings_path=settings_path, document=merged)

    if ledger.marketplace_owned and APM_MARKETPLACE_NAME in marketplaces:
        del marketplaces[APM_MARKETPLACE_NAME]
        result.changed = True
        result.removed_keys.append(f"{EXTRA_MARKETPLACES_KEY}.{APM_MARKETPLACE_NAME}")

    for key in sorted(ledger.enabled_plugins):
        if key in enabled:
            del enabled[key]
            result.changed = True
            result.removed_keys.append(f"{ENABLED_PLUGINS_KEY}.{key}")

    _store_or_drop(merged, EXTRA_MARKETPLACES_KEY, marketplaces)
    _store_or_drop(merged, ENABLED_PLUGINS_KEY, enabled)
    return result


def _store_or_drop(document: dict[str, Any], key: str, value: dict[str, Any]) -> None:
    """Write *value* back, dropping the key entirely once it is empty."""
    if value:
        document[key] = value
    elif key in document:
        del document[key]
