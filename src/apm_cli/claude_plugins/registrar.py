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
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm_cli.core.scope import InstallScope, get_deploy_root, is_user_scope
from apm_cli.native_plugins.layout import (
    NativeLayoutError,
    deploy_plugin,
    plugins_root,
    prune_empty,
    retire_plugin,
    write_catalog,
)
from apm_cli.native_plugins.record import NativePluginRecord, lock_native_plugins
from apm_cli.native_plugins.settings import (
    BINDING_CONFLICT,
    BINDING_MOVED,
    ENABLED_KEY,
    MARKETPLACES_KEY,
    PREVIOUS_LOCK_MISSING,
    NativeSettingsError,
    SettingsDeclaration,
    read_settings,
    write_declaration,
)
from apm_cli.utils.atomic_io import atomic_write_text
from apm_cli.utils.path_security import has_symlink_component

from .admission import ClaudeNativePlugin, ClaudeNativePluginError, fingerprint_native_claude_source

HARNESS = "claude"
# Earlier, unreleased heads of #3290 drove the claude CLI and kept a ledger.
LEGACY_MARKETPLACE = "apm-claude"
LEGACY_LEDGER_RELATIVE = ".claude-plugin/apm-registration.json"
LEGACY_CATALOG_RELATIVE = ".claude-plugin/marketplace.json"


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
        for key, record in lock_native_plugins(lockfile, HARNESS).items()
    }


def _migrate_legacy_registration(modules_dir: Path, settings_path: Path, logger: Any) -> None:
    """Remove registrations made by earlier heads of this route, when provably theirs."""
    ledger = modules_dir / LEGACY_LEDGER_RELATIVE
    if not ledger.is_file() or has_symlink_component(modules_dir, ledger):
        return
    try:
        plugins = json.loads(ledger.read_text(encoding="utf-8")).get("plugins") or {}
        owned = {key for key in plugins if isinstance(key, str)}
    except (OSError, ValueError, AttributeError):
        owned = set()
    document = read_settings(settings_path)
    marketplaces = document.get(MARKETPLACES_KEY)
    enabled = document.get(ENABLED_KEY)
    changed = False
    if isinstance(marketplaces, dict):
        legacy = marketplaces.get(LEGACY_MARKETPLACE)
        path = legacy.get("source", {}).get("path") if isinstance(legacy, dict) else None
        if isinstance(path, str) and Path(path).resolve() == modules_dir.resolve():
            del marketplaces[LEGACY_MARKETPLACE]
            changed = True
        if not marketplaces:
            document.pop(MARKETPLACES_KEY, None)
    if isinstance(enabled, dict):
        for key in sorted(owned & set(enabled)):
            if key.endswith("@" + LEGACY_MARKETPLACE):
                del enabled[key]
                changed = True
        if not enabled:
            document.pop(ENABLED_KEY, None)
    if changed:
        atomic_write_text(settings_path, json.dumps(document, indent=2) + "\n")
    ledger.unlink()
    catalog = modules_dir / LEGACY_CATALOG_RELATIVE
    if catalog.is_file():
        catalog.unlink()
    if logger:
        logger.tree_item("Removed the Claude registration left by an earlier APM build.")


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
            "apm_modules. Run 'apm install --global' to restore it; Claude settings were not "
            "changed."
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
    it, or ``None`` when there was no previous lock. A failure leaves settings
    unchanged and removes roots this run created; the caller restores the lock.
    """
    if not is_user_scope(scope) or dry_run:
        return
    modules_dir = Path(modules_dir).absolute()
    root = plugins_root(deploy_root or get_deploy_root(InstallScope.USER))
    settings_path = claude_settings_path()
    marketplace = marketplace_name(modules_dir.parent)
    binding = (marketplace, str(root))
    created: list[str] = []
    prior = dict(previous or {})
    prior_names = {own.record.name for own in prior.values()}
    prior_bindings = {own.record.binding for own in prior.values()}
    try:
        if prior_bindings and prior_bindings != {binding}:
            raise NativeSettingsError(
                "The previous apm.lock.yaml declared native Claude plugins under "
                + ", ".join(f"{name} at {path}" for name, path in sorted(prior_bindings))
                + f"; this APM home now uses {marketplace} at {root}. APM changed nothing. "
                "Remove the old entries from Claude settings and ~/.agents/plugins, delete "
                "apm.lock.yaml, then run 'apm install --global' again."
            )
        _migrate_legacy_registration(modules_dir, settings_path, logger)
        following = native_ownership(lockfile) if lockfile is not None else {}
        if not following and not prior_names:
            return
        declared = _declared_plugins(following, lockfile, modules_dir, prepared or {}, prior, root)
        changed = 0
        for name, (_key, own, source) in declared.items():
            fresh = not (root / name).exists()
            if deploy_plugin(
                root, name, source, own.record.fingerprint, fingerprint_native_claude_source
            ):
                changed += 1
                if fresh:
                    created.append(name)
        write_catalog(root, marketplace, declared, owned=bool(prior_names))
        change = write_declaration(
            settings_path,
            SettingsDeclaration(
                marketplace,
                root,
                frozenset(declared),
                None if previous is None else frozenset(prior_names),
                next(iter(prior_bindings)) if prior_bindings else None,
            ),
        )
        if BINDING_MOVED in change.diagnostics:
            raise NativeSettingsError(f"{settings_path}: the marketplace binding moved.")
        if BINDING_CONFLICT in change.diagnostics:
            raise NativeSettingsError(
                f"{settings_path} already declares {MARKETPLACES_KEY}.{marketplace} with "
                "another source. APM did not change it. Remove or rename that entry, then retry."
            )
        retiring = sorted(prior_names - set(declared))
        fingerprints = {own.record.name: own.record.fingerprint for own in prior.values()}
        kept = [
            name
            for name in retiring
            if not retire_plugin(
                root, name, fingerprints.get(name), fingerprint_native_claude_source
            )
        ]
        prune_empty(root)
    except (NativeLayoutError, NativeSettingsError, OSError) as exc:
        _roll_back(root, marketplace, created, prior_names)
        raise ClaudeNativePluginError(f"Claude native plugin declaration failed: {exc}") from exc
    except ClaudeNativePluginError:
        _roll_back(root, marketplace, created, prior_names)
        raise
    if logger and PREVIOUS_LOCK_MISSING in change.diagnostics:
        logger.warning(
            f"{settings_path} already has entries for marketplace '{marketplace}' but no "
            "previous apm.lock.yaml records them. APM left them in place; review them in "
            "Claude and remove any you no longer want."
        )
    _report(logger, declared, retiring, kept, changed, change.changed)


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


def _roll_back(root: Path, marketplace: str, created: list[str], prior_names: set[str]) -> None:
    """Undo this run's new roots and catalog; prior roots stay owned by the old lock."""
    for name in created:
        retire_plugin(root, name, None, fingerprint_native_claude_source)
    with contextlib.suppress(NativeLayoutError, OSError):
        write_catalog(
            root, marketplace, [name for name in prior_names if (root / name).exists()], owned=True
        )
    prune_empty(root)


def _report(logger, declared, retiring, kept, changed, settings_changed) -> None:
    if not logger:
        return
    if retiring:
        logger.tree_item(f"{len(retiring)} native Claude plugin(s) removed.")
    for name in kept:
        logger.warning(
            f"Kept ~/.agents/plugins/{name}: its files changed since APM deployed it. "
            "Review and delete it yourself; Claude no longer loads it."
        )
    if declared and (changed or settings_changed):
        logger.tree_item(
            f"{len(declared)} native Claude plugin(s) declared and enabled in Claude settings. "
            "Restart Claude, or run /reload-plugins, to load them."
        )


def verify_claude_native_cache(*, modules_dir: Path, locked: Any, source_root: Path) -> None:
    """Audit: the replay source and the deployed copy both match the lock record."""
    from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError

    record = locked.native_plugin
    if record is None or HARNESS not in record.harnesses:
        return
    deployed = plugins_root(get_deploy_root(InstallScope.USER)) / record.name
    for label, path in (("source", source_root), ("deployed copy", deployed)):
        try:
            matches = path.is_dir() and fingerprint_native_claude_source(path) == record.fingerprint
        except ClaudeNativePluginError:
            matches = False
        if not matches:
            raise AgentPluginDeploymentBoundaryError(
                f"Native Claude plugin '{record.name}' {label} differs from apm.lock.yaml "
                f"({path}). Run 'apm install --global' to restore it."
            )
