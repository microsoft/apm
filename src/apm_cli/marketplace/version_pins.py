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
      "github.com/acme/catalog:marketplace.json#plugin/1.0.0": "v1.0.0",
      "github.com/acme/catalog:marketplace.json#plugin/2.0.0": "v2.0.0"
    }

A pin is scoped to the catalog it was read from
(:func:`~.source_identity.catalog_pin_identity`), never to the machine-local
alias, so an alias re-registered for another catalog starts without pins and
removing a marketplace keeps the pins of its catalog (#4966). Pins written by
an older APM are keyed ``alias/plugin/version``; they are read as belonging to
the catalog the alias's registration currently names and move to that catalog's key when the
alias is re-registered or removed, or when the pin is next recorded.

Reads fail closed: a pins file that exists but cannot be read or is not a
JSON object raises :class:`RefPinStoreError`, so ref-swap detection is
never silently disabled. A missing file starts an empty record. Writes are
best effort.

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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..core.remediation import MARKETPLACE_REF_SWAP_ACCEPT
from .errors import MarketplaceError

logger = logging.getLogger(__name__)

_PINS_FILENAME = "version-pins.json"


class RefPinStoreError(MarketplaceError):
    """The version-pins file exists but cannot be read as a JSON object."""

    def __init__(self, path: str, reason: str):
        super().__init__(
            f"Cannot read the marketplace version pins at {path}: {reason}. "
            "Marketplace installs need them to detect a changed plugin ref. "
            "Restore the file, or delete it to start a new record."
        )


class RefPinWriteError(RefPinStoreError):
    """The version-pins file could not be written; its recorded pins are kept."""

    def __init__(self, path: str, reason: str):
        MarketplaceError.__init__(
            self,
            f"Cannot write the marketplace version pins at {path} ({reason}): "
            "fix its permissions or free space and retry. Nothing was changed.",
        )


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


def _pin_key(marketplace_name: str, plugin_name: str, version: str = "", catalog: str = "") -> str:
    """Build the canonical dict key for a marketplace/plugin/version triple.

    With *catalog*, the key is scoped to that catalog identity; without it,
    the legacy alias-scoped key is returned.

    The *version* parameter corresponds to the plugin's declared ``version``
    field from the standard marketplace spec.  Including version in the key
    ensures that legitimate version bumps (new ``version`` value) create
    separate pin entries and never trigger false-positive ref-swap warnings.
    """
    # A catalog identity is kept verbatim: its path case can name another catalog.
    plugin = f"{plugin_name}/{version}" if version else plugin_name
    if catalog:
        return f"{catalog}#{plugin.lower()}"
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

    Returns an empty dict when the file is missing.

    Raises:
        RefPinStoreError: The file exists but cannot be read, is not valid
            JSON, or is not a JSON object.

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
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise RefPinStoreError(path, type(exc).__name__) from exc
    if not isinstance(data, dict):
        raise RefPinStoreError(path, "not a JSON object")
    return _normalized_keys(data)


def _normalized_keys(pins: dict) -> dict:
    """Move catalog pins of an older key form to the normal key; a normal key wins (#5169)."""
    from .source_identity import pin_identity_key

    def normal(key: str) -> str:
        catalog, sep, plugin = key.rpartition("#")
        return f"{pin_identity_key(catalog)}#{plugin}" if sep else key

    out = {key: ref for key, ref in pins.items() if normal(key) == key}
    for key, ref in pins.items():
        out.setdefault(normal(key), ref)
    return out


@contextmanager
def _pins_mutation(pins_dir: str | None, *, strict: bool = False) -> Iterator[None]:
    """Hold the cross-process pins lock around one load-modify-save (#5169).

    Both writers, the install commit and the registry's legacy-pin adoption,
    take it, so neither overwrites the other's write.
    """
    from apm_cli.config import user_state_writes_suppressed

    from ..cache.locking import shard_lock

    if user_state_writes_suppressed():
        yield  # a preview writes nothing, so there is nothing to guard
        return
    path = Path(_pins_path(pins_dir))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if strict:
            raise RefPinWriteError(str(path), type(exc).__name__) from exc
        yield  # the best-effort save fails the same way and writes nothing
        return
    with shard_lock(path):
        yield


def save_ref_pins(pins: dict, pins_dir: str | None = None, *, strict: bool = False) -> None:
    """Persist *pins* to disk atomically.

    Writes to a temporary file first, then uses ``os.replace`` to move
    it into place so readers never see a partial write.  Errors are
    logged and swallowed (advisory system), unless *strict*, which raises
    :class:`RefPinWriteError`. A preview writes nothing.
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
        if strict:
            raise RefPinWriteError(path, type(exc).__name__) from exc
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
    *,
    catalog: str = "",
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
    previous_ref = pins.get(_pin_key(marketplace_name, plugin_name, version, catalog))
    if previous_ref is None and catalog and _owns_legacy_pins(marketplace_name, catalog):
        # A legacy alias pin belongs to the catalog the alias's registration names.
        previous_ref = pins.get(_pin_key(marketplace_name, plugin_name, version))
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
    *,
    catalog: str = "",
) -> None:
    """Store a plugin-to-ref mapping in the pin cache.

    The *version* parameter scopes the pin to a specific plugin version.
    Overwrites any existing pin for the same plugin/version (advisory
    system -- we always record the current ref even if it changed).
    """
    with _pins_mutation(pins_dir):
        pins = load_ref_pins(pins_dir)
        _put(pins, marketplace_name, plugin_name, version, catalog, ref)
        save_ref_pins(pins, pins_dir)


def _owns_legacy_pins(marketplace_name: str, catalog: str) -> bool:
    """Whether *marketplace_name*'s persisted registration names *catalog*.

    Legacy pins are keyed by the alias of a registration; a project map that
    shadows the alias with another catalog never reads or consumes them.
    """
    from .registry import get_registered_marketplaces
    from .source_identity import catalog_pin_identity

    for source in get_registered_marketplaces():
        if source.name.lower() == marketplace_name.lower():
            try:
                return catalog_pin_identity(source) == catalog
            except ValueError:
                return False
    return False


def _put(pins: dict, marketplace: str, plugin: str, version: str, catalog: str, ref: str) -> None:
    pins[_pin_key(marketplace, plugin, version, catalog)] = ref
    if catalog and _owns_legacy_pins(marketplace, catalog):
        pins.pop(_pin_key(marketplace, plugin, version), None)


def adopt_alias_ref_pins(alias: str, catalog: str, pins_dir: str | None = None) -> None:
    """Move *alias*'s legacy pins to *catalog*, the catalog the alias names now.

    Called before an alias is re-registered or removed, so its pins stay with
    that catalog and are never read for another one (#4966). A pin already
    recorded for the catalog wins over a legacy one. A failed write raises
    :class:`RefPinWriteError`, so the registry change does not happen.
    """
    prefix = f"{alias.lower()}/"
    with _pins_mutation(pins_dir, strict=True):
        pins = load_ref_pins(pins_dir)
        # A catalog-scoped key always holds "#"; an alias (a hostname, say) can
        # prefix one, but only alias-scoped keys are legacy.
        legacy = [key for key in pins if "#" not in key and key.lower().startswith(prefix)]
        if not legacy:
            return
        for key in legacy:
            ref = pins.pop(key)
            pins.setdefault(f"{catalog}#{key[len(prefix) :].lower()}", ref)
        save_ref_pins(pins, pins_dir, strict=True)


# ------------------------------------------------------------------
# Install-attempt staging
# ------------------------------------------------------------------


class RefPinStage:
    """Ref pins resolved during one install attempt, written only on commit."""

    def __init__(self) -> None:
        self._pins: dict[tuple[str, str, str], tuple[str, str]] = {}
        self._lock = threading.Lock()

    def add(
        self,
        marketplace_name: str,
        plugin_name: str,
        ref: str,
        version: str = "",
        catalog: str = "",
    ) -> None:
        """Stage one plugin-to-ref mapping for :meth:`commit`."""
        with self._lock:
            self._pins[(marketplace_name, plugin_name, version)] = (ref, catalog)

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
        with _pins_mutation(pins_dir):
            pins = load_ref_pins(pins_dir)
            for (marketplace_name, plugin_name, version), (ref, catalog) in staged.items():
                _put(pins, marketplace_name, plugin_name, version, catalog, ref)
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
    catalog: str = "",
) -> None:
    """Stage a pin for the open install attempt; no attempt means no write."""
    with _stage_lock:
        stage = _active_stage
    if stage is not None:
        stage.add(marketplace_name, plugin_name, ref, version, catalog)


def enforce_ref_pin(
    marketplace_name: str,
    plugin_name: str,
    ref: str,
    version: str = "",
    pins_dir: str | None = None,
    *,
    accept_change: bool = False,
    catalog: str = "",
) -> str | None:
    """Refuse a ref that contradicts the recorded pin; otherwise stage it.

    A contradicting ref raises :class:`RefPinMismatchError` unless
    *accept_change* is set, which only an explicit ``apm update`` of that
    plugin does. A staged ref is written only when the install commits (#4400).

    Returns:
        The previously pinned ref when an accepted change replaces it, else ``None``.
    """
    previous_ref = check_ref_pin(
        marketplace_name, plugin_name, ref, version, pins_dir, catalog=catalog
    )
    if previous_ref is not None and not accept_change:
        key = _pin_key(marketplace_name, plugin_name, version, catalog)
        raise RefPinMismatchError(plugin_name, marketplace_name, previous_ref, ref, key)
    stage_ref_pin(marketplace_name, plugin_name, ref, version, catalog)
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
