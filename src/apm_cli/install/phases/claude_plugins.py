"""Declare admitted native Claude plugins only after lock and security gates."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.claude_plugins.admission import ClaudeNativePluginError
from apm_cli.claude_plugins.registrar import native_ownership, reconcile_claude_plugins
from apm_cli.core.scope import is_user_scope
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.native_plugins.errors import NativePluginError

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def _refuse_retained_loose_claude_files(ctx: InstallContext) -> None:
    """Refuse native activation beside package-owned loose Claude files.

    Stale cleanup keeps edited or undeletable package-owned files. Registering
    the intact plugin next to them would load the same content twice. Only
    paths attributed to the Claude target block; other targets' projections
    and unmanaged user files are not package-owned Claude state.
    """
    from apm_cli.install.manifest_reconcile import install_governance, is_governed_by_install

    plugins = getattr(ctx, "claude_native_plugins", None) or {}
    retained = getattr(ctx, "package_cleanup_retained", None) or {}
    if not plugins or not retained:
        return
    for key, plugin in sorted(plugins.items()):
        native = [target for target in ctx.targets or () if target.name in plugin.harnesses]
        if not native:
            continue
        prefixes, schemes = install_governance(native)
        paths = sorted(
            path
            for path in retained.get(key, {})
            if is_governed_by_install(path, prefixes, schemes)
        )
        if paths:
            raise ClaudeNativePluginError(
                f"Cannot switch {key} to a native plugin: package-owned loose files "
                f"were kept because they changed or could not be removed: {', '.join(paths)}. "
                "Move or delete them, then run the install again."
            )


def manifest_root_of(ctx: InstallContext) -> Path:
    """Directory of the consumer ``apm.yml``; differs from the deploy root under ``--root``."""
    package_path = getattr(getattr(ctx, "apm_package", None), "package_path", None)
    return Path(package_path) if package_path is not None else Path(ctx.project_root)


def run(ctx: InstallContext) -> None:
    """Converge native harnesses onto the saved lock, using the same owner as removal.

    The lock and Claude's declarations are one rollback unit (req-na-004): when
    declaration fails, the previous lock is restored so a retry still owns
    everything it must retire.
    """
    if ctx.apm_modules_dir is None:
        return
    _refuse_retained_loose_claude_files(ctx)
    lock_path = get_lockfile_path(ctx.apm_dir)
    previous = ctx.existing_lockfile
    configured: frozenset[str] | None = None
    manifest_root = manifest_root_of(ctx)
    if not is_user_scope(ctx.scope):
        from apm_cli.native_plugins.harnesses import configured_harnesses

        configured = configured_harnesses(manifest_root, extra=ctx.targets or ())
    try:
        reconcile_claude_plugins(
            modules_dir=ctx.apm_modules_dir,
            scope=ctx.scope,
            previous=None if previous is None else native_ownership(previous),
            lockfile=LockFile.read(lock_path),
            prepared=ctx.claude_native_plugins,
            deploy_root=ctx.project_root,
            configured=configured,
            manifest_root=manifest_root,
            logger=ctx.logger,
            dry_run=ctx.dry_run,
        )
    except NativePluginError:
        if previous is not None:
            previous.write(lock_path)
        else:
            lock_path.unlink(missing_ok=True)
        raise
