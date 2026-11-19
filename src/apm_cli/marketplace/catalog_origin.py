"""Catalog-origin safety for machine-local marketplace aliases (#3912).

An alias such as ``company`` is a nickname on one machine. The lockfile records
the catalog's canonical origin (``source_url``), and these checks compare that
origin -- never the alias -- before an alias is trusted to resolve.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from .errors import MarketplaceOriginConflictError
from .registry import get_marketplace_by_name
from .source_identity import marketplace_origin, same_marketplace_origin

if TYPE_CHECKING:
    from ..deps.lockfile import LockFile
    from .models import MarketplaceSource


def require_locked_catalog_origin(
    marketplace_name: str, plugin_name: str, lockfile: LockFile | None
) -> None:
    """Refuse to resolve through an alias that now names a different catalog.

    A lock row without ``source_url`` (written before origins were recorded)
    is accepted, so older lockfiles keep working and gain the origin on their
    next resolution.

    Raises:
        MarketplaceOriginConflictError: The alias's registered origin differs
            from the origin the lockfile recorded for this plugin.
        MarketplaceNotFoundError: The alias is not registered.
    """
    if lockfile is None:
        return
    locked = lockfile.get_dependency_by_marketplace(marketplace_name, plugin_name)
    if locked is None or not locked.source_url:
        return
    source = get_marketplace_by_name(marketplace_name)
    current = marketplace_origin(source)
    if same_marketplace_origin(locked.source_url, current):
        return
    raise MarketplaceOriginConflictError(
        marketplace_name, plugin_name, locked.source_url, current or source.display_source
    )


def registrations_sharing_origin(
    source: MarketplaceSource, registered: Iterable[MarketplaceSource]
) -> list[MarketplaceSource]:
    """Return other aliases registered for the same catalog (origin and path)."""
    origin = marketplace_origin(source)
    if not origin:
        return []
    name = source.name.lower()
    return [
        existing
        for existing in registered
        if existing.name.lower() != name
        and existing.path == source.path
        and same_marketplace_origin(marketplace_origin(existing), origin)
    ]


def duplicate_origin_warning(source: MarketplaceSource, existing: MarketplaceSource) -> str:
    """Render the advisory shown when a second alias names an existing catalog."""
    return (
        f"Marketplace '{source.name}' points at the same catalog as '{existing.name}' "
        f"({marketplace_origin(source)}). Both names resolve the same plugins; "
        f"if this was unintended, run: apm marketplace remove {source.name}"
    )
