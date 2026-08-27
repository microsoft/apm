"""Canonical owner of APM's native GitHub Copilot Agent Plugin registration.

One function -- :func:`synchronize_copilot_plugins` -- rebuilds the whole
registration from canonical resolved state:

1. read the materialized Agent Plugins named by the resolved dependency set;
2. render the APM-owned directory-marketplace catalog;
3. merge APM's two namespaced settings entries;
4. record what APM owns in its ledger.

Install, update, restore, uninstall, and prune all call the same function, so
there is exactly one place that decides what Copilot sees. Writes are staged
and rolled back together: a failure leaves the previous registration bytes
exactly as they were.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agent_plugins.errors import AgentPluginError
from ..core.scope import InstallScope, is_user_scope
from ..utils.atomic_io import atomic_write_text
from .capability import NativeRegistrationCapability
from .catalog import (
    CatalogSourceError,
    NativePluginEntry,
    build_entry,
    order_entries,
    render_catalog,
)
from .constants import (
    APM_MARKETPLACE_NAME,
    MARKETPLACE_MANIFEST_RELATIVE,
    REGISTRATION_LEDGER_RELATIVE,
)
from .settings import (
    CopilotSettingsCollisionError,
    merge_registration,
    read_ledger,
    read_settings_document,
    remove_registration,
    render_ledger,
    render_settings_document,
    resolve_settings_path,
)


@dataclass(frozen=True, slots=True)
class ResolvedPluginCandidate:
    """One resolved dependency whose bytes may hold an Agent Plugin."""

    dependency_key: str
    install_path: Path


@dataclass
class CopilotPluginSyncResult:
    """What one registration synchronization changed."""

    entries: list[NativePluginEntry] = field(default_factory=list)
    changed: bool = False
    catalog_path: Path | None = None
    settings_path: Path | None = None
    skipped_reason: str | None = None
    collisions: list[str] = field(default_factory=list)

    @property
    def plugin_names(self) -> tuple[str, ...]:
        """Return registered plugin names in catalog order."""
        return tuple(entry.plugin_name for entry in self.entries)


class _StagedWrite:
    """One file mutation that can be rolled back byte-for-byte."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.existed = path.is_file()
        self.previous = path.read_bytes() if self.existed else None

    def restore(self) -> None:
        """Restore the pre-write bytes, or remove a file APM created."""
        try:
            if self.previous is None:
                if self.path.is_file():
                    self.path.unlink()
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(self.previous)
        except OSError:  # pragma: no cover - best-effort rollback
            pass


def catalog_path_for(modules_dir: Path) -> Path:
    """Return the APM-owned catalog path for one materialization root."""
    return modules_dir / MARKETPLACE_MANIFEST_RELATIVE


def ledger_path_for(modules_dir: Path) -> Path:
    """Return the APM ownership ledger path for one materialization root."""
    return modules_dir / REGISTRATION_LEDGER_RELATIVE


def marketplace_source_path(
    *,
    modules_dir: Path,
    project_root: Path,
    scope: InstallScope | Any,
) -> str:
    """Return the marketplace path recorded in Copilot settings.

    Project scope uses a repository-relative path so the registration survives
    clones, worktrees, and moved checkouts. Global scope uses an absolute path
    because the settings document is not anchored to any repository.
    """
    if is_user_scope(scope):
        return modules_dir.as_posix()
    from ..utils.paths import portable_relpath

    return portable_relpath(modules_dir, project_root)


def discover_native_plugins(
    candidates: Iterable[ResolvedPluginCandidate],
    *,
    modules_dir: Path,
) -> tuple[list[NativePluginEntry], list[str]]:
    """Return catalog entries plus collision notes for the resolved set."""
    from ..bundle.local_bundle import route_agent_plugin_package

    entries: list[NativePluginEntry] = []
    collisions: list[str] = []
    claimed: dict[str, NativePluginEntry] = {}
    seen_paths: set[Path] = set()

    for candidate in sorted(candidates, key=lambda item: item.dependency_key):
        install_path = candidate.install_path
        if not install_path.is_dir():
            continue
        resolved = install_path.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        try:
            detection = route_agent_plugin_package(install_path)
        except AgentPluginError:
            continue
        if detection is None or detection.plugin is None:
            continue
        try:
            entry = build_entry(
                dependency_key=candidate.dependency_key,
                plugin=detection.plugin,
                marketplace_root=modules_dir,
            )
        except CatalogSourceError:
            continue
        previous = claimed.get(entry.plugin_name)
        if previous is not None:
            collisions.append(
                f"Agent Plugin name '{entry.plugin_name}' is provided by both "
                f"{previous.dependency_key} and {entry.dependency_key}; "
                f"registering {previous.dependency_key} and skipping "
                f"{entry.dependency_key}. Rename one plugin to register both."
            )
            continue
        claimed[entry.plugin_name] = entry
        entries.append(entry)

    return order_entries(entries), collisions


def registration_status(modules_dir: Path) -> tuple[str, ...]:
    """Return the plugin names APM currently registers with Copilot."""
    from .settings import read_ledger

    ledger = read_ledger(ledger_path_for(modules_dir))
    suffix = f"@{APM_MARKETPLACE_NAME}"
    return tuple(
        key[: -len(suffix)] if key.endswith(suffix) else key
        for key in sorted(ledger.enabled_plugins)
    )


def candidates_from_lockfile(lockfile: Any, modules_dir: Path) -> list[ResolvedPluginCandidate]:
    """Return registration candidates from canonical locked state."""
    candidates: list[ResolvedPluginCandidate] = []
    for dep_key, locked in sorted(getattr(lockfile, "dependencies", {}).items()):
        if dep_key == ".":
            continue
        to_reference = getattr(locked, "to_dependency_ref", None)
        if to_reference is None:
            continue
        try:
            install_path = to_reference().get_install_path(modules_dir)
        except (ValueError, AttributeError):
            continue
        candidates.append(
            ResolvedPluginCandidate(dependency_key=dep_key, install_path=Path(install_path))
        )
    return candidates


def resync_native_plugins(
    *,
    project_root: Path,
    modules_dir: Path,
    scope: InstallScope | Any,
    lockfile: Any | None,
    logger: Any | None = None,
    dry_run: bool = False,
    targets: Any | None = None,
) -> CopilotPluginSyncResult:
    """Rebuild the registration after a lifecycle command mutated locked state.

    Uninstall, prune, and restore all converge here so the APM-owned catalog
    and settings entries never drift from ``apm.lock.yaml``.
    """
    from ..integration.targets import resolve_targets
    from .capability import resolve_native_registration_capability

    resolved_targets = (
        targets
        if targets is not None
        else resolve_targets(project_root, user_scope=is_user_scope(scope))
    )
    capability = resolve_native_registration_capability(resolved_targets)
    candidates = candidates_from_lockfile(lockfile, modules_dir) if lockfile is not None else []
    return synchronize_copilot_plugins(
        project_root=project_root,
        modules_dir=modules_dir,
        scope=scope,
        candidates=candidates,
        capability=capability,
        logger=logger,
        dry_run=dry_run,
    )


def synchronize_copilot_plugins(
    *,
    project_root: Path,
    modules_dir: Path,
    scope: InstallScope | Any,
    candidates: Sequence[ResolvedPluginCandidate],
    capability: NativeRegistrationCapability | None,
    logger: Any | None = None,
    dry_run: bool = False,
) -> CopilotPluginSyncResult:
    """Rebuild APM's Copilot plugin registration from resolved state."""
    catalog_path = catalog_path_for(modules_dir)
    ledger_path = ledger_path_for(modules_dir)
    ledger = read_ledger(ledger_path)
    had_registration = ledger.marketplace_owned or bool(ledger.enabled_plugins)

    if (capability is None or not capability.supported) and not had_registration:
        return CopilotPluginSyncResult(
            skipped_reason=(capability.reason if capability is not None else None)
        )

    entries: list[NativePluginEntry] = []
    collisions: list[str] = []
    if capability is not None and capability.supported:
        entries, collisions = discover_native_plugins(candidates, modules_dir=modules_dir)

    settings_path = resolve_settings_path(scope, project_root, ledger)
    result = CopilotPluginSyncResult(
        entries=entries,
        catalog_path=catalog_path if entries else None,
        settings_path=settings_path,
        collisions=collisions,
    )
    if logger is not None:
        for note in collisions:
            logger.warning(note)

    if dry_run:
        result.changed = bool(entries) or had_registration
        return result

    document = read_settings_document(settings_path)
    if entries:
        marketplace_path = marketplace_source_path(
            modules_dir=modules_dir, project_root=project_root, scope=scope
        )
        enabled_keys = tuple(entry.enabled_key for entry in entries)
        merge = merge_registration(
            document=document,
            settings_path=settings_path,
            marketplace_path=marketplace_path,
            enabled_keys=enabled_keys,
            ledger=ledger,
        )
        _commit(
            catalog_path=catalog_path,
            catalog_text=render_catalog(entries),
            ledger_path=ledger_path,
            ledger_text=render_ledger(
                settings_path=settings_path,
                marketplace_path=marketplace_path,
                enabled_plugins=enabled_keys,
                project_root=project_root,
                scope=scope,
            ),
            settings_path=settings_path,
            settings_text=render_settings_document(merge.document),
            write_settings=merge.changed,
        )
        result.changed = True
        _log_registration(logger, entries, settings_path)
        return result

    removal = remove_registration(document=document, settings_path=settings_path, ledger=ledger)
    _commit(
        catalog_path=catalog_path,
        catalog_text=None,
        ledger_path=ledger_path,
        ledger_text=None,
        settings_path=settings_path,
        settings_text=render_settings_document(removal.document),
        write_settings=removal.changed and settings_path.is_file(),
    )
    result.changed = removal.changed or had_registration
    if logger is not None and result.changed:
        logger.info(
            f"Removed the APM Copilot plugin marketplace from {settings_path.name}",
            symbol="info",
        )
    return result


def _commit(
    *,
    catalog_path: Path,
    catalog_text: str | None,
    ledger_path: Path,
    ledger_text: str | None,
    settings_path: Path,
    settings_text: str,
    write_settings: bool,
) -> None:
    """Apply catalog, ledger, and settings writes as one rollback unit."""
    staged = [_StagedWrite(catalog_path), _StagedWrite(ledger_path)]
    if write_settings:
        staged.append(_StagedWrite(settings_path))
    try:
        if catalog_text is None:
            _remove_generated(catalog_path)
        else:
            catalog_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(catalog_path, catalog_text)
        if ledger_text is None:
            _remove_generated(ledger_path)
        else:
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(ledger_path, ledger_text)
        if write_settings:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(settings_path, settings_text)
    except (OSError, CopilotSettingsCollisionError):
        for write in reversed(staged):
            write.restore()
        raise
    if catalog_text is None and ledger_text is None:
        _prune_empty_parents(catalog_path.parent, stop=catalog_path.parents[2])


def _remove_generated(path: Path) -> None:
    """Delete one APM-generated file when it exists."""
    if path.is_file():
        path.unlink()


def _prune_empty_parents(start: Path, *, stop: Path) -> None:
    """Remove APM-generated catalog directories once they are empty."""
    current = start
    while current != stop and current.is_dir():
        try:
            next(current.iterdir())
            return
        except StopIteration:
            pass
        except OSError:  # pragma: no cover - defensive
            return
        try:
            current.rmdir()
        except OSError:  # pragma: no cover - defensive
            return
        current = current.parent


def _log_registration(
    logger: Any | None,
    entries: list[NativePluginEntry],
    settings_path: Path,
) -> None:
    """Report the APM-owned registration through the canonical logger."""
    if logger is None or not entries:
        return
    noun = "Plugin" if len(entries) == 1 else "Plugins"
    names = ", ".join(entry.plugin_name for entry in entries)
    logger.info(
        f"Registered {len(entries)} Agent {noun} with GitHub Copilot "
        f"via the '{APM_MARKETPLACE_NAME}' marketplace ({names})",
        symbol="check",
    )
    logger.verbose_detail(f"    settings: {settings_path}")
