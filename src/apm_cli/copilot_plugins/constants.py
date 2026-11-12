"""Stable vocabulary for APM-owned GitHub Copilot Agent Plugin registration."""

from __future__ import annotations

APM_MARKETPLACE_NAME = "apm"
"""Namespace APM owns inside Copilot's ``extraKnownMarketplaces``."""

APM_MARKETPLACE_OWNER = {"name": "APM", "email": "apm@users.noreply.github.com"}
"""Deterministic owner block written into the APM-owned catalog."""

MARKETPLACE_MANIFEST_RELATIVE = ".github/plugin/marketplace.json"
"""Catalog location relative to the APM marketplace root (``apm_modules``)."""

REGISTRATION_LEDGER_RELATIVE = ".github/plugin/apm-registration.json"
"""APM-owned ownership ledger, stored beside the generated catalog."""

PROJECT_SETTINGS_LOCAL_RELATIVE = ".github/copilot/settings.local.json"
"""Machine-local project activation surface preferred by APM."""

PROJECT_SETTINGS_SHARED_RELATIVE = ".github/copilot/settings.json"
"""Shared repository activation surface, adopted only on prior evidence."""

USER_SETTINGS_FILENAME = "settings.json"
"""Global activation surface, relative to the Copilot home directory."""

EXTRA_MARKETPLACES_KEY = "extraKnownMarketplaces"
ENABLED_PLUGINS_KEY = "enabledPlugins"

COPILOT_TARGET_NAME = "copilot"
"""Canonical target name that owns native Agent Plugin registration."""

REGISTRATION_LEDGER_VERSION = 1
