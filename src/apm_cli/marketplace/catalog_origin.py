"""Catalog-origin safety for machine-local marketplace aliases (#3912).

An alias such as ``company`` is a nickname on one machine. The lockfile records
the catalog's canonical origin (``source_url``), and these checks compare that
origin -- never the alias -- before an alias is trusted to resolve.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from typing import TYPE_CHECKING

from ..core.remediation import MARKETPLACE_DUPLICATE_ALIAS
from .errors import MarketplaceOriginConflictError
from .registry import get_marketplace_by_name
from .source_identity import (
    marketplace_origin,
    same_marketplace_catalog,
    same_marketplace_origin,
)

if TYPE_CHECKING:
    from ..deps.lockfile import LockedDependency, LockFile
    from .models import MarketplaceSource


def require_locked_catalog_origin(
    marketplace_name: str, plugin_name: str, lockfile: LockFile | None
) -> None:
    """Refuse to resolve through an alias that now names a different catalog.

    Every locked row recorded for this alias/plugin pair is checked
    (case-insensitively, as the registry resolves aliases), so neither an
    ambiguous association nor a recased alias can skip the check. A row
    without ``source_url`` (written before origins were recorded) is
    accepted, so older lockfiles keep working and gain the origin on their
    next lock write. Only the origin is compared: the lock does not record
    the catalog path, so a move to another path in the same repository is
    not detected (see :func:`same_marketplace_catalog`).

    Raises:
        MarketplaceOriginConflictError: The alias's registered origin differs
            from an origin the lockfile recorded for this plugin.
        MarketplaceNotFoundError: The alias is not registered.
    """
    if lockfile is None:
        return
    for dep in lockfile.marketplace_rows(marketplace_name, plugin_name):
        require_locked_row_origin(dep, marketplace_name=marketplace_name)


def require_locked_row_origin(locked: LockedDependency, *, marketplace_name: str = "") -> None:
    """Refuse one lock row whose alias now names a different catalog (#3959).

    For consumers that already hold the row, such as ``apm outdated``. A row
    without ``source_url`` or marketplace provenance is accepted.

    Raises:
        MarketplaceOriginConflictError: The alias's registered origin differs
            from the row's ``source_url``.
        MarketplaceNotFoundError: The alias is not registered.
    """
    alias = marketplace_name or locked.discovered_via or ""
    if not (locked.source_url and alias and locked.marketplace_plugin_name):
        return
    source = get_marketplace_by_name(alias)
    current = marketplace_origin(source)
    if not same_marketplace_origin(locked.source_url, current):
        raise MarketplaceOriginConflictError(
            alias,
            locked.marketplace_plugin_name,
            locked.source_url,
            current or source.display_source,
        )


def registrations_sharing_origin(
    source: MarketplaceSource, registered: Iterable[MarketplaceSource]
) -> list[MarketplaceSource]:
    """Return other aliases registered for the same catalog (origin and path)."""
    if not marketplace_origin(source):
        return []
    name = source.name.lower()
    return [
        existing
        for existing in registered
        if existing.name.lower() != name and same_marketplace_catalog(existing, source)
    ]


def duplicate_origin_warning(source: MarketplaceSource, existing: MarketplaceSource) -> str:
    """Render the advisory shown when a second alias names an existing catalog."""
    return (
        f"Marketplace '{source.name}' points at the same catalog as '{existing.name}' "
        f"({marketplace_origin(source)}). Both names resolve the same plugins; "
        "if this was unintended, "
        f"{MARKETPLACE_DUPLICATE_ALIAS.bind(name=shlex.quote(source.name)).clause()}."
    )
