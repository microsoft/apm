"""Native GitHub Copilot Agent Plugin registration owned by APM.

Public surface:

* :mod:`~apm_cli.copilot_plugins.capability` decides whether native
  registration is available for the running command;
* :mod:`~apm_cli.copilot_plugins.registrar` rebuilds the APM-owned catalog,
  settings entries, and ownership ledger from canonical resolved state.
"""

from __future__ import annotations

from .capability import (
    NativeRegistrationCapability,
    activate_native_registration,
    admits_native_plugin,
    current_native_registration,
    reset_native_registration,
    resolve_native_registration_capability,
)
from .catalog import NativePluginEntry, render_catalog
from .constants import (
    APM_MARKETPLACE_NAME,
    MARKETPLACE_MANIFEST_RELATIVE,
    REGISTRATION_LEDGER_RELATIVE,
)
from .registrar import (
    CopilotPluginSyncResult,
    ResolvedPluginCandidate,
    catalog_path_for,
    ledger_path_for,
    synchronize_copilot_plugins,
)
from .settings import CopilotSettingsCollisionError

__all__ = [
    "APM_MARKETPLACE_NAME",
    "MARKETPLACE_MANIFEST_RELATIVE",
    "REGISTRATION_LEDGER_RELATIVE",
    "CopilotPluginSyncResult",
    "CopilotSettingsCollisionError",
    "NativePluginEntry",
    "NativeRegistrationCapability",
    "ResolvedPluginCandidate",
    "activate_native_registration",
    "admits_native_plugin",
    "catalog_path_for",
    "current_native_registration",
    "ledger_path_for",
    "render_catalog",
    "reset_native_registration",
    "resolve_native_registration_capability",
    "synchronize_copilot_plugins",
]
