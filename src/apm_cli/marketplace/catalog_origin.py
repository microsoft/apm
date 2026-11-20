"""Catalog-origin safety for machine-local marketplace aliases (#3912).

An alias such as ``company`` is a nickname on one machine. The lockfile records
the catalog's canonical origin (``source_url``), and these checks compare that
origin -- never the alias -- before an alias is trusted to resolve.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from typing import TYPE_CHECKING

from .errors import MarketplaceError, MarketplaceOriginConflictError, MarketplaceProvenanceError
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
        f"if this was unintended, run: apm marketplace remove {shlex.quote(source.name)}"
    )


_PROVENANCE_FIELDS = ("discovered_via", "marketplace_plugin_name", "source_url", "source_digest")
_CATALOG_FIELDS = ("source_url", "source_digest")


def reverify_locked_provenance(lockfile: LockFile | None) -> list[str]:
    """Re-verify every locked marketplace row against its registered catalog (#4228).

    ``apm update`` calls this before planning, so recorded provenance is never
    carried forward on the lock's word alone. A row's alias and plugin name
    must both be recorded and still resolve; its origin must match the
    registered catalog (:func:`require_locked_row_origin`). The catalog's
    current ``source_url`` and ``source_digest`` then replace what the lock
    recorded, and each changed row is returned so the caller can report it.

    Raises:
        MarketplaceProvenanceError: A row records incomplete provenance, or
            its alias or plugin no longer resolves.
        MarketplaceOriginConflictError: The alias names a different catalog.
    """
    from .dependency_confusion import admit_marketplace_resolution
    from .resolver import resolve_marketplace_plugin

    restored: list[str] = []
    for dep in lockfile.dependencies.values() if lockfile is not None else ():
        if not any(getattr(dep, field) for field in _PROVENANCE_FIELDS):
            continue
        key = dep.get_unique_key()
        alias, plugin = dep.discovered_via, dep.marketplace_plugin_name
        if not (alias and plugin):
            raise MarketplaceProvenanceError(
                f"apm.lock.yaml records incomplete marketplace provenance for {key}: "
                "discovered_via and marketplace_plugin_name are both required. Nothing was "
                "changed. Reinstall it from its marketplace with "
                "'apm install <plugin>@<marketplace>' to record it again."
            )
        require_locked_row_origin(dep)
        try:
            resolution = resolve_marketplace_plugin(plugin, alias)
            admit_marketplace_resolution(resolution)
        except MarketplaceError as exc:
            raise MarketplaceProvenanceError(
                f"Cannot re-verify the marketplace provenance of {key} ({plugin}@{alias}): {exc}"
            ) from exc
        current = resolution.provenance(alias, plugin)
        changed = False
        for field in _CATALOG_FIELDS:
            value = current.get(field)
            if value and getattr(dep, field) != value:
                setattr(dep, field, value)
                changed = True
        if changed:
            restored.append(f"{key} ({plugin}@{alias})")
    return restored
