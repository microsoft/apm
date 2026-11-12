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
    direct: bool = False
    target_subset: tuple[str, ...] | None = None
    exec_status: str | None = None


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


def _raise_or_note(strict: bool, collisions: list[str], message: str) -> None:
    """Refuse loudly under strict policy, or record a note when advisory."""
    if strict:
        raise CopilotSettingsCollisionError(message)
    collisions.append(message)


def discover_native_plugins(
    candidates: Iterable[ResolvedPluginCandidate],
    *,
    modules_dir: Path,
    ledger: Any | None = None,
    strict: bool = True,
    logger: Any | None = None,
) -> tuple[list[NativePluginEntry], list[str]]:
    """Return catalog entries plus collision notes for the resolved set.

    A directly declared dependency always outranks a transitive one when both
    claim a plugin name; the plugin name is attacker-controlled metadata, so a
    transitive package can never silently capture a name a direct dependency
    declares. Two claimants of the same precedence class are genuinely
    ambiguous and refuse (strict) or warn (advisory). A per-dependency target
    subset that excludes ``copilot`` drops the candidate entirely, and a
    winner whose identity flips relative to the ledger's recorded owner is
    refused unless the new winner is a direct dependency.

    Filesystem routing is split from entry building so callers can first ask
    whether any Agent Plugin is physically present before reading the
    (probe-free) admission capability.
    """
    routed = list(_routed_plugin_candidates(candidates, modules_dir=modules_dir, logger=logger))
    return build_native_plugin_entries(
        routed, modules_dir=modules_dir, ledger=ledger, strict=strict, logger=logger
    )


def _routed_plugin_candidates(
    candidates: Iterable[ResolvedPluginCandidate],
    *,
    modules_dir: Path,
    logger: Any | None = None,
) -> Iterable[tuple[ResolvedPluginCandidate, Any]]:
    """Yield ``(candidate, plugin)`` for every candidate that IS an Agent Plugin.

    Pure filesystem work: no capability read, no collision decision.
    Candidates are yielded in precedence order (direct first, then
    dependency key) so the entry builder can resolve name collisions
    deterministically. Every skip emits a named ``_drop`` trace, including
    the same-path dedupe branch.
    """
    from ..bundle.local_bundle import route_agent_plugin_package
    from .constants import COPILOT_TARGET_NAME

    def _drop(dependency_key: str, reason: str) -> None:
        if logger is not None:
            logger.verbose_detail(
                f"    Copilot native registration skipped {dependency_key}: {reason}"
            )

    seen_paths: set[Path] = set()
    ordered = sorted(candidates, key=lambda item: (not item.direct, item.dependency_key))
    for candidate in ordered:
        if (
            candidate.target_subset is not None
            and COPILOT_TARGET_NAME not in candidate.target_subset
        ):
            _drop(candidate.dependency_key, "its target subset excludes 'copilot'")
            continue
        install_path = candidate.install_path
        if not install_path.is_dir():
            _drop(candidate.dependency_key, "its install path is not a directory")
            continue
        resolved = install_path.resolve()
        if resolved in seen_paths:
            _drop(candidate.dependency_key, "another candidate already claims its install path")
            continue
        seen_paths.add(resolved)
        try:
            detection = route_agent_plugin_package(install_path)
        except AgentPluginError as exc:
            _drop(candidate.dependency_key, f"Agent Plugin routing failed ({exc})")
            continue
        if detection is None or detection.plugin is None:
            continue
        yield candidate, detection.plugin


def build_native_plugin_entries(
    routed: Iterable[tuple[ResolvedPluginCandidate, Any]],
    *,
    modules_dir: Path,
    ledger: Any | None = None,
    strict: bool = True,
    logger: Any | None = None,
) -> tuple[list[NativePluginEntry], list[str]]:
    """Build catalog entries from routed plugins, resolving name collisions.

    Consumes the precedence-ordered stream from :func:`_routed_plugin_candidates`
    and applies direct-wins name precedence, same-precedence ambiguity refusal,
    and the ledger-owner identity-flip guard.
    """

    def _drop(dependency_key: str, reason: str) -> None:
        if logger is not None:
            logger.verbose_detail(
                f"    Copilot native registration skipped {dependency_key}: {reason}"
            )

    entries: list[NativePluginEntry] = []
    collisions: list[str] = []
    claimed: dict[str, NativePluginEntry] = {}
    ambiguous_names: set[str] = set()
    for candidate, plugin in routed:
        try:
            entry = build_entry(
                dependency_key=candidate.dependency_key,
                plugin=plugin,
                marketplace_root=modules_dir,
                direct=candidate.direct,
            )
        except CatalogSourceError as exc:
            _drop(candidate.dependency_key, f"its catalog entry could not be built ({exc})")
            continue
        if entry.plugin_name in ambiguous_names:
            collisions.append(
                f"Agent Plugin name '{entry.plugin_name}' has multiple claimants at the "
                f"same precedence; skipping {entry.dependency_key}."
            )
            continue
        previous = claimed.get(entry.plugin_name)
        if previous is not None:
            if previous.direct and not entry.direct:
                collisions.append(
                    f"Agent Plugin name '{entry.plugin_name}' is declared directly by "
                    f"{previous.dependency_key} and transitively by "
                    f"{entry.dependency_key}; registering the direct dependency "
                    f"{previous.dependency_key} and skipping {entry.dependency_key}."
                )
                continue
            _raise_or_note(
                strict,
                collisions,
                f"Agent Plugin name '{entry.plugin_name}' is claimed by both "
                f"{previous.dependency_key} and {entry.dependency_key} at the same "
                "precedence; refusing to register an ambiguous plugin name. "
                "Rename one plugin so both can register.",
            )
            entries.remove(previous)
            claimed.pop(entry.plugin_name)
            ambiguous_names.add(entry.plugin_name)
            continue
        claimed[entry.plugin_name] = entry
        entries.append(entry)

    ordered_entries = order_entries(entries)
    if ledger is not None:
        admitted_entries: list[NativePluginEntry] = []
        for entry in ordered_entries:
            recorded = ledger.owner_of(entry.enabled_key)
            if recorded is not None and recorded != entry.dependency_key and not entry.direct:
                _raise_or_note(
                    strict,
                    collisions,
                    f"Agent Plugin '{entry.plugin_name}' was registered by "
                    f"{recorded} but is now claimed by the transitive dependency "
                    f"{entry.dependency_key}; refusing to silently re-point the "
                    "registration. Uninstall the previous owner first if this is "
                    "intended.",
                )
                continue
            admitted_entries.append(entry)
        ordered_entries = admitted_entries
    return ordered_entries, collisions


def registration_status(modules_dir: Path) -> tuple[str, ...]:
    """Return the plugin names APM currently registers with Copilot."""
    ledger = read_ledger(ledger_path_for(modules_dir))
    suffix = f"@{APM_MARKETPLACE_NAME}"
    return tuple(
        key[: -len(suffix)] if key.endswith(suffix) else key
        for key in sorted(ledger.enabled_plugins)
    )


def candidates_from_lockfile(lockfile: Any, modules_dir: Path) -> list[ResolvedPluginCandidate]:
    """Return registration candidates from canonical locked state.

    The synthesized self-entry (dependency key ``.``) is never a registration
    candidate; every other consumer of the dependency map filters it too.
    Direct dependencies (no declaring parent, not resolved by a parent repo)
    and any per-dependency target subset flow through so precedence and target
    narrowing decisions have the same inputs as the install path.

    This is the ONE executable trust gate shared by install and every lifecycle
    command (uninstall, prune, restore): a locked entry whose executables the
    scanner denied or left pending approval (``exec_status`` of
    :data:`~apm_cli.security.executables.TRUST_DENIED` /
    :data:`~apm_cli.security.executables.TRUST_GATED`) is dropped here, so a
    resync can never silently enable an MCP server the install refused. The
    in-flight ``deps_to_install`` overlay is gated separately by the install
    phase, since its exec status is not yet persisted to the lockfile.
    """
    from ..deps.lockfile import _SELF_KEY
    from ..security.executables import REGISTRABLE_EXEC_STATUSES

    candidates: list[ResolvedPluginCandidate] = []
    for dep_key, locked in sorted(getattr(lockfile, "dependencies", {}).items()):
        if dep_key == _SELF_KEY:
            continue
        exec_status = getattr(locked, "exec_status", None)
        # Allowlist, not denylist: a status APM does not recognise is never
        # registrable, matching the severity ladder that ranks unknown above
        # `denied`.
        if exec_status not in REGISTRABLE_EXEC_STATUSES:
            continue
        to_reference = getattr(locked, "to_dependency_ref", None)
        if to_reference is None:
            continue
        try:
            install_path = to_reference().get_install_path(modules_dir)
        except (ValueError, AttributeError):
            continue
        target_subset = getattr(locked, "target_subset", None)
        direct = (
            getattr(locked, "declaring_parent", None) is None
            and getattr(locked, "resolved_by", None) is None
        )
        candidates.append(
            ResolvedPluginCandidate(
                dependency_key=dep_key,
                install_path=Path(install_path),
                direct=direct,
                target_subset=tuple(target_subset) if target_subset else None,
                exec_status=exec_status,
            )
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
    strict: bool = False,
) -> CopilotPluginSyncResult:
    """Rebuild the registration after a lifecycle command mutated locked state.

    Uninstall, prune, and restore all converge here so the APM-owned catalog
    and settings entries never drift from ``apm.lock.yaml``. These lifecycle
    commands are advisory by default (``strict=False``): a residual plugin-name
    collision is downgraded to a note instead of failing the whole command.
    """
    from ..integration.targets import resolve_targets
    from .capability import (
        current_native_registration,
        resolve_native_registration_capability,
    )

    # Reuse the capability already published for this command when available, so
    # a single lifecycle command never resolves targets more than once. Only
    # when nothing is published do we resolve fresh from the caller-supplied
    # targets (or, as a last resort, re-derive them). Resolving is a pure,
    # immediate function of target names -- no runtime is invoked.
    capability = current_native_registration()
    if capability is None:
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
        strict=strict,
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
    strict: bool = True,
) -> CopilotPluginSyncResult:
    """Rebuild APM's Copilot plugin registration from resolved state."""
    catalog_path = catalog_path_for(modules_dir)
    ledger_path = ledger_path_for(modules_dir)
    ledger = read_ledger(ledger_path)
    had_registration = ledger.marketplace_owned or bool(ledger.enabled_plugins)

    # Filesystem discovery FIRST: routing is pure filesystem work, so a command
    # with no Agent Plugin present and no prior registration returns here
    # without ever reading the capability. This is the overwhelmingly common
    # case.
    routed = list(_routed_plugin_candidates(candidates, modules_dir=modules_dir, logger=logger))
    if not routed and not had_registration:
        return CopilotPluginSyncResult()

    entries: list[NativePluginEntry] = []
    collisions: list[str] = []
    if capability is not None and capability.supported:
        entries, collisions = build_native_plugin_entries(
            routed, modules_dir=modules_dir, ledger=ledger, strict=strict, logger=logger
        )
    elif not had_registration:
        return CopilotPluginSyncResult(
            skipped_reason=(capability.reason if capability is not None else None)
        )

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
        if logger is not None:
            if entries:
                noun = "Agent Plugin" if len(entries) == 1 else "Agent Plugins"
                logger.dry_run_notice(
                    f"Would register {len(entries)} {noun} with GitHub Copilot in {settings_path}"
                )
            elif had_registration:
                logger.dry_run_notice(
                    f"Would remove the APM Copilot marketplace from {settings_path}"
                )
        return result

    document = read_settings_document(settings_path)
    if entries:
        marketplace_path = marketplace_source_path(
            modules_dir=modules_dir, project_root=project_root, scope=scope
        )
        enabled_keys = tuple(entry.enabled_key for entry in entries)
        plugin_owners = {entry.enabled_key: entry.dependency_key for entry in entries}
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
                plugin_owners=plugin_owners,
            ),
            settings_path=settings_path,
            settings_text=render_settings_document(merge.document),
            write_settings=merge.changed,
            modules_dir=modules_dir,
        )
        result.changed = True
        _log_registration(logger, entries, settings_path)
        return result

    removal_ledger = _removal_ledger(ledger, catalog_path)
    removal = remove_registration(
        document=document, settings_path=settings_path, ledger=removal_ledger
    )
    _commit(
        catalog_path=catalog_path,
        catalog_text=None,
        ledger_path=ledger_path,
        ledger_text=None,
        settings_path=settings_path,
        settings_text=render_settings_document(removal.document),
        write_settings=removal.changed and settings_path.is_file(),
        modules_dir=modules_dir,
    )
    result.changed = removal.changed or had_registration
    if logger is not None and result.changed:
        logger.info(
            f"Removed the APM Copilot plugin marketplace from {settings_path}",
            symbol="info",
        )
    return result


def _removal_ledger(ledger: Any, catalog_path: Path) -> Any:
    """Return the ledger to drive removal, healing an unreadable ledger.

    A ledger that exists but cannot be parsed would otherwise remove nothing
    (fail-OPEN) while the catalog is still deleted, leaving orphaned
    ``enabledPlugins`` entries. When that happens, the still-present catalog is
    the authoritative record of what APM owns, so removal keys are derived from
    it instead.
    """
    if not getattr(ledger, "unreadable", False):
        return ledger
    from .settings import RegistrationLedger

    return RegistrationLedger(
        marketplace_owned=True,
        enabled_plugins=_catalog_derived_enabled_keys(catalog_path),
    )


def _catalog_derived_enabled_keys(catalog_path: Path) -> tuple[str, ...]:
    """Return ``<plugin>@apm`` keys derived from the on-disk catalog."""
    import json

    if not catalog_path.is_file():
        return ()
    try:
        loaded = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    plugins = loaded.get("plugins") if isinstance(loaded, dict) else None
    if not isinstance(plugins, list):
        return ()
    keys = []
    for plugin in plugins:
        if isinstance(plugin, dict) and isinstance(plugin.get("name"), str):
            keys.append(f"{plugin['name']}@{APM_MARKETPLACE_NAME}")
    return tuple(sorted(set(keys)))


def _commit(
    *,
    catalog_path: Path,
    catalog_text: str | None,
    ledger_path: Path,
    ledger_text: str | None,
    settings_path: Path,
    settings_text: str,
    write_settings: bool,
    modules_dir: Path,
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
    except OSError:
        for write in reversed(staged):
            write.restore()
        raise
    if catalog_text is None and ledger_text is None:
        _prune_empty_parents(catalog_path.parent, stop=modules_dir)


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
    names = [entry.plugin_name for entry in entries]
    shown = ", ".join(names[:3])
    if len(names) > 3:
        shown += f", and {len(names) - 3} more"
    logger.success(
        f"Registered {len(entries)} Agent {noun} with GitHub Copilot "
        f"via the '{APM_MARKETPLACE_NAME}' marketplace ({shown})",
        symbol="check",
    )
    if len(names) > 3:
        logger.verbose_detail(f"    plugins: {', '.join(names)}")
    logger.verbose_detail(f"    settings: {settings_path}")
