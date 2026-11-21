"""Native GitHub Copilot Agent Plugin admission owned by APM.

Public surface:

* :mod:`~apm_cli.copilot_plugins.capability` decides whether native
  activation for Copilot is available for the running command;
* :mod:`~apm_cli.copilot_plugins.registrar` shares the locked-state trust
  gate and migrates the registration earlier releases kept in
  ``apm_modules``. Declaration itself belongs to the shared owner,
  :func:`apm_cli.claude_plugins.registrar.reconcile_native_plugins`.
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
from .constants import (
    APM_MARKETPLACE_NAME,
    MARKETPLACE_MANIFEST_RELATIVE,
    REGISTRATION_LEDGER_RELATIVE,
)
from .registrar import ResolvedPluginCandidate, plan_legacy_copilot_retirement
from .settings import CopilotSettingsCollisionError

__all__ = [
    "APM_MARKETPLACE_NAME",
    "MARKETPLACE_MANIFEST_RELATIVE",
    "REGISTRATION_LEDGER_RELATIVE",
    "CopilotSettingsCollisionError",
    "NativeRegistrationCapability",
    "ResolvedPluginCandidate",
    "activate_native_registration",
    "admits_native_plugin",
    "current_native_registration",
    "plan_legacy_copilot_retirement",
    "reset_native_registration",
    "resolve_native_registration_capability",
]
