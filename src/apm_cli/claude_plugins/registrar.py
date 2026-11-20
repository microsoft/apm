"""Claude's native plugin owner (#3191, #3181, #3921).

APM deploys each admitted plugin root intact to ``.agents/plugins/<plugin>/``
(``~/.agents/plugins`` at user scope), generates the directory marketplace
beside it, and declares both through the two documented settings keys. The
harness-neutral reconcile in :mod:`apm_cli.native_plugins.reconcile` does the
work for every native harness; this module adds Claude's legacy-registration
migration and its audit check. Ownership comes only from the ``native_plugin``
records in the previous and next ``apm.lock.yaml``. APM never runs the
``claude`` CLI and keeps no receipt file (OpenAPM req-na-002 to req-na-004
and req-na-007).
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from apm_cli.core.scope import InstallScope, get_deploy_root, is_user_scope
from apm_cli.native_plugins.binding import (
    PROJECT_MARKETPLACE_PATH,
    binding_for,
    project_binding,
    user_marketplace_name,
)
from apm_cli.native_plugins.errors import NativePluginError
from apm_cli.native_plugins.layout import plugins_root
from apm_cli.native_plugins.reconcile import (
    NativeOwnership,
    native_ownership,
    reconcile_native_plugins,
)
from apm_cli.native_plugins.settings import ENABLED_KEY, MARKETPLACES_KEY, read_settings
from apm_cli.utils.atomic_io import atomic_write_text
from apm_cli.utils.path_security import has_symlink_component

from .admission import ClaudeNativePlugin, ClaudeNativePluginError

__all__ = [
    "NativeOwnership",
    "claude_settings_path",
    "marketplace_binding",
    "marketplace_name",
    "native_ownership",
    "reconcile_claude_plugins",
    "verify_claude_native_cache",
]

HARNESS = "claude"
# Earlier, unreleased heads of #3290 drove the claude CLI and kept a ledger.
LEGACY_MARKETPLACE = "apm-claude"
LEGACY_LEDGER_RELATIVE = ".claude-plugin/apm-registration.json"
LEGACY_CATALOG_RELATIVE = ".claude-plugin/marketplace.json"


def marketplace_name(apm_home: Path) -> str:
    """Deterministic user-scope marketplace name, unique per APM home (req-na-007)."""
    return user_marketplace_name(apm_home)


def marketplace_binding(apm_home: Path, deploy_root: Path) -> tuple[str, str]:
    """``(marketplace name, absolute profile root)`` for one user-scope APM home."""
    return marketplace_name(apm_home), str(plugins_root(deploy_root))


def claude_settings_path() -> Path:
    """Claude's user settings document, honoring ``CLAUDE_CONFIG_DIR``."""
    from apm_cli.integration.targets import resolve_user_root

    return resolve_user_root(HARNESS) / "settings.json"


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


def reconcile_claude_plugins(
    *,
    modules_dir: Path,
    scope: Any,
    previous: Mapping[str, NativeOwnership] | None,
    lockfile: Any,
    prepared: Mapping[str, ClaudeNativePlugin] | None = None,
    deploy_root: Path | None = None,
    configured: Collection[str] | None = None,
    manifest_root: Path | None = None,
    logger: Any = None,
    dry_run: bool = False,
) -> None:
    """Converge deployed roots, the catalog and harness settings onto *lockfile*.

    *previous* is the ownership the lock recorded before this command changed
    it, or ``None`` when there was no previous lock. *configured* names the
    project's targets, which choose between ``.claude/settings.json`` and
    ``.github/copilot/settings.json`` at project scope; ``None`` reads them
    from the project. *manifest_root* holds the project's ``apm.yml`` when
    it is not *deploy_root* (``apm install --root``). A failure leaves
    settings unchanged and removes roots this run created; the caller restores
    the lock.
    """
    if dry_run:
        return
    user = is_user_scope(scope)
    modules_dir = Path(modules_dir).absolute()
    deploy_root = Path(
        deploy_root or get_deploy_root(InstallScope.USER if user else InstallScope.PROJECT)
    )
    if user:
        _migrate_legacy_registration(modules_dir, claude_settings_path(), logger)
    if not previous and (lockfile is None or not native_ownership(lockfile)):
        return
    if configured is None:
        from apm_cli.native_plugins.harnesses import configured_harnesses

        configured = () if user else configured_harnesses(manifest_root or deploy_root)
    try:
        reconcile_native_plugins(
            binding=binding_for(
                scope=scope,
                modules_dir=modules_dir,
                deploy_root=deploy_root,
                manifest_root=manifest_root,
            ),
            modules_dir=modules_dir,
            deploy_root=deploy_root,
            previous=previous,
            lockfile=lockfile,
            configured=configured,
            prepared=prepared,
            logger=logger,
        )
    except ClaudeNativePluginError:
        raise
    except NativePluginError as exc:
        raise ClaudeNativePluginError(str(exc)) from exc


def verify_claude_native_cache(
    *, modules_dir: Path, locked: Any, source_root: Path, project_root: Path | None = None
) -> None:
    """Audit: the replay source and the deployed copy both match the lock record."""
    from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError
    from apm_cli.native_plugins.binding import user_binding

    record = locked.native_plugin
    if record is None:
        return
    if record.marketplace_path == PROJECT_MARKETPLACE_PATH and project_root is not None:
        try:
            binding = project_binding(project_root)
        except NativePluginError as exc:
            raise AgentPluginDeploymentBoundaryError(str(exc)) from exc
    else:
        binding = user_binding(
            Path(modules_dir).absolute().parent, get_deploy_root(InstallScope.USER)
        )
    deployed = binding.root / record.name
    for label, path in (("source", source_root), ("deployed copy", deployed)):
        try:
            observed = binding.fingerprint(path) if path.is_dir() else "missing"
        except NativePluginError:
            observed = "unreadable"
        if observed != record.fingerprint:
            restore = "apm install" if binding.project else "apm install --global"
            raise AgentPluginDeploymentBoundaryError(
                f"Native plugin '{record.name}' {label} differs from apm.lock.yaml "
                f"({path}): expected {record.fingerprint}, observed {observed}. "
                f"Run '{restore}' to restore it."
            )
