"""Vocabulary of GitHub Copilot native plugins, including the legacy registration.

The ``apm`` marketplace, its catalog under ``apm_modules`` and the
``apm-registration.json`` ledger are what earlier releases wrote; they are
named here only so the migration can find and remove them (#3921).
"""

from __future__ import annotations

APM_MARKETPLACE_NAME = "apm"
"""Legacy marketplace name earlier releases declared, pointing at ``apm_modules``."""

MARKETPLACE_MANIFEST_RELATIVE = ".github/plugin/marketplace.json"
"""Legacy catalog location relative to ``apm_modules``."""

REGISTRATION_LEDGER_RELATIVE = ".github/plugin/apm-registration.json"
"""Legacy ownership ledger beside the legacy catalog."""

PROJECT_SETTINGS_LOCAL_RELATIVE = ".github/copilot/settings.local.json"
"""Machine-local project settings the legacy route wrote by default."""

PROJECT_SETTINGS_SHARED_RELATIVE = ".github/copilot/settings.json"
"""Committed project settings: the legacy route's alternative, and today's file."""

USER_SETTINGS_FILENAME = "settings.json"
"""User settings file, relative to the Copilot home directory."""

COPILOT_TARGET_NAME = "copilot"
"""Canonical target name that owns native Agent Plugin registration."""
