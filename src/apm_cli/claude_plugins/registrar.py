"""Single owner of APM's natively activated plugins (#3191, #3181, #3921).

APM deploys each admitted plugin root intact to ``.agents/plugins/<plugin>/``
(``~/.agents/plugins`` at user scope; committed in the project at project
scope), generates the directory marketplace beside it, and declares both
through the two documented settings keys of every harness that loads the
plugin: Claude Code and, at project scope or for Agent Plugins, GitHub
Copilot. Ownership comes only from the ``native_plugin`` records in the
previous and next ``apm.lock.yaml``. APM never runs a vendor CLI and keeps no
receipt file (OpenAPM req-na-002 to req-na-004 and req-na-007).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm_cli.core.remediation import (
    CLAUDE_NATIVE_PLUGIN_DRIFT,
    CLAUDE_PLUGIN_BINDING_MOVED,
    CLAUDE_PLUGIN_SOURCE_STALE,
    kept_note,
)
from apm_cli.core.scope import InstallScope, get_deploy_root, is_user_scope
from apm_cli.native_plugins.claude_marketplaces import (
    release_project_marketplaces,
    sync_project_marketplace,
)
from apm_cli.native_plugins.layout import (
    NativeLayoutError,
    attributes_enabled,
    deploy_plugin,
    discard_replaced,
    plugins_root,
    prune_empty,
    restore_replaced,
    retire_plugin,
    write_attributes,
    write_catalog,
)
from apm_cli.native_plugins.record import NativePluginRecord, lock_native_plugins
from apm_cli.native_plugins.scopes import (
    CLAUDE,
    COPILOT,
    HARNESSES,
    PROJECT_MARKETPLACE_PATH,
    committed_catalog_name,
    project_marketplace_name,
    record_harnesses,
    settings_path,
    user_marketplace_name,
)
from apm_cli.native_plugins.settings import (
    BINDING_CONFLICT,
    BINDING_MOVED,
    ENABLED_KEY,
    MARKETPLACES_KEY,
    PREVIOUS_LOCK_MISSING,
    NativeSettingsError,
    SettingsChange,
    SettingsDeclaration,
    preview_declaration,
    read_settings,
    restore_settings,
    snapshot_settings,
    write_declaration,
)
from apm_cli.utils.content_hash import compute_native_plugin_hash
from apm_cli.utils.paths import portable_relpath

from .admission import (
    ClaudeNativePlugin,
    ClaudeNativePluginError,
    fingerprint_native_claude_source,
    pin_native_claude_source,
)


@dataclass(frozen=True)
class NativeOwnership:
    """One locked dependency's native record and its immutable source pin."""

    record: NativePluginRecord
    pin: str
    local: bool
    harnesses: frozenset[str] = frozenset()
    """Harnesses the row's format, scope and own ``targets:`` allow."""
    declared: frozenset[str] | None = None
    """Harnesses that held APM's ``true`` when the row was written (``native_declared``)."""


_HARNESS_LABELS = {CLAUDE: "Claude Code", COPILOT: "GitHub Copilot"}


def marketplace_name(apm_home: Path) -> str:
    """Deterministic user-scope marketplace name, unique per APM home (req-na-007)."""
    return user_marketplace_name(apm_home)


def marketplace_binding(apm_home: Path, deploy_root: Path) -> tuple[str, str]:
    """``(marketplace name, absolute profile root)`` for one user-scope APM home."""
    return marketplace_name(apm_home), str(plugins_root(deploy_root))


def scope_binding(
    *,
    user_scope: bool,
    modules_dir: Path,
    deploy_root: Path,
    previous: Mapping[str, NativeOwnership] | None,
) -> tuple[str, str]:
    """``(marketplace name, path as written)`` for this scope (req-na-003, req-na-007).

    User scope: a per-APM-home name and the absolute profile root. Project
    scope: a per-project name, reused from the previous lock or the committed
    catalog so every clone agrees, and the relative ``./.agents/plugins``.
    """
    if user_scope:
        return marketplace_binding(Path(modules_dir).parent, deploy_root)
    recorded = next(
        (
            own.record.marketplace
            for own in (previous or {}).values()
            if own.record.marketplace_path == PROJECT_MARKETPLACE_PATH
        ),
        None,
    )
    catalog = committed_catalog_name(plugins_root(deploy_root))
    return project_marketplace_name(deploy_root, recorded, catalog), PROJECT_MARKETPLACE_PATH


def claude_settings_path() -> Path:
    """Claude's user settings document, honoring ``CLAUDE_CONFIG_DIR``."""
    return settings_path(CLAUDE, user_scope=True, project_root=Path.home())


def _fingerprinter(user_scope: bool) -> Callable[[Path], str]:
    """The lock-recorded root digest for this scope.

    Project-scope roots are committed, so their digest covers paths and
    bytes only; a teammate's checkout that drops the exec bit still matches.
    """
    return lambda root: fingerprint_native_claude_source(root, portable=not user_scope)


def _machine_hash(root: Path) -> str:
    """Per-machine digest, exec bit included: deployed copy versus its local source."""
    try:
        return compute_native_plugin_hash(root)
    except (OSError, ValueError) as exc:
        raise ClaudeNativePluginError(f"Cannot fingerprint native plugin {root}: {exc}") from exc


def _source_pin(locked: Any) -> str:
    revision = (
        locked.local_path
        if locked.source == "local"
        else locked.resolved_commit or locked.resolved_hash
    )
    return f"{locked.source}:{revision}" if revision else ""


def native_ownership(lockfile: Any) -> dict[str, NativeOwnership]:
    """Return ``{dependency_key: ownership}`` for Claude records in *lockfile*."""
    owned = {}
    for key, record in lock_native_plugins(lockfile).items():
        locked = lockfile.dependencies[key]
        harnesses = record_harnesses(locked.package_type, record.marketplace_path)
        if locked.target_subset:
            harnesses &= frozenset(locked.target_subset)
        declared = getattr(locked, "native_declared", None)
        owned[key] = NativeOwnership(
            record,
            _source_pin(locked),
            locked.source == "local",
            harnesses,
            None if declared is None else frozenset(declared),
        )
    return owned


def _verify_next(
    key: str,
    own: NativeOwnership,
    lockfile: Any,
    modules_dir: Path,
    prepared: Mapping[str, ClaudeNativePlugin],
    previous: Mapping[str, NativeOwnership],
    fingerprint_of: Callable[[Path], str],
    hint: str,
    deployed: Path,
) -> Path | None:
    """Return the verified ``apm_modules`` source for one declared plugin.

    ``None`` when this run did not admit the plugin and its deployed root
    already carries the locked fingerprint: a carried-forward plugin stays
    exactly as deployed, whatever ``apm_modules`` now holds.
    """
    if key not in prepared and deployed.is_dir():
        with contextlib.suppress(ClaudeNativePluginError):
            if fingerprint_of(deployed) == own.record.fingerprint:
                return None
    locked = lockfile.dependencies[key]
    source = locked.to_dependency_ref().get_install_path(modules_dir)
    if not source.is_dir():
        raise ClaudeNativePluginError(
            f"The source of native plugin '{own.record.name}' ({key}) is missing from "
            "apm_modules; settings were not changed. "
            + CLAUDE_PLUGIN_SOURCE_STALE.with_command(hint).sentence()
        )
    plan = prepared.get(key)
    if plan is not None and plan.bundle_hash != own.record.fingerprint:
        raise ClaudeNativePluginError("Native Claude source changed after deployment admission.")
    prior = previous.get(key)
    if (
        prior is not None
        and not own.local
        and prior.pin == own.pin
        and prior.record.fingerprint != own.record.fingerprint
    ):
        raise ClaudeNativePluginError(
            f"Native Claude plugin '{own.record.name}' ({key}) changed content at the same "
            "pinned revision. APM will not adopt modified publisher content; restore the "
            "pinned package or pin a new revision."
        )
    if fingerprint_of(source) != own.record.fingerprint:
        raise ClaudeNativePluginError(
            f"Native plugin source for {key} does not match its locked fingerprint. "
            f"Restore the pinned package with '{hint}' before retrying."
        )
    return source


class _Rollback:
    """What one reconcile changed before the harness settings committed (req-na-004)."""

    def __init__(
        self, root: Path, settings_paths: Collection[Path], fingerprint_of: Callable[[Path], str]
    ) -> None:
        self.root = root
        self.marketplace = ""
        self.settings_paths = tuple(settings_paths)
        self.fingerprint_of = fingerprint_of
        self.created: list[str] = []
        self.replaced: dict[str, Path] = {}
        self.settings: dict[Path, bytes | None] = {}
        self.attributes = False

    def snapshot(self, extra: Collection[Path] = ()) -> None:
        """Record every settings file this run may write, harness and legacy alike."""
        self.settings = {path: snapshot_settings(path) for path in (*self.settings_paths, *extra)}
        with contextlib.suppress(NativeLayoutError, OSError, UnicodeDecodeError):
            self.attributes = attributes_enabled(self.root)

    def commit(self) -> None:
        """Settings committed: the moved-aside previous roots are no longer needed."""
        for replaced in self.replaced.values():
            with contextlib.suppress(NativeLayoutError, OSError):
                discard_replaced(self.root, replaced)

    def undo(self, prior_names: set[str]) -> None:
        """Restore settings bytes, previous roots and catalog; drop new roots."""
        for path, snapshot in self.settings.items():
            with contextlib.suppress(NativeSettingsError, OSError, UnicodeDecodeError):
                restore_settings(path, snapshot)
        for name in self.created:
            retire_plugin(self.root, name, None, self.fingerprint_of)
        for name, replaced in self.replaced.items():
            with contextlib.suppress(NativeLayoutError, OSError):
                restore_replaced(self.root, name, replaced)
        if self.marketplace:
            with contextlib.suppress(NativeLayoutError, OSError):
                write_catalog(
                    self.root,
                    self.marketplace,
                    [name for name in prior_names if (self.root / name).exists()],
                    owned=True,
                )
            # Restored plugin roots stay byte-pinned: keep git from converting them.
            with contextlib.suppress(NativeLayoutError, OSError):
                write_attributes(self.root, enabled=self.attributes)
        with contextlib.suppress(OSError):
            prune_empty(self.root)


def _refuse_binding(change: SettingsChange, settings_path: Path, marketplace: str) -> None:
    if BINDING_MOVED in change.diagnostics:
        raise NativeSettingsError(f"{settings_path}: the marketplace binding moved.")
    if BINDING_CONFLICT in change.diagnostics:
        raise NativeSettingsError(
            f"{settings_path} already declares {MARKETPLACES_KEY}.{marketplace} with "
            "another source. APM did not change it. Remove or rename that entry, then retry."
        )


def reconcile_native_plugins(
    *,
    modules_dir: Path,
    scope: Any,
    previous: Mapping[str, NativeOwnership] | None,
    lockfile: Any,
    prepared: Mapping[str, ClaudeNativePlugin] | None = None,
    deploy_root: Path | None = None,
    run_targets: Collection[str] | None = None,
    logger: Any = None,
    dry_run: bool = False,
    rejected: Collection[str] = (),
    retired_harnesses: Collection[str] = (),
) -> dict[str, frozenset[str]]:
    """Converge deployed roots, the catalog and harness settings onto *lockfile*.

    Returns ``{plugin name: harnesses whose file now holds APM's true}``, for
    the caller to record as ``native_declared`` beside each lock row.

    *previous* is the ownership the lock recorded before this command changed
    it, or ``None`` when there was no previous lock. *prepared* holds the
    plugins this run admitted, with the harnesses their targets select;
    *run_targets* names the targets this run evaluated; *rejected* names the
    plugins it evaluated for Copilot and refused; *retired_harnesses* names
    the harnesses target contraction retired (#4874). A harness this run did
    not evaluate keeps the declarations its settings already hold, as does a
    locked plugin this run did not admit, unless it is retired. Every harness settings file is
    checked before any root is deployed. A failure up to the settings commit
    restores every settings file and replaced root, and removes roots this
    run created; the caller restores the lock. Retirement runs only after the
    commit and keeps (with a warning) any root it cannot safely remove.
    """
    if dry_run:
        return {}
    user_scope = is_user_scope(scope)
    modules_dir = Path(modules_dir).absolute()
    if user_scope:
        deploy_root = get_deploy_root(InstallScope.USER) if deploy_root is None else deploy_root
    elif deploy_root is None:
        deploy_root = modules_dir.parent
    deploy_root = Path(deploy_root).absolute()
    root = plugins_root(deploy_root)
    fingerprint_of = _fingerprinter(user_scope)
    hint = "apm install --global" if user_scope else "apm install"
    settings_paths = {
        harness: settings_path(harness, user_scope=user_scope, project_root=deploy_root)
        for harness in HARNESSES
    }
    rollback = _Rollback(root, settings_paths.values(), fingerprint_of)
    prior = dict(previous or {})
    prior_names = {own.record.name for own in prior.values()}
    prior_bindings = {own.record.binding for own in prior.values()}
    from apm_cli.copilot_plugins.registrar import plan_legacy_copilot_retirement

    legacy = plan_legacy_copilot_retirement(
        modules_dir=modules_dir, project_root=deploy_root, user_scope=user_scope
    )
    try:
        following = native_ownership(lockfile) if lockfile is not None else {}
        if not following and not prior_names:
            if legacy is not None and legacy.plugins and legacy.plugins <= set(rejected):
                # Every legacy plugin was refused: retire the registration that loads it.
                legacy = _migratable(legacy, {}, run_targets, logger, rejected)
                rollback.snapshot(legacy.settings_files if legacy else ())
                if legacy is not None:
                    legacy.apply()
                    with contextlib.suppress(OSError):
                        legacy.finish(logger)
            else:
                _keep_legacy(legacy, logger, "no native plugin is locked for it")
            # No plugin is declared: an entry a failed uninstall left converges now (#5258).
            release_project_marketplaces(root, user_scope=user_scope, logger=logger)
            return {}
        marketplace, path = scope_binding(
            user_scope=user_scope,
            modules_dir=modules_dir,
            deploy_root=deploy_root,
            previous=previous,
        )
        rollback.marketplace = marketplace
        if prior_bindings and prior_bindings != {(marketplace, path)}:
            raise NativeSettingsError(
                "The previous apm.lock.yaml declared native plugins under "
                + ", ".join(f"{name} at {where}" for name, where in sorted(prior_bindings))
                + f"; this scope now uses {marketplace} at {path}. APM changed nothing. "
                + CLAUDE_PLUGIN_BINDING_MOVED.bind(plugins_dir=_display_root(root, user_scope))
                .with_command(hint)
                .sentence()
            )
        declared = _declared_plugins(
            following, lockfile, modules_dir, prepared or {}, prior, root, fingerprint_of, hint
        )
        harnesses = _declared_harnesses(
            declared,
            lockfile,
            prepared or {},
            settings_paths,
            marketplace,
            run_targets,
            retired_harnesses,
        )
        declarations = _settings_declarations(
            harnesses, previous, settings_paths, marketplace, path, user_scope, retired_harnesses
        )
        for harness, target in settings_paths.items():
            _refuse_binding(preview_declaration(target, declarations[harness]), target, marketplace)
        legacy = _migratable(legacy, harnesses, run_targets, logger, rejected)
        rollback.snapshot(legacy.settings_files if legacy else ())
        if legacy is not None:
            legacy.apply()
        changed = 0
        restored: list[str] = []
        locked_before = {own.record.name: own.record.fingerprint for own in prior.values()}
        for name, (key, own, source) in declared.items():
            if source is None:
                continue
            fresh = not (root / name).exists()
            plan = (prepared or {}).get(key)
            deployment = deploy_plugin(
                root,
                name,
                source,
                _machine_hash(source),
                _machine_hash,
                approved_pin=None if plan is None else plan.approved_pin,
                pin_of=pin_native_claude_source,
            )
            if deployment.changed:
                changed += 1
                if fresh:
                    rollback.created.append(name)
                if deployment.replaced is not None:
                    rollback.replaced[name] = deployment.replaced
                    if locked_before.get(name) == own.record.fingerprint:
                        restored.append(name)
        write_catalog(root, marketplace, declared, owned=bool(prior_names))
        write_attributes(root, enabled=bool(declared) and not user_scope)
        changes = {}
        for harness, target in settings_paths.items():
            changes[harness] = write_declaration(target, declarations[harness])
            _refuse_binding(changes[harness], target, marketplace)
    except (NativeLayoutError, NativeSettingsError, OSError) as exc:
        rollback.undo(prior_names)
        raise ClaudeNativePluginError(f"Native plugin declaration failed: {exc}") from exc
    except ClaudeNativePluginError:
        rollback.undo(prior_names)
        raise
    rollback.commit()
    if legacy is not None:
        with contextlib.suppress(OSError):
            legacy.finish(logger)
    # Claude loads a project plugin's skills and hooks in a first session only
    # from a marketplace its user registry lists (#5176).
    sync_project_marketplace(
        marketplace,
        root,
        user_scope=user_scope,
        claude_declared=any(CLAUDE in on for on in harnesses.values()),
        logger=logger,
    )
    retiring = sorted(prior_names - set(declared))
    fingerprints = {own.record.name: own.record.fingerprint for own in prior.values()}
    kept = [
        name for name in retiring if not _retire(root, name, fingerprints.get(name), fingerprint_of)
    ]
    with contextlib.suppress(OSError):
        prune_empty(root)
    for harness, change in changes.items():
        if logger and PREVIOUS_LOCK_MISSING in change.diagnostics:
            logger.warning(
                f"{settings_paths[harness]} already has entries for marketplace '{marketplace}' "
                "but no previous apm.lock.yaml records them. APM left them in place; review "
                "them and remove any you no longer want."
            )
    _warn_overridden(logger, changes, settings_paths, prior)
    rewritten = {
        harness: _display_path(settings_paths[harness], root.parent.parent, user_scope)
        for harness, change in changes.items()
        if change.changed
    }
    _report(
        logger,
        declared,
        retiring,
        kept,
        changed,
        rewritten,
        _display_root(root, user_scope),
        restored,
    )
    return {name: frozenset(on) for name, on in harnesses.items()}


def _keep_legacy(legacy: Any, logger: Any, reason: str) -> None:
    if legacy is not None and logger is not None:
        logger.warning(legacy.keep_message(reason))


def _migratable(
    legacy: Any,
    harnesses: Mapping[str, frozenset[str]],
    run_targets: Collection[str] | None,
    logger: Any,
    rejected: Collection[str] = (),
) -> Any:
    """The legacy Copilot registration to retire in this transaction, or ``None``.

    Retire it only in a run that evaluated Copilot and declares every plugin it
    enabled from ``.agents/plugins``, or refused it (*rejected*: the gate must
    not leave it loading live ``apm_modules`` bytes); otherwise retiring would
    silently switch plugins off (uninstall, prune, a narrowed --target, an old
    lock). An unreadable ledger is never deleted.
    """
    if legacy is None:
        return None
    if legacy.unreadable_ledger is not None:
        _keep_legacy(legacy, logger, f"its ledger {legacy.unreadable_ledger} cannot be read")
        return None
    if COPILOT not in frozenset(run_targets or ()):
        _keep_legacy(legacy, logger, "this command did not evaluate the copilot target")
        return None
    declared = {name for name, on in harnesses.items() if COPILOT in on}
    missing = legacy.plugins - declared - set(rejected)
    if missing:
        _keep_legacy(legacy, logger, f"{', '.join(sorted(missing))} would no longer be declared")
        return None
    return legacy


def _warn_overridden(logger, changes, settings_paths, prior) -> None:
    """Report an owned Copilot value APM changed that APM did not write.

    What APM last wrote follows the harnesses the previous row records as
    ``native_declared`` (an older row: its targets): ``true`` when they
    included Copilot, ``false`` (suppression) when only Claude. A different
    existing value is a hand edit, and changing it is said once; a value APM
    wrote, even one a narrowed run left behind, changes silently.
    """
    if not logger:
        return
    wrote = {
        own.record.name: own.harnesses if own.declared is None else own.declared
        for own in prior.values()
    }
    for harness, change in changes.items():
        for key, value in change.flipped:
            name = key.rsplit("@", 1)[0]
            previous = wrote.get(name, frozenset())
            expected = True if COPILOT in previous else False if CLAUDE in previous else None
            if expected is not None and expected is (not value):
                continue
            logger.warning(
                f"Set {key} to {str(value).lower()} in {settings_paths[harness]} to follow the "
                f"plugin's targets; it was {str(not value).lower()}. Choose which harness loads "
                "it with the dependency's 'targets:' in apm.yml."
            )


def _retire(
    root: Path, name: str, fingerprint: str | None, fingerprint_of: Callable[[Path], str]
) -> bool:
    """Retire after the settings commit: keep, never raise, when unsure."""
    try:
        return retire_plugin(root, name, fingerprint, fingerprint_of)
    except (NativeLayoutError, ClaudeNativePluginError, OSError):
        return False


def _settings_declarations(
    harnesses: Mapping[str, frozenset[str]],
    previous: Mapping[str, NativeOwnership] | None,
    settings_paths: Mapping[str, Path],
    marketplace: str,
    path: str,
    user_scope: bool,
    retired: Collection[str] = (),
) -> dict[str, SettingsDeclaration]:
    """The declaration each harness settings file converges onto.

    In a harness target contraction *retired* (#4874), APM owns only the
    plugins it wrote there, so a user's own enable entry stays.
    """
    prior = previous or {}
    prior_names = {own.record.name for own in prior.values()}
    prior_bindings = {own.record.binding for own in prior.values()}
    suppressed, managed = _suppression(
        harnesses, prior_names, _observations(settings_paths, marketplace), user_scope
    )
    return {
        harness: SettingsDeclaration(
            marketplace,
            path,
            frozenset(name for name, on in harnesses.items() if harness in on),
            _owned_in(
                harness,
                None if previous is None else frozenset(prior_names),
                prior,
                retired,
                user_scope,
            ),
            next(iter(prior_bindings)) if prior_bindings else None,
            suppressed if harness == COPILOT else frozenset(),
            managed if harness == COPILOT else frozenset(),
            catalog=frozenset(harnesses),
        )
        for harness in HARNESSES
    }


def _owned_in(
    harness: str,
    owned: frozenset[str] | None,
    prior: Mapping[str, NativeOwnership],
    retired: Collection[str],
    user_scope: bool,
) -> frozenset[str] | None:
    """*owned* narrowed, in a harness target contraction retired, to what APM wrote there.

    That is the plugins APM declared to it (``native_declared``; an older row,
    its harnesses), plus at project scope Copilot's value for a plugin
    declared to Claude (#4874), so a user's own enable entry stays.
    """
    if owned is None or harness not in retired:
        return owned
    kept = set()
    for own in prior.values():
        wrote = own.harnesses if own.declared is None else own.declared
        if harness in wrote or (harness == COPILOT and CLAUDE in wrote and not user_scope):
            kept.add(own.record.name)
    return owned & frozenset(kept)


def preview_project_root_retirement(record: NativePluginRecord, deploy_root: Path) -> bool | None:
    """Whether retiring *record* at project scope removes its root; changes nothing.

    ``None`` when the deployed root is absent, ``False`` when retirement would
    keep it (its bytes changed since deploy, or it cannot be fingerprinted),
    as :func:`retire_plugin` decides.
    """
    destination = plugins_root(deploy_root) / record.name
    if not destination.exists():
        return None
    try:
        return _fingerprinter(False)(destination) == record.fingerprint
    except (OSError, ValueError, RuntimeError):
        return False


def preview_project_settings_changes(
    *,
    modules_dir: Path,
    previous: Mapping[str, NativeOwnership] | None,
    lockfile: Any,
    deploy_root: Path,
) -> tuple[str, ...]:
    """Project settings files :func:`reconcile_native_plugins` would change; read-only.

    Plans the same declarations the reconcile writes for *lockfile* (the lock
    after the command) against *previous*, and previews each file with the
    settings owner. Raises the errors the reconcile would raise reading them.
    """
    deploy_root = Path(deploy_root).absolute()
    modules_dir = Path(modules_dir).absolute()
    following = native_ownership(lockfile) if lockfile is not None else {}
    if not following and not previous:
        return ()
    settings_paths = {
        harness: settings_path(harness, user_scope=False, project_root=deploy_root)
        for harness in HARNESSES
    }
    marketplace, path = scope_binding(
        user_scope=False, modules_dir=modules_dir, deploy_root=deploy_root, previous=previous
    )
    declared = {own.record.name: (key, own, None) for key, own in following.items()}
    harnesses = _declared_harnesses(declared, lockfile, {}, settings_paths, marketplace, None)
    declarations = _settings_declarations(
        harnesses, previous, settings_paths, marketplace, path, False
    )
    changed = []
    for harness, target in settings_paths.items():
        change = preview_declaration(target, declarations[harness])
        _refuse_binding(change, target, marketplace)
        if change.changed or change.flipped:
            changed.append(_display_path(target, deploy_root, False))
    return tuple(changed)


def _observed(settings: Path, marketplace: str) -> dict[str, Any]:
    """``{plugin: enable value}`` the settings file already holds under *marketplace*."""
    enabled = read_settings(settings).get(ENABLED_KEY)
    if not isinstance(enabled, dict):
        return {}
    suffix = f"@{marketplace}"
    return {key[: -len(suffix)]: value for key, value in enabled.items() if key.endswith(suffix)}


def _observations(
    settings_paths: Mapping[str, Path], marketplace: str
) -> dict[str, frozenset[str]]:
    """Plugins each harness file declares. A Copilot ``false`` for a plugin the
    Claude file names is APM's cross-target suppression, not a declaration."""
    values = {harness: _observed(target, marketplace) for harness, target in settings_paths.items()}
    claude = values[CLAUDE]
    return {
        CLAUDE: frozenset(claude),
        COPILOT: frozenset(
            name for name, value in values[COPILOT].items() if value is True or name not in claude
        ),
    }


def _suppression(
    harnesses: Mapping[str, frozenset[str]],
    prior_names: Collection[str],
    observed: Mapping[str, frozenset[str]],
    user_scope: bool,
) -> tuple[frozenset[str], frozenset[str]]:
    """``(suppressed, managed)`` for the Copilot project file (#3921).

    Copilot also reads ``.claude/settings.json``, so a plugin declared to
    Claude Code alone is switched off for Copilot with an APM-owned ``false``
    in ``.github/copilot/settings.json``, which Copilot reads later. APM owns
    that key, true or false, for every plugin it declares to Claude, so it
    follows the plugin's targets. User scope needs none: Copilot never reads
    ``~/.claude/settings.json``.
    """
    if user_scope:
        return frozenset(), frozenset()
    suppressed = frozenset(
        name for name, on in harnesses.items() if CLAUDE in on and COPILOT not in on
    )
    managed = frozenset(
        name
        for name in set(harnesses) | set(prior_names)
        if CLAUDE in harnesses.get(name, ()) or name in observed[CLAUDE]
    )
    return suppressed, managed


def plan_native_declarations(
    *,
    modules_dir: Path,
    scope: InstallScope,
    previous: Mapping[str, NativeOwnership] | None,
    lockfile: Any,
    prepared: Mapping[str, ClaudeNativePlugin] | None,
    deploy_root: Path | None,
    run_targets: Collection[str] | None,
    retired_harnesses: Collection[str] = (),
) -> dict[str, frozenset[str]] | None:
    """The harnesses :func:`reconcile_native_plugins` will declare, read-only.

    The install stamps them into the lock before its one commit, so a
    successful run writes the lockfile once (#4329); the reconcile result
    still decides what is recorded. ``None`` when the plan cannot be read.
    """
    following = native_ownership(lockfile) if lockfile is not None else {}
    if not following:
        return None
    user_scope = is_user_scope(scope)
    modules_dir = Path(modules_dir).absolute()
    if user_scope:
        deploy_root = get_deploy_root(InstallScope.USER) if deploy_root is None else deploy_root
    elif deploy_root is None:
        deploy_root = modules_dir.parent
    deploy_root = Path(deploy_root).absolute()
    settings_paths = {
        harness: settings_path(harness, user_scope=user_scope, project_root=deploy_root)
        for harness in HARNESSES
    }
    try:
        marketplace, _path = scope_binding(
            user_scope=user_scope,
            modules_dir=modules_dir,
            deploy_root=deploy_root,
            previous=previous,
        )
        declared = {own.record.name: (key, own, None) for key, own in following.items()}
        return _declared_harnesses(
            declared,
            lockfile,
            prepared or {},
            settings_paths,
            marketplace,
            run_targets,
            retired_harnesses,
        )
    except (NativeLayoutError, NativeSettingsError, OSError, ValueError):
        return None


def _declared_harnesses(
    declared: Mapping[str, tuple[str, NativeOwnership, Path]],
    lockfile: Any,
    prepared: Mapping[str, ClaudeNativePlugin],
    settings_paths: Mapping[str, Path],
    marketplace: str,
    run_targets: Collection[str] | None,
    retired: Collection[str] = (),
) -> dict[str, frozenset[str]]:
    """Which harness files declare each plugin after this run.

    A plugin admitted in this run is declared to the harnesses its targets
    select. A locked plugin this run did not admit keeps the declarations its
    settings already hold, so a run narrowed with --target, an uninstall or a
    prune never retires a harness it did not evaluate; when no settings file
    names it yet, it follows the lock entry's own target subset. A declaration
    never extends past the harnesses the plugin's format and scope allow, nor
    to a harness *retired* by target contraction: apm.yml no longer declares
    it and this run did not target it (#4874).
    """
    observed = _observations(settings_paths, marketplace)
    result: dict[str, frozenset[str]] = {}
    for name, (key, own, _source) in declared.items():
        locked = lockfile.dependencies[key]
        allowed = record_harnesses(
            getattr(locked, "package_type", None), own.record.marketplace_path
        ) - frozenset(retired)
        kept = {
            harness for harness in HARNESSES if name in observed[harness] and harness not in retired
        }
        plan = prepared.get(key)
        if plan is not None:
            # The dependency's own targets: is part of this run's decision, so a
            # harness it excludes counts as evaluated (and retired) here.
            evaluated = frozenset(run_targets or ()) & allowed
            kept = set(plan.harnesses) | {harness for harness in kept if harness not in evaluated}
        elif not kept:
            subset = getattr(locked, "target_subset", None)
            kept = set(allowed if not subset else allowed & set(subset))
        result[name] = frozenset(kept) & allowed
    return result


def _declared_plugins(
    following: Mapping[str, NativeOwnership],
    lockfile: Any,
    modules_dir: Path,
    prepared: Mapping[str, ClaudeNativePlugin],
    prior: Mapping[str, NativeOwnership],
    root: Path,
    fingerprint_of: Callable[[Path], str],
    hint: str,
) -> dict[str, tuple[str, NativeOwnership, Path | None]]:
    """Verify every plugin the next lock activates; refuse collisions."""
    prior_names = {own.record.name for own in prior.values()}
    declared: dict[str, tuple[str, NativeOwnership, Path | None]] = {}
    folded: dict[str, tuple[str, str]] = {}
    for key, own in sorted(following.items()):
        name = own.record.name
        if name.casefold() in folded:
            other, other_key = folded[name.casefold()]
            raise ClaudeNativePluginError(
                f"Native plugin name collision: {key} declares '{name}' and {other_key} "
                f"declares '{other}' (names compared ignoring case). Exclude one of them "
                "from its targets, or rename one plugin."
            )
        folded[name.casefold()] = (name, key)
        source = _verify_next(
            key, own, lockfile, modules_dir, prepared, prior, fingerprint_of, hint, root / name
        )
        existing = root / name
        if (
            name not in prior_names
            and existing.exists()
            and fingerprint_of(existing) != own.record.fingerprint
        ):
            raise ClaudeNativePluginError(
                f"{existing} already exists and the previous apm.lock.yaml does not record "
                "it. Move it aside, then retry; APM never replaces content it did not deploy."
            )
        declared[name] = (key, own, source)
    return declared


def _display_root(root: Path, user_scope: bool) -> str:
    """The plugins root as users name it: ``~/.agents/plugins`` or ``.agents/plugins``."""
    return f"~/{root.parent.name}/{root.name}" if user_scope else f"{root.parent.name}/{root.name}"


def _display_path(path: Path, base: Path, user_scope: bool) -> str:
    """A settings file as users name it: relative to the project, or ``~/`` at user scope."""
    relative = portable_relpath(Path(path), base)
    if Path(relative).is_absolute():
        return relative
    return f"~/{relative}" if user_scope else relative


def _report(
    logger, declared, retiring, kept, changed, settings_changed, root: str, restored=()
) -> None:
    if not logger:
        return
    for name in restored:
        logger.warning(f"Restored {root}/{name}: its files differed from apm.lock.yaml.")
    if retiring:
        logger.tree_item(f"{len(retiring)} native plugin(s) removed.")
    for name in kept:
        logger.warning(
            kept_note(
                f"{root}/{name}",
                "its files changed since APM deployed it or could not be removed",
                "Review and delete it yourself; no harness loads it any more.",
            )
        )
    if declared and (changed or settings_changed):
        files = " and ".join(
            f"{settings_changed[h]} ({_HARNESS_LABELS[h]})"
            for h in HARNESSES
            if h in settings_changed
        )
        where = f" in {files}" if files else ""
        logger.tree_item(
            f"{len(declared)} native plugin(s) deployed to {root}/ and declared{where}. "
            "Start a new session, or run /reload-plugins in Claude, to load them."
        )


def native_deployed_drift(deploy_root: Path, lockfile: Any) -> list[Any]:
    """A ``DeployedHashDrift`` for each locked native plugin whose deployed copy differs.

    The deployed root must carry its record's fingerprint, as audit's drift
    replay checks (:func:`verify_claude_native_cache`). A root that does not
    exist yet is not drift: the install deploys it.
    """
    from apm_cli.policy.ci_checks import DeployedHashDrift

    found = []
    for key, record in lock_native_plugins(lockfile).items():
        path = plugins_root(deploy_root) / record.name
        if not path.exists() and not path.is_symlink():
            continue
        # A committed project-scope root records the portable digest (#3921).
        user_scope = record.marketplace_path != PROJECT_MARKETPLACE_PATH
        remedy = CLAUDE_NATIVE_PLUGIN_DRIFT.with_command(
            "apm install --global" if user_scope else "apm install"
        )
        try:
            observed = (
                fingerprint_native_claude_source(path, portable=not user_scope)
                if path.is_dir() and not path.is_symlink()
                else "unreadable"
            )
        except ClaudeNativePluginError:
            observed = "unreadable"
        if observed != record.fingerprint:
            found.append(
                DeployedHashDrift(
                    owner=key,
                    path=f"native plugin '{record.name}' ({record.root})",
                    expected=record.fingerprint,
                    actual=f"{observed}; {remedy.clause()}",
                )
            )
    return found


def verify_claude_native_cache(
    *, modules_dir: Path, locked: Any, source_root: Path, project_root: Path | None = None
) -> None:
    """Audit: the replay source and the deployed copy both match the lock record.

    A project-scope record (relative marketplace path) names the committed
    root under *project_root* and its path-and-bytes digest.
    """
    from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError

    record = locked.native_plugin
    if record is None:
        return
    user_scope = record.marketplace_path != PROJECT_MARKETPLACE_PATH
    deploy_root = (
        get_deploy_root(InstallScope.USER)
        if user_scope
        else Path(project_root or Path(modules_dir).parent)
    )
    deployed = plugins_root(deploy_root) / record.name
    hint = "apm install --global" if user_scope else "apm install"
    for label, path in (("source", source_root), ("deployed copy", deployed)):
        try:
            observed = (
                fingerprint_native_claude_source(path, portable=not user_scope)
                if path.is_dir()
                else "missing"
            )
        except ClaudeNativePluginError:
            observed = "unreadable"
        if observed != record.fingerprint:
            raise AgentPluginDeploymentBoundaryError(
                f"Native plugin '{record.name}' {label} differs from apm.lock.yaml "
                f"({path}): expected {record.fingerprint}, observed {observed}. "
                + CLAUDE_PLUGIN_SOURCE_STALE.with_command(hint).sentence()
            )
