"""Declare admitted native Claude plugins only after lock and security gates."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apm_cli.claude_plugins.admission import ClaudeNativePluginError
from apm_cli.claude_plugins.registrar import native_ownership, reconcile_claude_plugins
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.deps.lockfile_write_authority import flush_lockfile_writes, remove_lockfile

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def _refuse_retained_loose_claude_files(ctx: InstallContext) -> None:
    """Refuse native activation beside package-owned loose Claude files.

    Stale cleanup keeps edited or undeletable package-owned files. Registering
    the intact plugin next to them would load the same content twice. Only
    paths attributed to the Claude target block; other targets' projections
    and unmanaged user files are not package-owned Claude state.
    """
    from apm_cli.install.target_governance import install_governance, is_governed_by_install

    plugins = getattr(ctx, "claude_native_plugins", None) or {}
    retained = getattr(ctx, "package_cleanup_retained", None) or {}
    claude = [target for target in ctx.targets or () if target.name == "claude"]
    if not plugins or not retained or not claude:
        return
    prefixes, schemes = install_governance(claude)
    for key in sorted(plugins):
        paths = sorted(
            path
            for path in retained.get(key, {})
            if is_governed_by_install(path, prefixes, schemes)
        )
        if paths:
            raise ClaudeNativePluginError(
                f"Cannot switch {key} to native Claude: package-owned loose Claude files "
                f"were kept because they changed or could not be removed: {', '.join(paths)}. "
                "Move or delete them, then run the install again."
            )


def run(ctx: InstallContext) -> None:
    """Converge Claude onto the saved lock, using the same owner as removal.

    The lock and Claude's declarations are one rollback unit (req-na-004): when
    declaration fails, the previous lock is restored so a retry still owns
    everything it must retire.
    """
    if ctx.apm_modules_dir is None:
        return
    _refuse_retained_loose_claude_files(ctx)
    lock_path = get_lockfile_path(ctx.apm_dir)
    previous = ctx.existing_lockfile
    previous_owned = None if previous is None else native_ownership(previous)
    if ctx.claude_native_plugins or previous_owned:
        # Claude's declarations never run ahead of the lock they follow.
        flush_lockfile_writes()
    try:
        reconcile_claude_plugins(
            modules_dir=ctx.apm_modules_dir,
            scope=ctx.scope,
            previous=previous_owned,
            lockfile=LockFile.read(lock_path),
            prepared=ctx.claude_native_plugins,
            deploy_root=ctx.project_root,
            logger=ctx.logger,
            dry_run=ctx.dry_run,
        )
    except ClaudeNativePluginError:
        if previous is not None:
            # The rollback is durable even though this install fails.
            previous.write(lock_path)
            flush_lockfile_writes()
        else:
            remove_lockfile(lock_path)
        raise
