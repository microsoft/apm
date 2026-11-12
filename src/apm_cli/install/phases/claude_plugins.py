"""Publish admitted native Claude plugins only after lock and security gates."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apm_cli.claude_plugins.admission import ClaudeNativePluginError
from apm_cli.claude_plugins.registrar import resync_claude_plugins
from apm_cli.deps.lockfile import LockFile, get_lockfile_path

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
    """Use the same reconciliation owner as removal, with scanned new plans."""
    if ctx.apm_modules_dir is None:
        return
    from apm_cli.install.phases.targets import declared_target_profiles

    _refuse_retained_loose_claude_files(ctx)
    declared = declared_target_profiles(ctx)
    resync_claude_plugins(
        modules_dir=ctx.apm_modules_dir,
        scope=ctx.scope,
        lockfile=LockFile.read(get_lockfile_path(ctx.apm_dir)),
        targets=ctx.targets,
        retained_targets=None if declared is None else list(declared),
        prepared=ctx.claude_native_plugins,
        allow_executables=ctx.exec_allow_map,
        logger=ctx.logger,
        dry_run=ctx.dry_run,
    )
