"""Claude's native plugin owner (#3191, #3181, #3921).

APM deploys each admitted plugin root intact to ``.agents/plugins/<plugin>/``
(``~/.agents/plugins`` at user scope), generates the directory marketplace
beside it, and declares both through the two documented settings keys. The
harness-neutral reconcile in :mod:`apm_cli.native_plugins.reconcile` does that
for every native harness; this module keeps Claude's entry point and its
audit check. Ownership comes only from the ``native_plugin`` records in the
previous and next ``apm.lock.yaml``. APM never runs the ``claude`` CLI and
keeps no receipt file (OpenAPM req-na-002 to req-na-004 and req-na-007).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from apm_cli.core.scope import InstallScope, get_deploy_root, is_user_scope
from apm_cli.native_plugins.binding import (
    PROJECT_MARKETPLACE_PATH,
    binding_for,
    user_marketplace_name,
)
from apm_cli.native_plugins.errors import NativePluginError
from apm_cli.native_plugins.layout import plugins_root
from apm_cli.native_plugins.reconcile import (
    NativeOwnership,
    native_ownership,
    reconcile_native_plugins,
)

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
    it is not *deploy_root* (``apm install --root``). A failure up to the
    settings commit restores settings and replaced roots and removes roots
    this run created; the caller restores the lock.
    """
    if dry_run:
        return
    user = is_user_scope(scope)
    modules_dir = Path(modules_dir).absolute()
    deploy_root = Path(
        deploy_root or get_deploy_root(InstallScope.USER if user else InstallScope.PROJECT)
    )
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


def verify_claude_native_cache(*, modules_dir: Path, locked: Any, source_root: Path) -> None:
    """Audit: the replay source and the deployed copy both match the lock record.

    A project-scope record's root sits beside the project's ``apm_modules``;
    a user-scope record's under the user deploy root.
    """
    from apm_cli.agent_plugins.errors import AgentPluginDeploymentBoundaryError
    from apm_cli.native_plugins.binding import native_fingerprint

    record = locked.native_plugin
    if record is None:
        return
    project = record.marketplace_path == PROJECT_MARKETPLACE_PATH
    deploy_root = (
        Path(modules_dir).absolute().parent if project else get_deploy_root(InstallScope.USER)
    )
    deployed = plugins_root(deploy_root) / record.name
    for label, path in (("source", source_root), ("deployed copy", deployed)):
        try:
            observed = native_fingerprint(path, project=project) if path.is_dir() else "missing"
        except NativePluginError:
            observed = "unreadable"
        if observed != record.fingerprint:
            restore = "apm install" if project else "apm install --global"
            raise AgentPluginDeploymentBoundaryError(
                f"Native plugin '{record.name}' {label} differs from apm.lock.yaml "
                f"({path}): expected {record.fingerprint}, observed {observed}. "
                f"Run '{restore}' to restore it."
            )
