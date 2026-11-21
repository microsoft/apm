"""Owner of the root project's ``marketplaces:`` map (#2903 D1/D3).

A root project ``apm.yml`` may declare the marketplaces its ``marketplace:
<name>`` entries resolve through, in the user-manifest shape (``name``,
``source``, optional ``ref``)::

    marketplaces:
      - name: contoso
        source: https://github.com/contoso/agent-marketplace
    dependencies:
      apm:
        - { name: security-review, marketplace: contoso }

While a command binds the map (:func:`bind_project_marketplaces`):

* :func:`~apm_cli.marketplace.registry.get_marketplace_by_name` resolves a
  declared name here first, so a cold clone or CI installs with no
  ``apm marketplace add``, and a same-named registration is ignored with one
  ``[i]`` line.
* The map lives in memory only: it is never written to the user's
  marketplace registry, and its catalogs are cached by catalog identity,
  never by name.
* A transitive ``marketplace: <name>`` must name a declared marketplace
  (:func:`require_root_declared_marketplace`).

Only the root project's map counts: the resolver refuses a dependency's
``marketplaces:`` (see :func:`~apm_cli.marketplace.user_manifest.
dependency_marketplaces_error`).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import MarketplaceError, MarketplaceFetchError, UndeclaredMarketplaceError
from .models import ORIGIN_PROJECT, MarketplaceSource
from .source_identity import marketplace_origin, same_marketplace_origin

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from .user_manifest import DeclaredMarketplace

_FIX_HINT = "Check the 'marketplaces:' entry in apm.yml, then retry."


@dataclass
class _ProjectMarketplaces:
    declared: dict[str, DeclaredMarketplace]
    notice: Callable[[str], None] | None
    resolved: dict[str, MarketplaceSource] = field(default_factory=dict)
    origin_changes_noted: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)


_active: _ProjectMarketplaces | None = None
_ACTIVE_LOCK = threading.Lock()


def _default_notice(message: str) -> None:
    from ..utils.console import _rich_info

    _rich_info(message, symbol="info")


@contextmanager
def project_marketplace_scope(
    declared: Sequence[DeclaredMarketplace],
    *,
    notice: Callable[[str], None] | None = _default_notice,
) -> Iterator[None]:
    """Resolve *declared* names ahead of the registry for one command."""
    global _active
    scope = _ProjectMarketplaces(
        declared={entry.name.lower(): entry for entry in declared}, notice=notice
    )
    with _ACTIVE_LOCK:
        previous = _active
        _active = scope
    try:
        yield
    finally:
        with _ACTIVE_LOCK:
            _active = previous


def bind_project_marketplaces(
    declared: Sequence[DeclaredMarketplace] | None, *, user_scope: bool = False
) -> AbstractContextManager[None]:
    """Bind the root project's ``marketplaces:`` map for one command.

    A no-op when the project declares no map, and at user scope, where the
    user manifest's map is reconciled into the registry instead.
    """
    if declared is None or user_scope:
        return nullcontext()
    return project_marketplace_scope(declared)


def read_project_marketplaces(project_root: Path) -> tuple[DeclaredMarketplace, ...] | None:
    """Return the ``marketplaces:`` map of the ``apm.yml`` at *project_root*.

    For commands that read the lockfile without parsing the whole manifest.
    Returns ``None`` when there is no readable manifest or no map.

    Raises:
        ValueError: The manifest or its ``marketplaces:`` map is malformed.
    """
    from ..core.scope import manifest_file

    manifest = manifest_file(project_root)
    if not manifest.is_file():
        return None
    from ..models.manifest_io import load_manifest
    from .user_manifest import validate_marketplaces_declaration

    try:
        data = load_manifest(manifest)
    except OSError:
        return None
    if not isinstance(data, Mapping):
        return None
    return validate_marketplaces_declaration(data, manifest)


def project_declares_marketplaces() -> bool:
    """Return True while a root project's ``marketplaces:`` map is bound."""
    return _active is not None


def declared_marketplace_origin(entry: DeclaredMarketplace) -> str:
    """Return the catalog origin an entry names, without network access.

    The value matches what the lockfile records as ``source_url``; a local
    catalog has no portable origin and returns ``""``.

    Raises:
        ValueError: The entry's source is malformed.
    """
    from .registration import parse_registration_source

    parsed = parse_registration_source(entry.source, ref=entry.ref)
    if parsed.kind == "local":
        return ""
    return marketplace_origin(
        MarketplaceSource(
            name=entry.name,
            url=parsed.url,
            ref=parsed.ref,
            path="" if parsed.is_direct_url else "marketplace.json",
            origin=ORIGIN_PROJECT,
        )
    )


def _resolve(entry: DeclaredMarketplace) -> MarketplaceSource:
    from .registration import parse_registration_source, probe_registration

    try:
        parsed = parse_registration_source(entry.source, ref=entry.ref)
        source, _manifest = probe_registration(
            entry.name,
            parsed.url,
            parsed.ref,
            is_direct_url=parsed.is_direct_url,
            origin=ORIGIN_PROJECT,
        )
    except MarketplaceFetchError:
        raise
    except (MarketplaceError, ValueError) as exc:
        raise MarketplaceFetchError(entry.name, str(exc), retry_hint=_FIX_HINT) from exc
    return source


def _notice_shadowed_registration(scope: _ProjectMarketplaces, source: MarketplaceSource) -> None:
    if scope.notice is None:
        return
    from .registry import get_registered_marketplaces

    lower = source.name.lower()
    registered = next((s for s in get_registered_marketplaces() if s.name.lower() == lower), None)
    if registered is None:
        return
    scope.notice(
        f"Using marketplace '{source.name}' from apm.yml ({source.display_source}); "
        f"the registered '{registered.name}' ({registered.display_source}) "
        "is not used in this project."
    )


def project_marketplace(name: str) -> MarketplaceSource | None:
    """Return the project-declared marketplace for *name*, or ``None``.

    The first lookup of each name locates and fetches its catalog (no
    registry write) and prints one ``[i]`` line when a registration with the
    same name exists.

    Raises:
        MarketplaceFetchError: The declared catalog cannot be located or fetched.
    """
    scope = _active
    if scope is None:
        return None
    lower = name.lower()
    entry = scope.declared.get(lower)
    if entry is None:
        return None
    with scope.lock:
        source = scope.resolved.get(lower)
        if source is None:
            source = _resolve(entry)
            scope.resolved[lower] = source
            _notice_shadowed_registration(scope, source)
    return source


def declared_origin_conflict(name: str, locked_origin: str | None) -> str | None:
    """Return the origin apm.yml now declares for *name* when the lock recorded another.

    Compares the bound map's entry with a lock row's ``source_url`` without
    network access. ``None`` when no map is bound, the map does not declare
    *name*, the row records no origin (written before origins were recorded)
    or both name the same catalog origin. A local catalog entry has no
    origin, so it conflicts with any recorded one and is returned as its
    ``source``.
    """
    scope = _active
    if scope is None or not locked_origin:
        return None
    entry = scope.declared.get(name.lower())
    if entry is None:
        return None
    try:
        declared = declared_marketplace_origin(entry)
    except ValueError:
        declared = ""
    if declared and same_marketplace_origin(locked_origin, declared):
        return None
    return declared or entry.source


def rebound_by_project_map(name: str, locked_origin: str | None) -> bool:
    """Return True when the bound map names another origin than the lock row.

    Such a row re-resolves through the map (:func:`project_wins_locked_origin`
    prints its one ``[i]`` line); a row from the declared origin does not.
    """
    return declared_origin_conflict(name, locked_origin) is not None and (
        project_wins_locked_origin(name, locked_origin)
    )


def project_wins_locked_origin(name: str, locked_origin: str | None) -> bool:
    """Return True when the bound map, not a registration, owns *name*'s origin.

    The committed file wins (#2903 D3): a lock row recorded from another
    origin re-resolves through the map instead of raising the alias
    conflict a registration gets. The first such row per name prints one
    ``[i]`` line.
    """
    scope = _active
    if scope is None or name.lower() not in scope.declared:
        return False
    declared = declared_origin_conflict(name, locked_origin)
    if declared is None:
        return True
    with scope.lock:
        if name.lower() in scope.origin_changes_noted:
            return True
        scope.origin_changes_noted.add(name.lower())
    if scope.notice is not None:
        scope.notice(
            f"Marketplace '{name}' in apm.yml now names {declared}; apm.lock.yaml "
            f"recorded {locked_origin}. Re-resolving through apm.yml."
        )
    return True


def require_root_declared_marketplace(name: str, plugin_name: str, *, required_by: str) -> None:
    """Refuse a transitive ``marketplace: <name>`` the root map does not declare.

    No-op when the root project declares no ``marketplaces:`` map.

    Raises:
        UndeclaredMarketplaceError: The root map does not declare *name*.
    """
    scope = _active
    if scope is None or name.lower() in scope.declared:
        return
    raise UndeclaredMarketplaceError(name, plugin_name, required_by)
