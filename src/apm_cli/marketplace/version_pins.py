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

Resolution only *stages* a pin (:func:`stage_ref_pin`).  The install
transaction owns one :class:`RefPinStage` per attempt and writes the
staged pins when the attempt commits; a dry run, a failed install or a
read-only command (``apm view``) writes nothing (#4212, #4194). A requested
package that validation rejects withdraws its pins, so a partly successful
install records only the packages it installs (#4322).
"""

import json
import logging
import os
import threading

from ..core.remediation import MARKETPLACE_REF_SWAP_ACCEPT
from .errors import MarketplaceError

logger = logging.getLogger(__name__)

_PINS_FILENAME = "version-pins.json"


class RefPinMismatchError(MarketplaceError):
    """A marketplace plugin version resolved to a ref other than its recorded pin."""

    def __init__(
        self, plugin: str, marketplace: str, previous_ref: str, current_ref: str, key: str
    ):
        super().__init__(
            f"Plugin {plugin}@{marketplace} ref changed: was '{previous_ref}', now "
            f"'{current_ref}'. This may indicate a ref swap attack; refusing to install. "
            f"{MARKETPLACE_REF_SWAP_ACCEPT.bind(key=key).sentence()}"
        )


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

    from ..core.scope import get_apm_home

    return os.path.join(os.fspath(get_apm_home()), "cache", "marketplace", _PINS_FILENAME)


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
    logged and swallowed (advisory system). A preview writes nothing.
    """
    from apm_cli.config import user_state_writes_suppressed

    if user_state_writes_suppressed():
        return
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


# ------------------------------------------------------------------
# Install-attempt staging
# ------------------------------------------------------------------


class RefPinStage:
    """Ref pins resolved during one install attempt, written only on commit."""

    def __init__(self) -> None:
        self._pins: dict[tuple[str, str, str], str] = {}
        self._lock = threading.Lock()

    def add(self, marketplace_name: str, plugin_name: str, ref: str, version: str = "") -> None:
        """Stage one plugin-to-ref mapping for :meth:`commit`."""
        with self._lock:
            self._pins[(marketplace_name, plugin_name, version)] = ref

    def keys(self) -> frozenset[tuple[str, str, str]]:
        """Return the ``(marketplace, plugin, version)`` keys staged so far."""
        with self._lock:
            return frozenset(self._pins)

    def drop(self, keys: frozenset[tuple[str, str, str]]) -> None:
        """Withdraw staged pins of a package that will not install (#4322)."""
        with self._lock:
            for key in keys:
                self._pins.pop(key, None)

    def commit(self, pins_dir: str | None = None) -> None:
        """Write every staged pin in one save and close this stage."""
        with self._lock:
            staged, self._pins = self._pins, {}
        self._close()
        if not staged:
            return
        pins = load_ref_pins(pins_dir)
        for (marketplace_name, plugin_name, version), ref in staged.items():
            pins[_pin_key(marketplace_name, plugin_name, version)] = ref
        save_ref_pins(pins, pins_dir)

    def discard(self) -> None:
        """Drop every staged pin and close this stage."""
        with self._lock:
            self._pins = {}
        self._close()

    def _close(self) -> None:
        global _active_stage
        with _stage_lock:
            if _active_stage is self:
                _active_stage = None


_stage_lock = threading.Lock()
_active_stage: RefPinStage | None = None


def open_ref_pin_stage() -> RefPinStage:
    """Open the stage that receives pins resolved by this install attempt."""
    global _active_stage
    stage = RefPinStage()
    with _stage_lock:
        _active_stage = stage
    return stage


def stage_ref_pin(
    marketplace_name: str,
    plugin_name: str,
    ref: str,
    version: str = "",
) -> None:
    """Stage a pin for the open install attempt; no attempt means no write."""
    with _stage_lock:
        stage = _active_stage
    if stage is not None:
        stage.add(marketplace_name, plugin_name, ref, version)


def enforce_ref_pin(
    marketplace_name: str,
    plugin_name: str,
    ref: str,
    version: str = "",
    pins_dir: str | None = None,
    *,
    accept_change: bool = False,
) -> str | None:
    """Refuse a ref that contradicts the recorded pin; otherwise stage it.

    A contradicting ref raises :class:`RefPinMismatchError` unless
    *accept_change* is set, which only an explicit ``apm update`` of that
    plugin does. A staged ref is written only when the install commits (#4400).

    Returns:
        The previously pinned ref when an accepted change replaces it, else ``None``.
    """
    previous_ref = check_ref_pin(marketplace_name, plugin_name, ref, version, pins_dir)
    if previous_ref is not None and not accept_change:
        key = _pin_key(marketplace_name, plugin_name, version)
        raise RefPinMismatchError(plugin_name, marketplace_name, previous_ref, ref, key)
    stage_ref_pin(marketplace_name, plugin_name, ref, version)
    return previous_ref


def staged_ref_pin_keys() -> frozenset[tuple[str, str, str]]:
    """Return the pin keys staged by the open install attempt (empty without one)."""
    with _stage_lock:
        stage = _active_stage
    return stage.keys() if stage is not None else frozenset()


def withdraw_ref_pins(keys: frozenset[tuple[str, str, str]]) -> None:
    """Withdraw staged pins of a requested package that validation rejected."""
    if not keys:
        return
    with _stage_lock:
        stage = _active_stage
    if stage is not None:
        stage.drop(keys)
