"""Ref pin cache for marketplace plugin immutability checks.

Records plugin-to-ref mappings per marketplace, keyed on the plugin's
declared ``version`` field from the standard marketplace spec.  When the
same ``(marketplace, plugin, version)`` triple resolves to a *different*
ref, a warning is emitted -- this may indicate a ref-swap attack where
an attacker changed the git ref for an existing version.

Legitimate version bumps (new ``version`` value) create new pin entries
and never trigger false-positive warnings.

The pin file lives at ``~/.apm/cache/marketplace/version-pins.json``
and has the structure::

    {
      "marketplace/plugin/1.0.0": "v1.0.0",
      "marketplace/plugin/2.0.0": "v2.0.0"
    }

All functions are **fail-open**: filesystem or JSON errors are logged
and never block resolution.
"""

import json
import logging
import os
from typing import Optional  # noqa: F401

logger = logging.getLogger(__name__)

_PINS_FILENAME = "version-pins.json"


# ------------------------------------------------------------------
# Path helpers
# ------------------------------------------------------------------


def _pins_path(pins_dir: str | None = None) -> str:
    """Return the full path to the version-pins JSON file.

    Args:
        pins_dir: Override directory for the pins file.  When ``None``,
            the default ``~/.apm/cache/marketplace/`` is used.
    """
    if pins_dir is not None:
        return os.path.join(pins_dir, _PINS_FILENAME)

    from ..config import CONFIG_DIR

    return os.path.join(CONFIG_DIR, "cache", "marketplace", _PINS_FILENAME)


def _pin_key(marketplace_name: str, plugin_name: str, version: str = "") -> str:
    """Build the canonical dict key for a marketplace/plugin/version triple.

    The *version* parameter corresponds to the plugin's declared ``version``
    field from the standard marketplace spec.  Including version in the key
    ensures that legitimate version bumps (new ``version`` value) create
    separate pin entries and never trigger false-positive ref-swap warnings.
    """
    base = f"{marketplace_name}/{plugin_name}".lower()
    if version:
        return f"{base}/{version}".lower()
    return base


# ------------------------------------------------------------------
# Load / save
# ------------------------------------------------------------------


def load_ref_pins(
    pins_dir: str | None = None,
    *,
    expect_exists: bool = False,
) -> dict:
    """Load the ref-pins file from disk.

    Returns an empty dict when the file is missing or contains invalid
    JSON.  Never raises.

    Args:
        pins_dir: Override directory for the pins file.
        expect_exists: When ``True`` and the file is missing, a warning
            is logged.  Use this when the caller previously wrote the
            file and its absence is unexpected (possible deletion).
    """
    path = _pins_path(pins_dir)
    if not os.path.exists(path):
        if expect_exists:
            logger.warning(
                "Version-pins file expected but missing: %s "
                "-- ref-swap detection is disabled until pins are rebuilt",
                path,
            )
        return {}
    try:
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.debug("version-pins file is not a JSON object; ignoring")
            return {}
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Failed to load version-pins: %s", exc)
        return {}


def save_ref_pins(pins: dict, pins_dir: str | None = None) -> None:
    """Persist *pins* to disk atomically.

    Writes to a temporary file first, then uses ``os.replace`` to move
    it into place so readers never see a partial write.  Errors are
    logged and swallowed (advisory system).
    """
    path = _pins_path(pins_dir)
    tmp_path = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp_path, "w") as fh:
            json.dump(pins, fh, indent=2)
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.debug("Failed to save version-pins: %s", exc)


# ------------------------------------------------------------------
# Check / record
# ------------------------------------------------------------------


def check_ref_pin(
    marketplace_name: str,
    plugin_name: str,
    ref: str,
    version: str = "",
    pins_dir: str | None = None,
) -> str | None:
    """Check whether *ref* matches the previously-recorded pin.

    The *version* parameter is the plugin's declared ``version`` field.
    When provided, pins are scoped per-version so that legitimate bumps
    (e.g. v2.0.0 -> v2.1.0) never trigger false-positive warnings.

    Returns:
        The **previously pinned ref** if it differs from *ref* (possible
        ref swap).  ``None`` if this is the first time seeing the
        plugin/version or the ref matches.
    """
    pins = load_ref_pins(pins_dir)
    key = _pin_key(marketplace_name, plugin_name, version)
    previous_ref = pins.get(key)
    if previous_ref is None:
        return None
    if not isinstance(previous_ref, str):
        return None
    if previous_ref == ref:
        return None
    return previous_ref


def record_ref_pin(
    marketplace_name: str,
    plugin_name: str,
    ref: str,
    version: str = "",
    pins_dir: str | None = None,
) -> None:
    """Store a plugin-to-ref mapping in the pin cache.

    The *version* parameter scopes the pin to a specific plugin version.
    Overwrites any existing pin for the same plugin/version (advisory
    system -- we always record the current ref even if it changed).
    """
    pins = load_ref_pins(pins_dir)
    key = _pin_key(marketplace_name, plugin_name, version)
    pins[key] = ref
    save_ref_pins(pins, pins_dir)
