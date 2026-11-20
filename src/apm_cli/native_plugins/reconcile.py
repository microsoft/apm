"""Converge one scope's deployed native plugins and harness settings onto the lock.

The single reconcile behind install, uninstall and prune for every harness
that loads plugins from a local directory marketplace (OpenAPM req-na-002 to
req-na-004). It deploys each locked plugin root to the profile root, writes
the generated catalog beside them, and declares them through the shared
settings writer in each harness's settings file. Ownership comes only from
the ``native_plugin`` records in the previous and next lock.
"""

from __future__ import annotations

import contextlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm_cli.utils.atomic_io import atomic_write_text

from .binding import NativeBinding
from .errors import NativePluginError
from .harnesses import (
    COPILOT,
    NATIVE_HARNESSES,
    effective_enable,
    entry_harnesses,
    foreign_marketplace,
    read_order,
    settings_path,
)
from .layout import (
    NativeLayoutError,
    deploy_plugin,
    prune_empty,
    retire_plugin,
    write_catalog,
)
from .record import NativePluginRecord, lock_native_plugins
from .settings import (
    BINDING_CONFLICT,
    BINDING_MOVED,
    MARKETPLACES_KEY,
    PREVIOUS_LOCK_MISSING,
    NativeSettingsError,
    SettingsChange,
    SettingsDeclaration,
    write_declaration,
)

HARNESS_LABELS = {"claude": "Claude", COPILOT: "Copilot"}


@dataclass(frozen=True)
class NativeOwnership:
    """One locked dependency's native record and its immutable source pin."""

    record: NativePluginRecord
    pin: str
    local: bool


def _source_pin(locked: Any) -> str:
    revision = (
        locked.local_path
        if locked.source == "local"
        else locked.resolved_commit or locked.resolved_hash
    )
    return f"{locked.source}:{revision}" if revision else ""


def native_ownership(lockfile: Any) -> dict[str, NativeOwnership]:
    """Return ``{dependency_key: ownership}`` for native records in *lockfile*."""
    return {
        key: NativeOwnership(
            record,
            _source_pin(lockfile.dependencies[key]),
            lockfile.dependencies[key].source == "local",
        )
        for key, record in lock_native_plugins(lockfile).items()
    }


def _verify_source(
    key: str,
    own: NativeOwnership,
    lockfile: Any,
    modules_dir: Path,
    binding: NativeBinding,
    prepared: Mapping[str, Any],
    previous: Mapping[str, NativeOwnership],
) -> Path:
    """Return the verified ``apm_modules`` source for one declared plugin."""
    name = own.record.name
    restore = "apm install --global" if not binding.project else "apm install"
    source = lockfile.dependencies[key].to_dependency_ref().get_install_path(modules_dir)
    if not source.is_dir():
        raise NativePluginError(
            f"The source of native plugin '{name}' ({key}) is missing from apm_modules. "
            f"Run '{restore}' to restore it; harness settings were not changed."
        )
    plan = prepared.get(key)
    if plan is not None and plan.bundle_hash != own.record.fingerprint:
        raise NativePluginError(f"Native plugin '{name}' source changed after admission.")
    prior = previous.get(key)
    if (
        prior is not None
        and not own.local
        and prior.pin == own.pin
        and prior.record.fingerprint != own.record.fingerprint
    ):
        raise NativePluginError(
            f"Native plugin '{name}' ({key}) changed content at the same pinned revision. "
            "APM will not adopt modified publisher content; restore the pinned package or "
            "pin a new revision."
        )
    if binding.fingerprint(source) != own.record.fingerprint:
        raise NativePluginError(
            f"Native plugin source for {key} does not match its locked fingerprint. "
            f"Restore the pinned package with '{restore}' before retrying."
        )
    return source


def _declared_plugins(
    following: Mapping[str, NativeOwnership],
    lockfile: Any,
    modules_dir: Path,
    binding: NativeBinding,
    prepared: Mapping[str, Any],
    prior: Mapping[str, NativeOwnership],
) -> dict[str, tuple[str, NativeOwnership, Path]]:
    """Verify every plugin the next lock activates; refuse collisions."""
    prior_names = {own.record.name for own in prior.values()}
    declared: dict[str, tuple[str, NativeOwnership, Path]] = {}
    folded: dict[str, tuple[str, str]] = {}
    for key, own in sorted(following.items()):
        name = own.record.name
        if name.casefold() in folded:
            other, other_key = folded[name.casefold()]
            raise NativePluginError(
                f"Native plugin name collision: '{name}' ({key}) and '{other}' ({other_key}) "
                "name the same plugin directory. Exclude one from its native targets."
            )
        folded[name.casefold()] = (name, key)
        source = _verify_source(key, own, lockfile, modules_dir, binding, prepared, prior)
        existing = binding.root / name
        if (
            name not in prior_names
            and existing.exists()
            and binding.fingerprint(existing) != own.record.fingerprint
        ):
            raise NativePluginError(
                f"{existing} already exists and the previous apm.lock.yaml does not record "
                "it. Move it aside, then retry; APM never replaces content it did not deploy."
            )
        declared[name] = (key, own, source)
    return declared


class _SettingsSnapshot:
    """Settings bytes before this run, restored when the run fails (req-na-004)."""

    def __init__(self, paths: Collection[Path]) -> None:
        self._before = {path: path.read_bytes() if path.is_file() else None for path in paths}

    def restore(self) -> None:
        for path, data in self._before.items():
            with contextlib.suppress(OSError):
                if data is None:
                    path.unlink(missing_ok=True)
                elif not path.is_file() or path.read_bytes() != data:
                    atomic_write_text(path, data.decode("utf-8"))


@dataclass
class _Surface:
    harness: str
    path: Path
    plugins: frozenset[str]


def _surfaces(
    declared: Mapping[str, tuple[str, NativeOwnership, Path]],
    lockfile: Any,
    binding: NativeBinding,
    deploy_root: Path,
    configured: Collection[str],
) -> list[_Surface]:
    wanted: dict[str, set[str]] = {harness: set() for harness in NATIVE_HARNESSES}
    for name, (key, _own, _source) in declared.items():
        locked = lockfile.dependencies[key]
        for harness in entry_harnesses(locked, project=binding.project, configured=configured):
            wanted[harness].add(name)
    return [
        _Surface(
            harness,
            settings_path(harness, deploy_root=deploy_root, project=binding.project),
            frozenset(names),
        )
        for harness, names in wanted.items()
    ]


def _refuse_foreign_binding(surface: _Surface, binding: NativeBinding, deploy_root: Path) -> None:
    """req-na-007: no file the harness reads may bind APM's name elsewhere."""
    if not surface.plugins:
        return
    expected = {"source": {"source": "directory", "path": binding.path}}
    files = read_order(surface.harness, deploy_root=deploy_root, project=binding.project)
    clash = foreign_marketplace(files, binding.marketplace, expected)
    if clash is not None:
        raise NativeSettingsError(
            f"{clash} already declares {MARKETPLACES_KEY}.{binding.marketplace} with another "
            "source. APM did not change it. Remove or rename that entry, then retry."
        )


def reconcile_native_plugins(
    *,
    binding: NativeBinding,
    modules_dir: Path,
    deploy_root: Path,
    previous: Mapping[str, NativeOwnership] | None,
    lockfile: Any,
    configured: Collection[str] = (),
    prepared: Mapping[str, Any] | None = None,
    logger: Any = None,
    dry_run: bool = False,
) -> None:
    """Converge deployed roots, the catalog and harness settings onto *lockfile*.

    *previous* is the ownership the lock recorded before this command changed
    it, or ``None`` when there was no previous lock. *configured* names the
    project's targets, which select the harnesses a project-scope plugin is
    declared to. A failure restores every settings file, removes roots this
    run created and raises :class:`NativePluginError`; the caller restores the
    lock.
    """
    if dry_run:
        return
    modules_dir = Path(modules_dir).absolute()
    root = binding.root
    created: list[str] = []
    prior = dict(previous or {})
    prior_names = {own.record.name for own in prior.values()}
    prior_bindings = {own.record.binding for own in prior.values()}
    following = native_ownership(lockfile) if lockfile is not None else {}
    if not following and not prior_names:
        return
    snapshot: _SettingsSnapshot | None = None
    try:
        if prior_bindings and prior_bindings != {binding.pair}:
            raise NativeSettingsError(_moved_message(prior_bindings, binding))
        declared = _declared_plugins(
            following, lockfile, modules_dir, binding, prepared or {}, prior
        )
        surfaces = _surfaces(declared, lockfile, binding, deploy_root, configured)
        for surface in surfaces:
            _refuse_foreign_binding(surface, binding, deploy_root)
        snapshot = _SettingsSnapshot([surface.path for surface in surfaces])
        changed = 0
        for name, (_key, own, source) in declared.items():
            fresh = not (root / name).exists()
            if deploy_plugin(root, name, source, own.record.fingerprint, binding.fingerprint):
                changed += 1
                if fresh:
                    created.append(name)
        write_catalog(root, binding.marketplace, declared, owned=bool(prior_names))
        changes = {
            surface.harness: _declare(surface, binding, previous, prior_names, prior_bindings)
            for surface in surfaces
        }
        retiring = sorted(prior_names - set(declared))
        fingerprints = {own.record.name: own.record.fingerprint for own in prior.values()}
        kept = [
            name
            for name in retiring
            if not retire_plugin(root, name, fingerprints.get(name), binding.fingerprint)
        ]
        prune_empty(root)
    except (NativeLayoutError, NativeSettingsError, OSError) as exc:
        _roll_back(binding, created, prior_names, snapshot)
        raise NativePluginError(f"Native plugin declaration failed: {exc}") from exc
    except NativePluginError:
        _roll_back(binding, created, prior_names, snapshot)
        raise
    _report(logger, binding, surfaces, changes, retiring, kept, changed, deploy_root)


def _declare(
    surface: _Surface,
    binding: NativeBinding,
    previous: Mapping[str, NativeOwnership] | None,
    prior_names: set[str],
    prior_bindings: set[tuple[str, str]],
) -> SettingsChange:
    change = write_declaration(
        surface.path,
        SettingsDeclaration(
            binding.marketplace,
            binding.path,
            surface.plugins,
            None if previous is None else frozenset(prior_names),
            next(iter(prior_bindings)) if prior_bindings else None,
        ),
    )
    if BINDING_MOVED in change.diagnostics:
        raise NativeSettingsError(f"{surface.path}: the marketplace binding moved.")
    if BINDING_CONFLICT in change.diagnostics:
        raise NativeSettingsError(
            f"{surface.path} already declares {MARKETPLACES_KEY}.{binding.marketplace} with "
            "another source. APM did not change it. Remove or rename that entry, then retry."
        )
    return change


def _moved_message(prior_bindings: set[tuple[str, str]], binding: NativeBinding) -> str:
    old = ", ".join(f"{name} at {path}" for name, path in sorted(prior_bindings))
    if binding.project:
        return (
            f"The previous apm.lock.yaml declared native plugins under {old}; this project "
            f"now uses {binding.marketplace} at {binding.path} (the marketplace name follows "
            "the 'name' in apm.yml). APM changed nothing. Restore the previous name, or remove "
            "the old entries from .claude/settings.json and .github/copilot/settings.json and "
            "the native_plugin records from apm.lock.yaml, then run 'apm install' again."
        )
    return (
        f"The previous apm.lock.yaml declared native plugins under {old}; this APM home now "
        f"uses {binding.marketplace} at {binding.path}. APM changed nothing. Remove the old "
        "entries from the harness settings and ~/.agents/plugins, delete apm.lock.yaml, then "
        "run 'apm install --global' again."
    )


def _roll_back(
    binding: NativeBinding,
    created: list[str],
    prior_names: set[str],
    snapshot: _SettingsSnapshot | None,
) -> None:
    """Undo this run's settings, new roots and catalog; prior roots stay owned by the old lock."""
    root = binding.root
    if snapshot is not None:
        snapshot.restore()
    for name in created:
        retire_plugin(root, name, None, binding.fingerprint)
    with contextlib.suppress(NativeLayoutError, OSError):
        write_catalog(
            root,
            binding.marketplace,
            [name for name in prior_names if (root / name).exists()],
            owned=True,
        )
    prune_empty(root)


def _report(logger, binding, surfaces, changes, retiring, kept, changed, deploy_root) -> None:
    if not logger:
        return
    for surface in surfaces:
        if PREVIOUS_LOCK_MISSING in changes[surface.harness].diagnostics:
            logger.warning(
                f"{surface.path} already has entries for marketplace '{binding.marketplace}' "
                "but no previous apm.lock.yaml records them. APM left them in place; review "
                f"them in {HARNESS_LABELS[surface.harness]} and remove any you no longer want."
            )
    if retiring:
        logger.tree_item(f"{len(retiring)} native plugin(s) removed.")
    for name in kept:
        logger.warning(
            f"Kept {binding.label}/{name}: its files changed since APM deployed it. "
            "Review and delete it yourself; no harness loads it any more."
        )
    settings_changed = any(change.changed for change in changes.values())
    declared = [surface for surface in surfaces if surface.plugins]
    if declared and (changed or settings_changed):
        count = len(set().union(*(surface.plugins for surface in declared)))
        where = " and ".join(HARNESS_LABELS[surface.harness] for surface in declared)
        logger.tree_item(
            f"{count} native plugin(s) declared and enabled in {where} settings. "
            "Restart the harness, or reload its plugins, to load them."
        )
    _report_copilot_overrides(logger, binding, surfaces, deploy_root)


def _report_copilot_overrides(logger, binding, surfaces, deploy_root) -> None:
    """Name plugins a higher-precedence file keeps off in Copilot."""
    if not binding.project:
        return
    files = read_order(COPILOT, deploy_root=deploy_root, project=True)
    for name in sorted(set().union(*(surface.plugins for surface in surfaces))):
        value, source = effective_enable(files, f"{name}@{binding.marketplace}")
        if value is False and source is not None:
            logger.verbose_detail(
                f"    Copilot keeps native plugin '{name}' off: "
                f"{source.parent.name}/{source.name} sets it to false."
            )
