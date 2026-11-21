"""Single owner of APM's native Claude user plugins (#3191, #3181).

APM deploys each admitted plugin root intact to ``~/.agents/plugins/<plugin>/``,
generates the directory marketplace beside it, and declares both to Claude
through the two documented settings keys. Ownership comes only from the
``native_plugin`` records in the previous and next ``apm.lock.yaml``. APM
never runs the ``claude`` CLI and keeps no receipt file (OpenAPM req-na-002
to req-na-004 and req-na-007).
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Mapping
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
from apm_cli.native_plugins.layout import (
    NativeLayoutError,
    deploy_plugin,
    discard_replaced,
    plugins_root,
    prune_empty,
    restore_replaced,
    retire_plugin,
    write_catalog,
)
from apm_cli.native_plugins.record import NativePluginRecord, lock_native_plugins
from apm_cli.native_plugins.settings import (
    BINDING_CONFLICT,
    BINDING_MOVED,
    MARKETPLACES_KEY,
    PREVIOUS_LOCK_MISSING,
    NativeSettingsError,
    SettingsChange,
    SettingsDeclaration,
    preview_declaration,
    restore_settings,
    snapshot_settings,
    write_declaration,
)

from .admission import (
    ClaudeNativePlugin,
    ClaudeNativePluginError,
    fingerprint_native_claude_source,
    pin_native_claude_source,
)

HARNESS = "claude"


@dataclass(frozen=True)
class NativeOwnership:
    """One locked dependency's native record and its immutable source pin."""

    record: NativePluginRecord
    pin: str
    local: bool


def marketplace_name(apm_home: Path) -> str:
    """Deterministic user-scope marketplace name, unique per APM home (req-na-007)."""
    digest = hashlib.sha256(str(Path(apm_home).resolve()).encode("utf-8")).hexdigest()
    return f"apm-u-{digest[:8]}"


def marketplace_binding(apm_home: Path, deploy_root: Path) -> tuple[str, str]:
    """``(marketplace name, absolute profile root)`` for one user-scope APM home."""
    return marketplace_name(apm_home), str(plugins_root(deploy_root))


def claude_settings_path() -> Path:
    """Claude's user settings document, honoring ``CLAUDE_CONFIG_DIR``."""
    from apm_cli.integration.targets import resolve_user_root

    return resolve_user_root(HARNESS) / "settings.json"


def _source_pin(locked: Any) -> str:
    revision = (
        locked.local_path
        if locked.source == "local"
        else locked.resolved_commit or locked.resolved_hash
    )
    return f"{locked.source}:{revision}" if revision else ""


def native_ownership(lockfile: Any) -> dict[str, NativeOwnership]:
    """Return ``{dependency_key: ownership}`` for Claude records in *lockfile*."""
    return {
        key: NativeOwnership(
            record,
            _source_pin(lockfile.dependencies[key]),
            lockfile.dependencies[key].source == "local",
        )
        for key, record in lock_native_plugins(lockfile).items()
    }


def _verify_next(
    key: str,
    own: NativeOwnership,
    lockfile: Any,
    modules_dir: Path,
    prepared: Mapping[str, ClaudeNativePlugin],
    previous: Mapping[str, NativeOwnership],
) -> Path:
    """Return the verified ``apm_modules`` source for one declared plugin."""
    locked = lockfile.dependencies[key]
    source = locked.to_dependency_ref().get_install_path(modules_dir)
    if not source.is_dir():
        raise ClaudeNativePluginError(
            f"The source of native Claude plugin '{own.record.name}' ({key}) is missing from "
            "apm_modules; Claude settings were not changed. "
            + CLAUDE_PLUGIN_SOURCE_STALE.sentence()
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
    if fingerprint_native_claude_source(source) != own.record.fingerprint:
        raise ClaudeNativePluginError(
            f"Native Claude source for {key} does not match its locked fingerprint. "
            "Restore the pinned package with 'apm install --global' before retrying."
        )
    return source


class _Rollback:
    """What one reconcile changed before Claude settings committed (req-na-004)."""

    def __init__(self, root: Path, marketplace: str, settings_path: Path) -> None:
        self.root = root
        self.marketplace = marketplace
        self.settings_path = settings_path
        self.created: list[str] = []
        self.replaced: dict[str, Path] = {}
        self.settings: bytes | None = None
        self.snapshotted = False

    def snapshot(self) -> None:
        self.settings = snapshot_settings(self.settings_path)
        self.snapshotted = True

    def commit(self) -> None:
        """Settings committed: the moved-aside previous roots are no longer needed."""
        for replaced in self.replaced.values():
            with contextlib.suppress(NativeLayoutError, OSError):
                discard_replaced(self.root, replaced)

    def undo(self, prior_names: set[str]) -> None:
        """Restore settings bytes, previous roots and catalog; drop new roots."""
        if self.snapshotted:
            with contextlib.suppress(NativeSettingsError, OSError, UnicodeDecodeError):
                restore_settings(self.settings_path, self.settings)
        for name in self.created:
            retire_plugin(self.root, name, None, fingerprint_native_claude_source)
        for name, replaced in self.replaced.items():
            with contextlib.suppress(NativeLayoutError, OSError):
                restore_replaced(self.root, name, replaced)
        with contextlib.suppress(NativeLayoutError, OSError):
            write_catalog(
                self.root,
                self.marketplace,
                [name for name in prior_names if (self.root / name).exists()],
                owned=True,
            )
        prune_empty(self.root)


def _refuse_binding(change: SettingsChange, settings_path: Path, marketplace: str) -> None:
    if BINDING_MOVED in change.diagnostics:
        raise NativeSettingsError(f"{settings_path}: the marketplace binding moved.")
    if BINDING_CONFLICT in change.diagnostics:
        raise NativeSettingsError(
            f"{settings_path} already declares {MARKETPLACES_KEY}.{marketplace} with "
            "another source. APM did not change it. Remove or rename that entry, then retry."
        )


def reconcile_claude_plugins(
    *,
    modules_dir: Path,
    scope: Any,
    previous: Mapping[str, NativeOwnership] | None,
    lockfile: Any,
    prepared: Mapping[str, ClaudeNativePlugin] | None = None,
    deploy_root: Path | None = None,
    logger: Any = None,
    dry_run: bool = False,
) -> None:
    """Converge deployed roots, the catalog and Claude settings onto *lockfile*.

    *previous* is the ownership the lock recorded before this command changed
    it, or ``None`` when there was no previous lock. Settings are checked
    before any root is deployed. A failure up to the settings commit restores
    settings bytes and every replaced root, and removes roots this run
    created; the caller restores the lock. Retirement runs only after the
    commit and keeps (with a warning) any root it cannot safely remove.
    """
    if not is_user_scope(scope) or dry_run:
        return
    modules_dir = Path(modules_dir).absolute()
    root = plugins_root(deploy_root or get_deploy_root(InstallScope.USER))
    settings_path = claude_settings_path()
    marketplace = marketplace_name(modules_dir.parent)
    binding = (marketplace, str(root))
    rollback = _Rollback(root, marketplace, settings_path)
    prior = dict(previous or {})
    prior_names = {own.record.name for own in prior.values()}
    prior_bindings = {own.record.binding for own in prior.values()}
    try:
        if prior_bindings and prior_bindings != {binding}:
            raise NativeSettingsError(
                "The previous apm.lock.yaml declared native Claude plugins under "
                + ", ".join(f"{name} at {path}" for name, path in sorted(prior_bindings))
                + f"; this APM home now uses {marketplace} at {root}. APM changed nothing. "
                + CLAUDE_PLUGIN_BINDING_MOVED.sentence()
            )
        following = native_ownership(lockfile) if lockfile is not None else {}
        if not following and not prior_names:
            return
        declared = _declared_plugins(following, lockfile, modules_dir, prepared or {}, prior, root)
        declaration = SettingsDeclaration(
            marketplace,
            root,
            frozenset(declared),
            None if previous is None else frozenset(prior_names),
            next(iter(prior_bindings)) if prior_bindings else None,
        )
        _refuse_binding(preview_declaration(settings_path, declaration), settings_path, marketplace)
        rollback.snapshot()
        changed = 0
        restored: list[str] = []
        locked_before = {own.record.name: own.record.fingerprint for own in prior.values()}
        for name, (key, own, source) in declared.items():
            fresh = not (root / name).exists()
            plan = (prepared or {}).get(key)
            deployment = deploy_plugin(
                root,
                name,
                source,
                own.record.fingerprint,
                fingerprint_native_claude_source,
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
        change = write_declaration(settings_path, declaration)
        _refuse_binding(change, settings_path, marketplace)
    except (NativeLayoutError, NativeSettingsError, OSError) as exc:
        rollback.undo(prior_names)
        raise ClaudeNativePluginError(f"Claude native plugin declaration failed: {exc}") from exc
    except ClaudeNativePluginError:
        rollback.undo(prior_names)
        raise
    rollback.commit()
    retiring = sorted(prior_names - set(declared))
    fingerprints = {own.record.name: own.record.fingerprint for own in prior.values()}
    kept = [name for name in retiring if not _retire(root, name, fingerprints.get(name))]
    with contextlib.suppress(OSError):
        prune_empty(root)
    if logger and PREVIOUS_LOCK_MISSING in change.diagnostics:
        logger.warning(
            f"{settings_path} already has entries for marketplace '{marketplace}' but no "
            "previous apm.lock.yaml records them. APM left them in place; review them in "
            "Claude and remove any you no longer want."
        )
    _report(logger, declared, retiring, kept, changed, change.changed, restored)


def _retire(root: Path, name: str, fingerprint: str | None) -> bool:
    """Retire after the settings commit: keep, never raise, when unsure."""
    try:
        return retire_plugin(root, name, fingerprint, fingerprint_native_claude_source)
    except (NativeLayoutError, ClaudeNativePluginError, OSError):
        return False


def _declared_plugins(
    following: Mapping[str, NativeOwnership],
    lockfile: Any,
    modules_dir: Path,
    prepared: Mapping[str, ClaudeNativePlugin],
    prior: Mapping[str, NativeOwnership],
    root: Path,
) -> dict[str, tuple[str, NativeOwnership, Path]]:
    """Verify every plugin the next lock activates; refuse collisions."""
    prior_names = {own.record.name for own in prior.values()}
    declared: dict[str, tuple[str, NativeOwnership, Path]] = {}
    folded: dict[str, str] = {}
    for key, own in sorted(following.items()):
        name = own.record.name
        if name.casefold() in folded:
            raise ClaudeNativePluginError(
                f"Native Claude plugin name collision: '{name}' ({key}) and "
                f"'{folded[name.casefold()]}' differ only by case. Exclude Claude from one."
            )
        folded[name.casefold()] = name
        source = _verify_next(key, own, lockfile, modules_dir, prepared, prior)
        existing = root / name
        if (
            name not in prior_names
            and existing.exists()
            and fingerprint_native_claude_source(existing) != own.record.fingerprint
        ):
            raise ClaudeNativePluginError(
                f"{existing} already exists and the previous apm.lock.yaml does not record "
                "it. Move it aside, then retry; APM never replaces content it did not deploy."
            )
        declared[name] = (key, own, source)
    return declared


def _report(logger, declared, retiring, kept, changed, settings_changed, restored=()) -> None:
    if not logger:
        return
    for name in restored:
        logger.warning(f"Restored ~/.agents/plugins/{name}: its files differed from apm.lock.yaml.")
    if retiring:
        logger.tree_item(f"{len(retiring)} native Claude plugin(s) removed.")
    for name in kept:
        logger.warning(
            kept_note(
                f"~/.agents/plugins/{name}",
                "its files changed since APM deployed it or could not be removed",
                "Review and delete it yourself; Claude no longer loads it.",
            )
        )
    if declared and (changed or settings_changed):
        logger.tree_item(
            f"{len(declared)} native Claude plugin(s) declared and enabled in Claude settings. "
            "Restart Claude, or run /reload-plugins, to load them."
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
        try:
            observed = (
                fingerprint_native_claude_source(path)
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
                    actual=f"{observed}; {CLAUDE_NATIVE_PLUGIN_DRIFT.clause()}",
                )
            )
    return found


def verify_claude_native_cache(*, modules_dir: Path, locked: Any, source_root: Path) -> None:
    """Audit: the replay source and the deployed copy both match the lock record."""
    from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError

    record = locked.native_plugin
    if record is None:
        return
    deployed = plugins_root(get_deploy_root(InstallScope.USER)) / record.name
    for label, path in (("source", source_root), ("deployed copy", deployed)):
        try:
            observed = fingerprint_native_claude_source(path) if path.is_dir() else "missing"
        except ClaudeNativePluginError:
            observed = "unreadable"
        if observed != record.fingerprint:
            raise AgentPluginDeploymentBoundaryError(
                f"Native Claude plugin '{record.name}' {label} differs from apm.lock.yaml "
                f"({path}): expected {record.fingerprint}, observed {observed}. "
                + CLAUDE_PLUGIN_SOURCE_STALE.sentence()
            )
