"""Manage registered marketplaces in ``~/.apm/marketplaces.json``."""

import json
import logging
import os
import threading
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path

from .errors import ManualMarketplaceConflictError, MarketplaceNotFoundError
from .models import ORIGIN_MANIFEST, PERSISTED_ORIGINS, MarketplaceSource

logger = logging.getLogger(__name__)

_MARKETPLACES_FILENAME = "marketplaces.json"

# Process-lifetime cache --------------------------------------------------
_registry_cache: list[MarketplaceSource] | None = None
_registry_lock = threading.Lock()
_mutation_lock = threading.Lock()


def _marketplaces_path() -> str:
    """Return the full path to ``~/.apm/marketplaces.json``."""
    from ..config import CONFIG_DIR

    return os.path.join(CONFIG_DIR, _MARKETPLACES_FILENAME)


def _invalidate_cache() -> None:
    global _registry_cache
    with _registry_lock:
        _registry_cache = None


@contextmanager
def _marketplace_mutation():
    """Lock the complete cross-process load-modify-save transaction."""
    from ..cache.locking import shard_lock
    from ..config import CONFIG_DIR

    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    path = Path(_marketplaces_path())
    with _mutation_lock, shard_lock(path):
        _invalidate_cache()
        yield


def _load() -> list[MarketplaceSource]:
    """Load registered marketplaces from disk (cached per-process).

    A read never writes: a missing registry reads as empty and no file or
    directory is created (#4600). Only ``_save`` creates the registry.
    """
    global _registry_cache
    with _registry_lock:
        if _registry_cache is not None:
            return list(_registry_cache)
        path = _marketplaces_path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read %s: %s", path, exc)
            data = {"marketplaces": []}
        sources: list[MarketplaceSource] = []
        for entry in data.get("marketplaces", []):
            try:
                sources.append(MarketplaceSource.from_dict(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.debug("Skipping invalid marketplace entry: %s", exc)
        _registry_cache = sources
        return list(sources)


def _save(sources: list[MarketplaceSource]) -> None:
    """Write marketplace list to disk atomically."""
    global _registry_cache
    # A root project's ``marketplaces:`` map is never registered (#2903 D3).
    if any(s.origin not in PERSISTED_ORIGINS for s in sources):
        raise ValueError("Project-declared marketplaces are never written to the registry")
    _adopt_ref_pins(_leaving(_load(), sources))
    from ..config import CONFIG_DIR

    # Only the directory: config.json is written by 'apm config set' alone.
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    path = _marketplaces_path()
    data = {"marketplaces": [s.to_dict() for s in sources]}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    with _registry_lock:
        _registry_cache = list(sources)


# Public API ---------------------------------------------------------------


def get_registered_marketplaces() -> list[MarketplaceSource]:
    """Return all registered marketplaces; a missing registry reads as empty."""
    return _load()


def get_marketplace_by_name(name: str) -> MarketplaceSource:
    """Return a marketplace by display name (case-insensitive).

    Inside a project whose root ``apm.yml`` declares ``marketplaces:``, a
    declared name wins over a registration with the same name (#2903 D3).

    Raises:
        MarketplaceNotFoundError: If not found.
    """
    from .project_marketplaces import project_marketplace

    declared = project_marketplace(name)
    if declared is not None:
        return declared
    lower = name.lower()
    for src in _load():
        if src.name.lower() == lower:
            return src
    from ..utils.github_host import default_host

    raise MarketplaceNotFoundError(name, host=default_host())


def _leaving(
    before: Sequence[MarketplaceSource], after: Sequence[MarketplaceSource]
) -> list[MarketplaceSource]:
    """Registrations whose name is removed or now names another catalog."""
    from .source_identity import catalog_pin_identity

    def identity(source: MarketplaceSource) -> str | None:
        try:
            return catalog_pin_identity(source)
        except ValueError:
            return None

    kept = {(s.name.lower(), identity(s)) for s in after}
    return [s for s in before if (s.name.lower(), identity(s)) not in kept]


def _adopt_ref_pins(leaving: Sequence[MarketplaceSource]) -> None:
    """Move legacy alias pins to the catalog each leaving registration names (#4966)."""
    if not leaving:
        return
    from .source_identity import catalog_pin_identity
    from .version_pins import adopt_alias_ref_pins

    for source in leaving:
        try:
            catalog = catalog_pin_identity(source)
        except ValueError:
            continue
        adopt_alias_ref_pins(source.name, catalog)


def add_marketplace(source: MarketplaceSource) -> None:
    """Register a marketplace (replaces if same name exists)."""
    with _marketplace_mutation():
        sources = [s for s in _load() if s.name.lower() != source.name.lower()]
        sources.append(source)
        _save(sources)
    logger.debug("Registered marketplace '%s'", source.name)


def remove_marketplace(name: str) -> None:
    """Remove a marketplace by name.

    Raises:
        MarketplaceNotFoundError: If not found.
    """
    with _marketplace_mutation():
        before = _load()
        after = [s for s in before if s.name.lower() != name.lower()]
        if len(after) == len(before):
            from ..utils.github_host import default_host

            raise MarketplaceNotFoundError(name, host=default_host())
        _save(after)
    logger.debug("Removed marketplace '%s'", name)


def apply_manifest_marketplaces(
    upserts: Sequence[MarketplaceSource], removals: Sequence[str]
) -> tuple[list[str], list[str], list[str]]:
    """Add, replace and remove manifest-origin registrations in one transaction.

    The ownership checks and the single write happen under one lock, so a
    manual registration is never replaced or removed: an upsert whose name a
    manual entry owns is reported as a conflict, and a removal only drops a
    manifest-origin entry.

    Returns:
        ``(applied, conflicts, removed)`` display names.
    """
    if any(source.origin != ORIGIN_MANIFEST for source in upserts):
        raise ValueError("apply_manifest_marketplaces requires manifest-origin sources")
    applied: list[str] = []
    conflicts: list[str] = []
    removed: list[str] = []
    with _marketplace_mutation():
        sources = _load()
        for source in upserts:
            lower = source.name.lower()
            owner = next((s for s in sources if s.name.lower() == lower), None)
            if owner is not None and owner.origin != ORIGIN_MANIFEST:
                conflicts.append(owner.name)
                continue
            sources = [s for s in sources if s.name.lower() != lower]
            sources.append(source)
            applied.append(source.name)
        for name in removals:
            lower = name.lower()
            kept = [
                s for s in sources if not (s.name.lower() == lower and s.origin == ORIGIN_MANIFEST)
            ]
            if len(kept) != len(sources):
                removed.append(name)
                sources = kept
        if applied or removed:
            _save(sources)
    logger.debug("Manifest marketplaces: applied=%s removed=%s", applied, removed)
    return applied, conflicts, removed


def register_manifest_marketplace(source: MarketplaceSource) -> None:
    """Add or replace one manifest-origin registration.

    Raises:
        ManualMarketplaceConflictError: A manual registration owns the name.
    """
    if source.origin != ORIGIN_MANIFEST:
        raise ValueError("register_manifest_marketplace requires a manifest-origin source")
    _applied, conflicts, _removed = apply_manifest_marketplaces([source], [])
    if conflicts:
        raise ManualMarketplaceConflictError(conflicts[0])


def unregister_manifest_marketplace(name: str) -> bool:
    """Remove a manifest-origin registration; manual entries are never removed.

    Returns True when an entry was removed.
    """
    return bool(apply_manifest_marketplaces([], [name])[2])


def marketplace_names() -> list[str]:
    """Return sorted list of registered marketplace names."""
    return sorted(s.name for s in _load())


def marketplace_count() -> int:
    """Return the number of registered marketplaces."""
    return len(_load())
