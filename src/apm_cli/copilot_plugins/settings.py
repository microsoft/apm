"""APM-owned merge into GitHub Copilot's documented settings surfaces.

APM writes exactly two namespaced facts and nothing else:

* ``extraKnownMarketplaces["apm"]`` -- a directory marketplace pointing at
  APM's materialization root;
* ``enabledPlugins["<plugin>@apm"]`` -- one entry per registered plugin.

Ownership is proven by an APM-owned ledger stored beside the generated
catalog, never by guessing from a path or a value shape. A pre-existing key
that the ledger does not claim is a collision and fails the operation
precisely instead of being overwritten.
"""

from __future__ import annotations

import json
import os
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
    REGISTRATION_LEDGER_VERSION,
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
    configured = os.environ.get("COPILOT_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".copilot"


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


def render_ledger(
    *,
    settings_path: Path,
    marketplace_path: str,
    enabled_plugins: tuple[str, ...],
    project_root: Path,
    scope: InstallScope | Any,
    plugin_owners: dict[str, str] | None = None,
) -> str:
    """Render the APM ownership ledger for the registration just written."""
    if is_user_scope(scope):
        recorded_settings = settings_path.as_posix()
    else:
        from ..utils.paths import portable_relpath

        recorded_settings = portable_relpath(settings_path, project_root)
    document = {
        "version": REGISTRATION_LEDGER_VERSION,
        "marketplace": APM_MARKETPLACE_NAME,
        "marketplaceOwned": True,
        "marketplacePath": marketplace_path,
        "settingsPath": recorded_settings,
        "enabledPlugins": list(enabled_plugins),
        "pluginOwners": dict(sorted((plugin_owners or {}).items())),
    }
    return json.dumps(document, indent=2, ensure_ascii=True) + "\n"


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


def merge_registration(
    *,
    document: dict[str, Any],
    settings_path: Path,
    marketplace_path: str,
    enabled_keys: tuple[str, ...],
    ledger: RegistrationLedger,
) -> SettingsMergeResult:
    """Merge APM's namespaced registration, preserving every other key."""
    merged = dict(document)
    marketplaces = _mapping_at(merged, EXTRA_MARKETPLACES_KEY, settings_path)
    enabled = _mapping_at(merged, ENABLED_PLUGINS_KEY, settings_path)

    desired_marketplace = {"source": {"source": "directory", "path": marketplace_path}}
    existing_marketplace = marketplaces.get(APM_MARKETPLACE_NAME)
    if (
        existing_marketplace is not None
        and not ledger.marketplace_owned
        and existing_marketplace != desired_marketplace
    ):
        raise CopilotSettingsCollisionError(
            f"{settings_path} already defines "
            f"{EXTRA_MARKETPLACES_KEY}['{APM_MARKETPLACE_NAME}'] and APM does not "
            "own it. Remove or rename that entry, then re-run the command; APM "
            "never overwrites settings it did not write."
        )

    result = SettingsMergeResult(settings_path=settings_path, document=merged)
    if existing_marketplace != desired_marketplace:
        marketplaces[APM_MARKETPLACE_NAME] = desired_marketplace
        result.changed = True
        result.added_keys.append(f"{EXTRA_MARKETPLACES_KEY}.{APM_MARKETPLACE_NAME}")

    for key in sorted(set(enabled_keys)):
        if enabled.get(key) is not True:
            enabled[key] = True
            result.changed = True
            result.added_keys.append(f"{ENABLED_PLUGINS_KEY}.{key}")

    # NAMESPACE sweep: the marketplace-ownership check above guarantees APM owns
    # ``extraKnownMarketplaces.apm``, so by construction it owns the whole ``apm``
    # marketplace namespace -- every ``<name>@apm`` enabled key belongs to APM.
    # Retire each namespaced key no longer desired even when the ledger was lost
    # (``rm -rf apm_modules``) and cannot name it; this keeps install fully
    # convergent with no ledger at all. The ledger's own set is folded in so a
    # non-namespaced key APM once wrote is still retired.
    desired = set(enabled_keys)
    apm_suffix = f"@{APM_MARKETPLACE_NAME}"
    for stale in sorted(enabled):
        if stale in desired:
            continue
        if stale.endswith(apm_suffix) or stale in ledger.enabled_plugins:
            del enabled[stale]
            result.changed = True
            result.removed_keys.append(f"{ENABLED_PLUGINS_KEY}.{stale}")

    _store_or_drop(merged, EXTRA_MARKETPLACES_KEY, marketplaces)
    _store_or_drop(merged, ENABLED_PLUGINS_KEY, enabled)
    return result


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
