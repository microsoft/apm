"""Declare admitted native plugins only after lock and security gates.

One phase converges Claude Code and GitHub Copilot onto the saved lock at
either scope (#3290, #3921).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apm_cli.claude_plugins.admission import ClaudeNativePluginError
from apm_cli.claude_plugins.registrar import native_ownership, reconcile_native_plugins
from apm_cli.deps.lockfile import LockFile, get_lockfile_path

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def _refuse_retained_loose_claude_files(ctx: InstallContext) -> None:
    """Refuse native activation beside package-owned loose files of its harnesses.

    Stale cleanup keeps edited or undeletable package-owned files. Registering
    the intact plugin next to them would load the same content twice. Only
    paths attributed to a harness that loads the plugin block; other targets'
    projections and unmanaged user files are not that harness's state.
    """
    from apm_cli.install.manifest_reconcile import install_governance, is_governed_by_install

    plugins = getattr(ctx, "claude_native_plugins", None) or {}
    retained = getattr(ctx, "package_cleanup_retained", None) or {}
    if not plugins or not retained:
        return
    for key in sorted(plugins):
        native = [t for t in ctx.targets or () if t.name in plugins[key].harnesses]
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


def run(ctx: InstallContext) -> None:
    """Converge native plugins onto the saved lock, using the same owner as removal.

    The lock and the harness declarations are one rollback unit (req-na-004):
    when declaration fails, the previous lock is restored so a retry still
    owns everything it must retire.
    """
    if ctx.apm_modules_dir is None:
        return
    _refuse_retained_loose_claude_files(ctx)
    lock_path = get_lockfile_path(ctx.apm_dir)
    previous = ctx.existing_lockfile
    try:
        declared = reconcile_native_plugins(
            modules_dir=ctx.apm_modules_dir,
            scope=ctx.scope,
            previous=None if previous is None else native_ownership(previous),
            lockfile=LockFile.read(lock_path),
            prepared=ctx.claude_native_plugins,
            deploy_root=ctx.project_root,
            run_targets=frozenset(target.name for target in ctx.targets or ()),
            logger=ctx.logger,
            dry_run=ctx.dry_run,
        )
    except ClaudeNativePluginError:
        if previous is not None:
            previous.write(lock_path)
        else:
            lock_path.unlink(missing_ok=True)
        raise
    if not ctx.dry_run and not getattr(ctx, "frozen", False):
        _record_declared(lock_path, declared)


def _record_declared(lock_path, declared) -> None:
    """Record beside each native row which harnesses now hold APM's ``true``.

    It is what APM last wrote, so a later run can tell its own values from a
    hand edit (#3921). A frozen install never writes the lock.
    """
    lockfile = LockFile.read(lock_path)
    if lockfile is None or not declared:
        return
    changed = False
    for locked in lockfile.dependencies.values():
        record = locked.native_plugin
        if record is None or record.name not in declared:
            continue
        harnesses = sorted(declared[record.name])
        if locked.native_declared != harnesses:
            locked.native_declared = harnesses
            changed = True
    if changed:
        lockfile.write(lock_path)
