"""Legacy GitHub Copilot registration state, read only to migrate it (#3921).

Earlier releases declared Agent Plugins to Copilot through a marketplace named
``apm`` pointing at ``apm_modules`` and recorded ownership in an
``apm-registration.json`` ledger beside the generated catalog. Copilot now
reads natively activated plugins from ``.agents/plugins`` through the shared
settings writer, with ownership in ``apm.lock.yaml``; this module only reads
the old ledger so :func:`~apm_cli.copilot_plugins.registrar.plan_legacy_copilot_retirement`
can remove what it recorded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


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


def copilot_home() -> Path:
    """Return the Copilot CLI configuration home, honoring ``COPILOT_HOME``."""
    from apm_cli.integration.targets import resolve_user_root

    return resolve_user_root("copilot")


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
