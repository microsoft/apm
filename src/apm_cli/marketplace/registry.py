"""Manage registered marketplaces in ``~/.apm/marketplaces.json``."""

import json
import logging
import os
import threading
from typing import Dict, List, Optional

from .errors import MarketplaceNotFoundError
from .models import MarketplaceSource

logger = logging.getLogger(__name__)

_MARKETPLACES_FILENAME = "marketplaces.json"

# Process-lifetime cache --------------------------------------------------
_registry_cache: Optional[List[MarketplaceSource]] = None
_registry_lock = threading.Lock()


def _marketplaces_path() -> str:
    """Return the full path to ``~/.apm/marketplaces.json``."""
    from ..config import CONFIG_DIR

    return os.path.join(CONFIG_DIR, _MARKETPLACES_FILENAME)


def _ensure_file() -> str:
    """Ensure the marketplaces file exists, creating it if needed."""
    from ..config import ensure_config_exists

    ensure_config_exists()
    path = _marketplaces_path()
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump({"marketplaces": []}, f, indent=2)
    return path


def _invalidate_cache() -> None:
    global _registry_cache
    with _registry_lock:
        _registry_cache = None


def _load() -> List[MarketplaceSource]:
    """Load registered marketplaces from disk (cached per-process)."""
    global _registry_cache
    with _registry_lock:
        if _registry_cache is not None:
            return list(_registry_cache)
        path = _ensure_file()
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read %s: %s", path, exc)
            data = {"marketplaces": []}
        sources: List[MarketplaceSource] = []
        for entry in data.get("marketplaces", []):
            try:
                sources.append(MarketplaceSource.from_dict(entry))
            except (KeyError, TypeError) as exc:
                logger.debug("Skipping invalid marketplace entry: %s", exc)
        _registry_cache = sources
        return list(sources)


def _save(sources: List[MarketplaceSource]) -> None:
    """Write marketplace list to disk atomically."""
    global _registry_cache
    path = _ensure_file()
    data = {"marketplaces": [s.to_dict() for s in sources]}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    with _registry_lock:
        _registry_cache = list(sources)


# Public API ---------------------------------------------------------------


def get_registered_marketplaces() -> List[MarketplaceSource]:
    """Return all registered marketplaces."""
    return _load()


def get_marketplace_by_name(name: str) -> MarketplaceSource:
    """Return a marketplace by display name (case-insensitive).

    Raises:
        MarketplaceNotFoundError: If not found.
    """
    lower = name.lower()
    for src in _load():
        if src.name.lower() == lower:
            return src
    raise MarketplaceNotFoundError(name)


def add_marketplace(source: MarketplaceSource) -> None:
    """Register a marketplace (replaces if same name exists)."""
    sources = [s for s in _load() if s.name.lower() != source.name.lower()]
    sources.append(source)
    _save(sources)
    logger.debug("Registered marketplace '%s'", source.name)


def remove_marketplace(name: str) -> None:
    """Remove a marketplace by name.

    Raises:
        MarketplaceNotFoundError: If not found.
    """
    before = _load()
    after = [s for s in before if s.name.lower() != name.lower()]
    if len(after) == len(before):
        raise MarketplaceNotFoundError(name)
    _save(after)
    logger.debug("Removed marketplace '%s'", name)


def marketplace_names() -> List[str]:
    """Return sorted list of registered marketplace names."""
    return sorted(s.name for s in _load())


def marketplace_count() -> int:
    """Return the number of registered marketplaces."""
    return len(_load())
