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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..core.remediation import (
    MARKETPLACE_REF_SWAP_ACCEPT,
    MARKETPLACE_REF_SWAP_ACCEPT_INSTALL,
    either_sentence,
)
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
            + either_sentence(
                MARKETPLACE_REF_SWAP_ACCEPT.bind(key=key), MARKETPLACE_REF_SWAP_ACCEPT_INSTALL
            )
        )


class RefPinConflictError(MarketplaceError):
    """Legacy ref pins from multiple aliases naming the same catalog conflict."""

    def __init__(
        self,
        plugin: str,
        catalog: str,
        conflicting_pins: dict[str, str],
        key: str,
        version: str = "",
    ):
        self.plugin = plugin
        self.catalog = catalog
        self.conflicting_pins = conflicting_pins
        self.key = key
        self.version = version
        details = ", ".join(
            f"'{alias}' pinned '{ref}'" for alias, ref in sorted(conflicting_pins.items())
        )
        plugin_desc = f"{plugin}/{version}" if version else plugin
        super().__init__(
            f"Plugin {plugin_desc} has conflicting legacy ref pins across aliases "
            f"naming catalog {catalog}: {details}. Refusing to resolve. "
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
def _pins_mutation(pins_dir: str | None, *, strict: bool = False) -> Iterator[bool]:
    """Hold the cross-process pins lock around one load-modify-save (#5169).

    Both writers, the install commit and the registry's legacy-pin adoption,
    take it, so neither overwrites the other's write. Yields whether the lock
    is held: an advisory writer skips its write when it is not, so it never
    fails a committed install; a *strict* writer raises
    :class:`RefPinWriteError` instead.
    """
    from apm_cli.config import user_state_writes_suppressed

    from ..cache.locking import shard_lock

    if user_state_writes_suppressed():
        yield False  # a preview writes nothing
        return
    path = Path(_pins_path(pins_dir))
    lock = shard_lock(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock.acquire()
    except OSError as exc:  # filelock's Timeout is an OSError too
        if strict:
            raise RefPinWriteError(str(path), type(exc).__name__) from exc
        logger.debug("Skipped a version-pins write: the pins lock is unavailable: %s", exc)
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


def save_ref_pins(pins: dict, pins_dir: str | None = None, *, strict: bool = False) -> bool:
    """Persist *pins* to disk atomically.

    Writes to a temporary file first, then uses ``os.replace`` to move
    it into place so readers never see a partial write.  Errors are
    logged and swallowed (advisory system), unless *strict*, which raises
    :class:`RefPinWriteError`. A preview writes nothing. Returns whether the
    pins were written.
    """
    from apm_cli.config import user_state_writes_suppressed

    if user_state_writes_suppressed():
        return False
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
        return False
    return True


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
    key = _pin_key(marketplace_name, plugin_name, version, catalog)
    previous_ref = pins.get(key)
    if previous_ref is None and catalog:
        aliases = _aliases_naming_catalog(catalog)
        legacy_pins: dict[str, str] = {}
        for alias in aliases:
            legacy_key = _pin_key(alias, plugin_name, version)
            val = pins.get(legacy_key)
            if isinstance(val, str):
                legacy_pins[alias] = val
        if legacy_pins:
            unique_refs = set(legacy_pins.values())
            if len(unique_refs) > 1:
                raise RefPinConflictError(plugin_name, catalog, legacy_pins, key, version=version)
            previous_ref = next(iter(unique_refs))
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
    with _pins_mutation(pins_dir) as held:
        if not held:
            return
        pins = load_ref_pins(pins_dir)
        _put(pins, marketplace_name, plugin_name, version, catalog, ref)
        save_ref_pins(pins, pins_dir)


def _aliases_naming_catalog(catalog: str) -> list[str]:
    """Return all registered marketplace aliases that name *catalog*."""
    from .registry import get_registered_marketplaces
    from .source_identity import catalog_pin_identity

    seen: set[str] = set()
    aliases: list[str] = []
    for source in get_registered_marketplaces():
        try:
            if catalog_pin_identity(source) == catalog:
                name = source.name
                if name.lower() not in seen:
                    seen.add(name.lower())
                    aliases.append(name)
        except ValueError:
            continue
    return aliases


def _owns_legacy_pins(marketplace_name: str, catalog: str) -> bool:
    """Whether *marketplace_name*'s persisted registration names *catalog*.

    Legacy pins are keyed by the alias of a registration; a project map that
    shadows the alias with another catalog never reads or consumes them.
    """
    return any(
        alias.lower() == marketplace_name.lower() for alias in _aliases_naming_catalog(catalog)
    )


def _put(pins: dict, marketplace: str, plugin: str, version: str, catalog: str, ref: str) -> None:
    if not ref:
        return
    pins[_pin_key(marketplace, plugin, version, catalog)] = ref
    if catalog:
        for alias in _aliases_naming_catalog(catalog):
            pins.pop(_pin_key(alias, plugin, version), None)
        if _owns_legacy_pins(marketplace, catalog):
            pins.pop(_pin_key(marketplace, plugin, version), None)


def adopt_alias_ref_pins(alias: str, catalog: str, pins_dir: str | None = None) -> None:
    """Move *alias*'s legacy pins to *catalog*, the catalog the alias names now.

    Called before an alias is re-registered or removed, so its pins stay with
    that catalog and are never read for another one (#4966). A pin already
    recorded for the catalog wins over a legacy one. A failed write raises
    :class:`RefPinWriteError`, so the registry change does not happen.
    """
    prefix = f"{alias.lower()}/"
    with _pins_mutation(pins_dir, strict=True) as held:
        if not held:
            return  # a preview writes nothing
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


@dataclass(frozen=True)
class StagedPinRepair:
    """Details of a staged marketplace ref-pin repair for display and summary."""

    marketplace_name: str
    plugin_name: str
    version: str
    new_ref: str
    previous_ref: str | None = None
    conflicting_pins: tuple[tuple[str, str], ...] = ()


class RefPinStage:
    """Ref pins resolved during one install attempt, written only on commit."""

    def __init__(self, *, accept_changes: bool = False) -> None:
        self._pins: dict[tuple[str, str, str], tuple[str, str]] = {}
        self._repairs: dict[tuple[str, str, str], StagedPinRepair] = {}
        self._same_commits: dict[tuple[str, str, str], str] = {}
        self._lock = threading.Lock()
        # ``apm install --accept-ref-change``: this attempt accepts changed refs.
        self.accept_changes = accept_changes

    def same_commit(self, refs: tuple[str, str, str]) -> str | None:
        """Return the commit a ``(key, recorded, current)`` pair was already matched to."""
        with self._lock:
            return self._same_commits.get(refs)

    def remember_same_commit(self, refs: tuple[str, str, str], commit: str) -> None:
        """Record that ``(key, recorded, current)`` select *commit* in this attempt."""
        with self._lock:
            self._same_commits[refs] = commit

    def add(
        self,
        marketplace_name: str,
        plugin_name: str,
        ref: str,
        version: str = "",
        catalog: str = "",
        repair: bool = False,
        previous_ref: str | None = None,
        conflicting_pins: dict[str, str] | None = None,
    ) -> None:
        """Stage one plugin-to-ref mapping for :meth:`commit`."""
        with self._lock:
            key = (marketplace_name, plugin_name, version)
            self._pins[key] = (ref, catalog)
            if repair:
                self._repairs[key] = StagedPinRepair(
                    marketplace_name=marketplace_name,
                    plugin_name=plugin_name,
                    version=version,
                    new_ref=ref,
                    previous_ref=previous_ref,
                    conflicting_pins=tuple(sorted((conflicting_pins or {}).items())),
                )

    def keys(self) -> frozenset[tuple[str, str, str]]:
        """Return the ``(marketplace, plugin, version)`` keys staged so far."""
        with self._lock:
            return frozenset(self._pins)

    def repairs(self) -> frozenset[tuple[str, str, str]]:
        """Return keys of pins that repair a conflict or mismatch."""
        with self._lock:
            return frozenset(self._repairs)

    def repair_details(self) -> tuple[StagedPinRepair, ...]:
        """Return details of pins staged as repairs."""
        with self._lock:
            return tuple(self._repairs.values())

    def drop(self, keys: frozenset[tuple[str, str, str]]) -> None:
        """Withdraw staged pins of a package that will not install (#4322)."""
        with self._lock:
            for key in keys:
                self._pins.pop(key, None)
                self._repairs.pop(key, None)

    def commit(self, pins_dir: str | None = None) -> None:
        """Write every staged pin in one save and close this stage.

        Records which staged repairs the save actually persisted, so a
        skipped or failed advisory write is never reported as a repair.
        """
        global _committed_repairs
        with self._lock:
            staged, self._pins = self._pins, {}
            repairs = tuple(self._repairs.values())
            self._repairs.clear()
        self._close()
        _committed_repairs = ()
        if not staged:
            return
        with _pins_mutation(pins_dir) as held:
            if not held:
                return
            pins = load_ref_pins(pins_dir)
            for (marketplace_name, plugin_name, version), (ref, catalog) in staged.items():
                _put(pins, marketplace_name, plugin_name, version, catalog, ref)
            if save_ref_pins(pins, pins_dir):
                _committed_repairs = repairs

    def discard(self) -> None:
        """Drop every staged pin and close this stage."""
        with self._lock:
            self._pins = {}
            self._repairs.clear()
        self._close()

    def _close(self) -> None:
        global _active_stage
        with _stage_lock:
            if _active_stage is self:
                _active_stage = None


_stage_lock = threading.Lock()
_active_stage: RefPinStage | None = None


# Repairs the last committed stage actually wrote (#5243).
_committed_repairs: tuple[StagedPinRepair, ...] = ()


def committed_ref_pin_repairs() -> tuple[StagedPinRepair, ...]:
    """Return the staged repairs the last commit persisted to the pins file."""
    return _committed_repairs


def open_ref_pin_stage(*, accept_changes: bool = False) -> RefPinStage:
    """Open the stage that receives pins resolved by this install attempt.

    With *accept_changes* (``apm install --accept-ref-change``), a ref that
    contradicts its recorded pin is accepted and recorded on commit.
    """
    global _active_stage, _committed_repairs
    _committed_repairs = ()
    stage = RefPinStage(accept_changes=accept_changes)
    with _stage_lock:
        _active_stage = stage
    return stage


def stage_ref_pin(
    marketplace_name: str,
    plugin_name: str,
    ref: str,
    version: str = "",
    catalog: str = "",
    repair: bool = False,
    previous_ref: str | None = None,
    conflicting_pins: dict[str, str] | None = None,
) -> None:
    """Stage a pin for the open install attempt; no attempt means no write."""
    with _stage_lock:
        stage = _active_stage
    if stage is not None:
        stage.add(
            marketplace_name,
            plugin_name,
            ref,
            version,
            catalog,
            repair=repair,
            previous_ref=previous_ref,
            conflicting_pins=conflicting_pins,
        )


def _same_commit(
    previous_ref: str, ref: str, commit_of: Callable[[str], str | None] | None
) -> str | None:
    """Return the commit SHA *ref* names when the recorded ref selects it now, else ``None``.

    Only a full commit SHA replaces a recorded ref: the download then fetches
    exactly the commit checked here. A tag replacing a recorded SHA could
    resolve differently at download time, so it is refused. ``None`` keeps
    the refusal: a ref with no fixed commit, a lookup that fails (offline,
    unreachable, no credentials) or two different commits.
    """
    from ..utils.github_host import is_full_commit_sha

    if commit_of is None or not is_full_commit_sha(ref):
        return None
    try:
        recorded, current = commit_of(previous_ref), commit_of(ref)
    except Exception as exc:
        logger.debug("Could not resolve marketplace ref %r to a commit: %s", previous_ref, exc)
        return None
    return recorded if recorded and recorded == current else None


def _migrate_same_commit_pin(
    marketplace_name: str,
    plugin_name: str,
    previous_ref: str,
    ref: str,
    key: str,
    commit_of: Callable[[str], str | None] | None,
) -> str | None:
    """Return the commit a recorded ref and *ref* share, announcing a moved pin once (#5526)."""
    with _stage_lock:
        stage = _active_stage
    refs = (key, previous_ref, ref)
    commit = stage.same_commit(refs) if stage is not None else None
    if commit is not None:
        return commit
    commit = _same_commit(previous_ref, ref, commit_of)
    if commit is None:
        return None
    if stage is not None:
        stage.remember_same_commit(refs, commit)
    from ..utils.console import _rich_info

    _rich_info(
        f"{plugin_name}@{marketplace_name}: pin moved from tag '{previous_ref}' to commit "
        f"{commit[:7]} (same code), recorded.",
        symbol="info",
    )
    return commit


def enforce_ref_pin(
    marketplace_name: str,
    plugin_name: str,
    ref: str,
    version: str = "",
    pins_dir: str | None = None,
    *,
    accept_change: bool = False,
    catalog: str = "",
    commit_of: Callable[[str], str | None] | None = None,
) -> str | None:
    """Refuse a ref that contradicts the recorded pin; otherwise stage it.

    A contradicting ref raises :class:`RefPinMismatchError` unless
    *accept_change* is set (an explicit ``apm update`` of that plugin) or the
    install attempt accepts changes (``apm install --accept-ref-change``).
    When *ref* is a full commit SHA and *commit_of* shows the recorded ref
    selects that commit now (a tag peeled to the SHA a catalog adds), the pin
    moves to the SHA instead (#5526); a commit that cannot be determined
    still refuses.
    A staged ref is written only when the install commits (#4400).

    Returns:
        The previously pinned ref when an accepted change replaces it, else ``None``.
    """
    with _stage_lock:
        stage = _active_stage
    by_install_flag = not accept_change and stage is not None and stage.accept_changes
    accept_change = accept_change or by_install_flag
    repair = False
    conflicting_pins: dict[str, str] | None = None
    try:
        previous_ref = check_ref_pin(
            marketplace_name, plugin_name, ref, version, pins_dir, catalog=catalog
        )
    except RefPinConflictError as exc:
        if not accept_change:
            raise
        previous_ref = None
        conflicting_pins = exc.conflicting_pins
        repair = True
    if previous_ref is not None and not accept_change:
        key = _pin_key(marketplace_name, plugin_name, version, catalog)
        commit = _migrate_same_commit_pin(
            marketplace_name, plugin_name, previous_ref, ref, key, commit_of
        )
        if commit is None:
            raise RefPinMismatchError(plugin_name, marketplace_name, previous_ref, ref, key)
        stage_ref_pin(marketplace_name, plugin_name, ref, version, catalog)
        return None
    if previous_ref is not None and accept_change:
        repair = True
    stage_ref_pin(
        marketplace_name,
        plugin_name,
        ref,
        version,
        catalog,
        repair=repair,
        previous_ref=previous_ref,
        conflicting_pins=conflicting_pins,
    )
    if conflicting_pins and by_install_flag:
        # ``apm update`` lists repaired pins in its plan; an install names them here.
        return ", ".join(f"{alias}={pinned}" for alias, pinned in sorted(conflicting_pins.items()))
    return previous_ref


def staged_ref_pin_keys() -> frozenset[tuple[str, str, str]]:
    """Return the pin keys staged by the open install attempt (empty without one)."""
    with _stage_lock:
        stage = _active_stage
    return stage.keys() if stage is not None else frozenset()


def staged_ref_pin_repairs() -> frozenset[tuple[str, str, str]]:
    """Return the pin keys staged as repairs by the open install attempt."""
    with _stage_lock:
        stage = _active_stage
    return stage.repairs() if stage is not None else frozenset()


def staged_ref_pin_repair_details() -> tuple[StagedPinRepair, ...]:
    """Return details of pin repairs staged by the open install attempt."""
    with _stage_lock:
        stage = _active_stage
    return stage.repair_details() if stage is not None else ()


def has_pending_pin_repairs() -> bool:
    """Whether the open install attempt has staged pin repairs."""
    return bool(staged_ref_pin_repairs())


def withdraw_ref_pins(keys: frozenset[tuple[str, str, str]]) -> None:
    """Withdraw staged pins of a requested package that validation rejected."""
    if not keys:
        return
    with _stage_lock:
        stage = _active_stage
    if stage is not None:
        stage.drop(keys)
