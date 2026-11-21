"""Declare admitted native plugins only after lock and security gates.

One phase converges Claude Code and GitHub Copilot onto the saved lock at
either scope (#3290, #3921).
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from apm_cli.claude_plugins.admission import ClaudeNativePluginError
from apm_cli.claude_plugins.registrar import native_ownership, reconcile_native_plugins
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.deps.lockfile_write_authority import flush_lockfile_writes, remove_lockfile

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def _refuse_retained_loose_claude_files(ctx: InstallContext) -> None:
    """Refuse native activation beside package-owned loose files of its harnesses.

    Stale cleanup keeps edited or undeletable package-owned files. Registering
    the intact plugin next to them would load the same content twice. Only
    paths attributed to a harness that loads the plugin block; other targets'
    projections and unmanaged user files are not that harness's state.
    """
    from apm_cli.install.target_governance import install_governance, is_governed_by_install

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
    owns everything it must retire. MCP/LSP reconciliation already ran (#4329),
    so the restored lock keeps the service records that reached disk.
    """
    if ctx.apm_modules_dir is None:
        return
    _refuse_retained_loose_claude_files(ctx)
    lock_path = get_lockfile_path(ctx.apm_dir)
    previous_owned = (
        None if ctx.existing_lockfile is None else native_ownership(ctx.existing_lockfile)
    )
    if ctx.claude_native_plugins or previous_owned:
        # Claude's declarations never run ahead of the lock they follow.
        flush_lockfile_writes()
    committed = LockFile.read(lock_path)
    try:
        declared = reconcile_native_plugins(
            modules_dir=ctx.apm_modules_dir,
            scope=ctx.scope,
            previous=previous_owned,
            lockfile=committed,
            prepared=ctx.claude_native_plugins,
            deploy_root=ctx.project_root,
            run_targets=frozenset(target.name for target in ctx.targets or ()),
            logger=ctx.logger,
            dry_run=ctx.dry_run,
            rejected=frozenset(ctx.native_rejected_names),
        )
    except ClaudeNativePluginError:
        previous = _with_reconciled_services(ctx.existing_lockfile, committed)
        if previous is not None:
            # The rollback is durable even though this install fails.
            previous.write(lock_path)
            flush_lockfile_writes()
        else:
            remove_lockfile(lock_path)
        raise
    from apm_cli.install.service import InstallService

    if ctx.dry_run or InstallService.frozen_disposition(ctx).withhold_lockfile_writes:
        return
    stamped = _record_declared(LockFile.read(lock_path), declared)
    if stamped is not None:
        stamped.write(lock_path)


def _record_declared(lockfile: LockFile | None, declared) -> LockFile | None:
    """Record beside each native row which harnesses now hold APM's ``true``.

    It is what APM last wrote, so a later run can tell its own values from a
    hand edit (#3921). Returns the lock to write, or ``None`` when nothing
    changed; a frozen install never writes the lock.
    """
    if lockfile is None or not declared:
        return None
    changed = False
    for locked in lockfile.dependencies.values():
        record = locked.native_plugin
        if record is None or record.name not in declared:
            continue
        harnesses = sorted(declared[record.name])
        if locked.native_declared != harnesses:
            locked.native_declared = harnesses
            changed = True
    return lockfile if changed else None


def _with_reconciled_services(previous: LockFile | None, committed: LockFile | None):
    """*previous* carrying the MCP/LSP records *committed* holds for what reached disk."""
    if committed is None or (
        previous is None
        and not committed.mcp_servers
        and not committed.lsp_servers
        and not (committed.mcp_target_servers or committed.lsp_target_servers)
    ):
        return previous
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    rollback = copy.deepcopy(previous) if previous is not None else LockFile()
    rollback.mcp_servers = list(committed.mcp_servers)
    rollback.mcp_configs = copy.deepcopy(committed.mcp_configs)
    rollback.mcp_omissions = copy.deepcopy(committed.mcp_omissions)
    rollback.mcp_config_provenance = copy.deepcopy(committed.mcp_config_provenance)
    rollback.lsp_servers = list(committed.lsp_servers)
    rollback.lsp_configs = copy.deepcopy(committed.lsp_configs)
    rollback.lsp_config_provenance = copy.deepcopy(committed.lsp_config_provenance)
    DeploymentLedgerCodec.replace_mcp_target_servers(
        rollback,
        committed.mcp_target_servers,
        native_hashes=DeploymentLedgerCodec.mcp_native_hashes(committed),
    )
    DeploymentLedgerCodec.replace_lsp_target_servers(rollback, committed.lsp_target_servers)
    return rollback
