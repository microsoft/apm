"""Catalog-origin safety for machine-local marketplace aliases (#3912).

An alias such as ``company`` is a nickname on one machine. The lockfile records
the catalog's canonical origin (``source_url``), and these checks compare that
origin -- never the alias -- before an alias is trusted to resolve.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.remediation import (
    MARKETPLACE_DUPLICATE_ALIAS,
    MARKETPLACE_ORIGIN_READD,
    MARKETPLACE_ORIGIN_REINSTALL,
    either_sentence,
)
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


def invocation_user_scope() -> bool:
    """Whether the running ``apm`` command was invoked at user scope (``-g``).

    Read from the Click invocation, so a refusal raised below a command that
    does not thread its scope still renders the remedy for that scope. Outside
    a Click invocation (library use) the scope is the project.
    """
    import click

    ctx = click.get_current_context(silent=True)
    while ctx is not None:
        if ctx.params.get("global_"):
            return True
        ctx = ctx.parent
    return False


def require_locked_catalog_origin(
    marketplace_name: str,
    plugin_name: str,
    lockfile: LockFile | None,
    *,
    user_scope: bool | None = None,
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

    *user_scope* selects the scope the refusal's remedy names; ``None``
    reads it from the invocation (:func:`invocation_user_scope`).

    Raises:
        MarketplaceOriginConflictError: The alias's registered origin differs
            from an origin the lockfile recorded for this plugin.
        MarketplaceNotFoundError: The alias is not registered.
    """
    if lockfile is None:
        return
    for dep in lockfile.marketplace_rows(marketplace_name, plugin_name):
        require_locked_row_origin(dep, marketplace_name=marketplace_name, user_scope=user_scope)


def require_locked_row_origin(
    locked: LockedDependency,
    *,
    marketplace_name: str = "",
    user_scope: bool | None = None,
) -> None:
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
        refused_user_scope = invocation_user_scope() if user_scope is None else user_scope
        raise MarketplaceOriginConflictError(
            alias,
            locked.marketplace_plugin_name,
            locked.source_url,
            current or source.display_source,
            user_scope=refused_user_scope,
            manifest_entry=declared_manifest_entry(locked, user_scope=refused_user_scope),
        )


def declared_manifest_entry(locked: LockedDependency, *, user_scope: bool) -> str | dict | None:
    """Return the ``apm.yml`` dependency entry that declares *locked*, as written.

    The remedy for a rebound alias names this entry (RA-042): a marketplace
    install records the resolved source, not ``name@marketplace``. ``None``
    when the scope's manifest is absent, unreadable or declares no match.
    """
    import yaml

    from ..core.scope import InstallScope, get_apm_dir, manifest_file
    from ..models.dependency.reference import DependencyReference

    scope = InstallScope.USER if user_scope else InstallScope.PROJECT
    try:
        wanted = locked.to_dependency_ref(effective=False).get_unique_key()
        data = yaml.safe_load(manifest_file(get_apm_dir(scope)).read_text(encoding="utf-8"))
        entries = (data or {}).get("dependencies", {}).get("apm", []) or []
    except (OSError, ValueError, AttributeError, TypeError, yaml.YAMLError):
        return None
    for entry in entries:
        try:
            if isinstance(entry, str):
                ref = DependencyReference.parse(entry)
            elif isinstance(entry, dict):
                ref = DependencyReference.parse_from_dict(entry)
            else:
                continue
        except (ValueError, TypeError, KeyError):
            continue
        if ref.is_marketplace:
            if (ref.marketplace_plugin_name, ref.marketplace_name) == (
                locked.marketplace_plugin_name,
                locked.discovered_via,
            ):
                return entry
        elif ref.get_unique_key() == wanted:
            return entry
    return None


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


_PROVENANCE_FIELDS = ("discovered_via", "marketplace_plugin_name", "source_url", "source_digest")
_CATALOG_FIELDS = ("source_url", "source_digest")
# Origin evidence (source_url/source_digest) and its lock format are first
# written by APM 0.37; an older generator stamp is the only legacy evidence.
_ORIGIN_RECORDING_RELEASE = (0, 37)
_RELEASE = re.compile(r"^\s*v?(\d+)\.(\d+)")
_NEXT_STEP = f"Nothing was changed. {either_sentence(MARKETPLACE_ORIGIN_READD, MARKETPLACE_ORIGIN_REINSTALL)}"


def locked_provenance(dep: LockedDependency) -> dict[str, str | None]:
    """One locked row's provenance snapshot, in the lockfile phase's shape (#4430)."""
    return {field: getattr(dep, field) for field in _PROVENANCE_FIELDS}


def _predates_origin_recording(apm_version: str | None) -> bool:
    """Whether the lock's generator stamp names a release before origin recording."""
    match = _RELEASE.match(apm_version) if isinstance(apm_version, str) else None
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2))) < _ORIGIN_RECORDING_RELEASE


def _legacy_lock(lockfile: LockFile) -> bool:
    """A pre-0.37 lock; a mixed one (old stamp, newer fields) is refused as edited."""
    if not _predates_origin_recording(lockfile.apm_version):
        return False
    rows = lockfile.dependencies.values()
    if lockfile.is_origin_recording_format() or any(
        getattr(dep, field) for dep in rows for field in _CATALOG_FIELDS
    ):
        raise MarketplaceProvenanceError(
            f"apm.lock.yaml says APM {lockfile.apm_version} wrote it, but it carries fields "
            f"only APM 0.37 or later writes, so it may have been edited. {_NEXT_STEP}"
        )
    return True


def _declared_row_keeps_provenance(lockfile: LockFile, declared: Iterable[object]) -> list[str]:
    """Restore or refuse a marketplace declaration whose locked row lost its provenance.

    apm.yml is the source of truth: an entry declared as ``name@marketplace``
    (or with a ``marketplace:`` key) must keep its marketplace provenance in
    the lock. A row that names another alias but records the declared alias's
    registered origin is restored to the declared alias and returned for the
    caller to report; a stripped row, or one recording another origin, is
    refused, so it is never re-recorded from the catalog unseen.
    """
    from ..models.dependency import DependencyReference
    from .dependency_confusion import admit_marketplace_resolution
    from .resolver import resolve_marketplace_plugin

    restored: list[str] = []
    for ref in declared:
        alias = getattr(ref, "marketplace_name", None)
        plugin = getattr(ref, "marketplace_plugin_name", None)
        if not (getattr(ref, "is_marketplace", False) and alias and plugin):
            continue
        if lockfile.marketplace_rows(alias, plugin):
            continue
        require_locked_catalog_origin(alias, plugin, lockfile)
        try:
            # Origin only: ref pins stay with install and update resolution.
            resolution = resolve_marketplace_plugin(plugin, alias, enforce_ref_pins=False)
            admit_marketplace_resolution(resolution)
        except MarketplaceError as exc:
            raise MarketplaceProvenanceError(
                f"Cannot re-verify the marketplace provenance of {plugin}@{alias}: {exc}"
            ) from exc
        resolved = resolution.dependency_reference or DependencyReference.parse(
            resolution.canonical
        )
        key = resolved.get_unique_key()
        dep = lockfile.dependencies.get(key)
        if dep is None:
            continue
        if _records_declared_origin(dep, alias):
            dep.discovered_via, dep.marketplace_plugin_name = alias, plugin
            restored.append(f"{key} ({plugin}@{alias})")
            continue
        raise MarketplaceProvenanceError(
            f"apm.yml declares {plugin}@{alias}, but apm.lock.yaml records {key} "
            f"without its marketplace provenance. {_NEXT_STEP}"
        )
    return restored


def _records_declared_origin(dep: LockedDependency, alias: str) -> bool:
    """Whether *dep* records the origin the user registered for *alias*."""
    if not (dep.discovered_via and dep.source_url):
        return False
    current = marketplace_origin(get_marketplace_by_name(alias))
    return same_marketplace_origin(dep.source_url, current)


def _registered_local_catalog(alias: str) -> bool:
    """Whether the user registered *alias* as a local catalog, which has no origin.

    Decided by the user's marketplace registration only, never by the lock row.
    """
    try:
        return get_marketplace_by_name(alias).kind == "local"
    except MarketplaceError:
        return False


def _declared_dependencies(manifest: Path | None) -> list[object]:
    if manifest is None or not manifest.is_file():
        return []
    from ..models.apm_package import APMPackage

    return list(APMPackage.from_apm_yml(manifest).get_all_apm_dependencies())


def reverify_locked_provenance(
    lockfile: LockFile | None, manifest: Path | None = None
) -> list[str]:
    """Re-verify every locked marketplace row against its registered catalog (#4228).

    ``apm update`` calls this before planning, so recorded provenance is never
    carried forward on the lock's word alone. A row's alias and plugin name
    must both be recorded and still resolve, and a lock from APM 0.37 or later
    must carry the row's origin evidence; its origin must match the registered
    catalog (:func:`require_locked_row_origin`). A stale or missing catalog
    digest is replaced by the catalog's current one: the digest records
    provenance and is not a trust anchor, and the row's origin already matched
    the user's registration. Each changed row is returned so the caller can
    report it. A pre-0.37 lock (:func:`_legacy_lock`) is
    accepted without origin evidence; its next lock write records it.

    Raises:
        MarketplaceProvenanceError: A row records incomplete provenance, a
            mixed lock claims an older generator, a *declared* marketplace
            entry's row lost its provenance, or the alias or plugin no longer
            resolves.
        MarketplaceOriginConflictError: The alias names a different catalog.
    """
    from .dependency_confusion import admit_marketplace_resolution
    from .resolver import resolve_marketplace_plugin

    if lockfile is None:
        return []
    legacy = _legacy_lock(lockfile)
    restored = _declared_row_keeps_provenance(lockfile, _declared_dependencies(manifest))
    for dep in lockfile.dependencies.values():
        if not any(getattr(dep, field) for field in _PROVENANCE_FIELDS):
            continue
        key = dep.get_unique_key()
        alias, plugin = dep.discovered_via, dep.marketplace_plugin_name
        if not (alias and plugin):
            raise MarketplaceProvenanceError(
                f"apm.lock.yaml records incomplete marketplace provenance for {key}: "
                f"discovered_via and marketplace_plugin_name are both required. {_NEXT_STEP}"
            )
        if legacy:
            continue
        if not dep.source_url and not _registered_local_catalog(alias):
            raise MarketplaceProvenanceError(
                f"apm.lock.yaml records no marketplace origin for {key} ({plugin}@{alias}). "
                + _NEXT_STEP
            )
        require_locked_row_origin(dep)
        try:
            # Origin only: ref pins stay with install and update resolution.
            resolution = resolve_marketplace_plugin(plugin, alias, enforce_ref_pins=False)
            admit_marketplace_resolution(resolution)
        except MarketplaceError as exc:
            raise MarketplaceProvenanceError(
                f"Cannot re-verify the marketplace provenance of {key} ({plugin}@{alias}): {exc}"
            ) from exc
        current = resolution.provenance(alias, plugin)
        changed = False
        for field in _CATALOG_FIELDS:
            value = current.get(field)
            if not value:
                continue
            if not getattr(dep, field) and field != "source_digest":
                raise MarketplaceProvenanceError(
                    f"apm.lock.yaml records no marketplace {field} for {key} "
                    f"({plugin}@{alias}). {_NEXT_STEP}"
                )
            if getattr(dep, field) != value:
                setattr(dep, field, value)
                changed = True
        if changed:
            restored.append(f"{key} ({plugin}@{alias})")
    return restored
