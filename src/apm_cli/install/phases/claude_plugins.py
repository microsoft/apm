"""Declare admitted native plugins only after lock and security gates.

One phase converges Claude Code and GitHub Copilot onto the saved lock at
either scope (#3290, #3921).
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from apm_cli.claude_plugins.admission import ClaudeNativePluginError
from apm_cli.claude_plugins.registrar import (
    native_ownership,
    plan_native_declarations,
    reconcile_native_plugins,
)
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.deps.lockfile_write_authority import (
    flush_lockfile_writes,
    remove_lockfile,
    restore_lockfile_bytes,
)

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
    # The lock on disk before this command commits, restored verbatim when the
    # rollback carries no service change (#4685).
    original = (
        lock_path.read_bytes()
        if ctx.existing_lockfile is not None and lock_path.is_file()
        else None
    )
    retired = _retired_harnesses(ctx)
    if ctx.claude_native_plugins or previous_owned:
        planned = _planned_declarations(ctx, lock_path, previous_owned, retired)
        if planned is not None:
            planned.write(lock_path)
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
            retired_harnesses=retired,
        )
    except ClaudeNativePluginError:
        previous = _with_reconciled_services(ctx.existing_lockfile, committed)
        if previous is not None and previous is ctx.existing_lockfile and original is not None:
            restore_lockfile_bytes(lock_path, original)
        elif previous is not None:
            # The rollback is durable even though this install fails.
            previous.write(lock_path)
            flush_lockfile_writes()
        else:
            remove_lockfile(lock_path)
        raise
    from apm_cli.install.service import InstallService

    if ctx.dry_run:
        return
    _note_native_route(ctx)
    if InstallService.frozen_disposition(ctx).withhold_lockfile_writes:
        return
    stamped = _record_declared(LockFile.read(lock_path), declared)
    if stamped is not None:
        stamped.write(lock_path)


def _note_native_route(ctx: InstallContext) -> None:
    """Name each dependency this install deployed through the native plugin route."""
    if ctx.diagnostics is None:
        return
    from apm_cli.claude_plugins.admission import has_loose_route, native_route_notice
    from apm_cli.core.scope import is_user_scope

    user_scope = is_user_scope(ctx.scope)
    for package_name, plugin in ctx.claude_native_plugins.items():
        loose_route = has_loose_route(plugin, user_scope=user_scope)
        ctx.diagnostics.warn(native_route_notice(loose_route=loose_route), package=package_name)


def _retired_harnesses(ctx: InstallContext) -> frozenset[str]:
    """Harnesses whose APM enable entries target contraction retires (#4874).

    The same rule that retires a plugin every harness of which is dropped
    (#4744): apm.yml declares targets and neither they nor this run name it.
    """
    from apm_cli.install.phases.targets import declared_target_profiles
    from apm_cli.install.target_governance import retired_native_harnesses

    return retired_native_harnesses(ctx.targets or (), declared_target_profiles(ctx))


def _planned_declarations(
    ctx: InstallContext, lock_path, previous_owned, retired: frozenset[str]
) -> LockFile | None:
    """The lock with the planned ``native_declared``, so the flush commits once (#4329).

    A failed declaration restores the previous lock, so a stamp ahead of the
    settings never survives; a plan that differs from the result is rewritten.
    """
    from apm_cli.install.service import InstallService

    if ctx.dry_run or InstallService.frozen_disposition(ctx).withhold_lockfile_writes:
        return None
    staged = LockFile.read(lock_path)
    planned = plan_native_declarations(
        modules_dir=ctx.apm_modules_dir,
        scope=ctx.scope,
        previous=previous_owned,
        lockfile=staged,
        prepared=ctx.claude_native_plugins,
        deploy_root=ctx.project_root,
        run_targets=frozenset(target.name for target in ctx.targets or ()),
        retired_harnesses=retired,
    )
    return _record_declared(staged, planned)


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
    """*previous* carrying the MCP/LSP records *committed* holds for what reached disk.

    When the install changed no service record, *previous* itself is returned, so
    the caller restores the previous lock's bytes (#4685).
    """
    if committed is None or (
        previous is None
        and not committed.mcp_servers
        and not committed.lsp_servers
        and not (committed.mcp_target_servers or committed.lsp_target_servers)
    ):
        return previous
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    if previous is not None and _service_records(previous) == _service_records(committed):
        return previous

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


def _service_records(lock: LockFile) -> str:
    """The MCP/LSP records the rollback carries, type-sensitive (``1`` is not ``true``)."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.deps.lockfile import _json_identity

    return _json_identity(
        [
            sorted(lock.mcp_servers),
            lock.mcp_configs,
            lock.mcp_omissions,
            lock.mcp_config_provenance,
            sorted(lock.lsp_servers),
            lock.lsp_configs,
            lock.lsp_config_provenance,
            {target: sorted(names) for target, names in lock.mcp_target_servers.items() if names},
            {target: sorted(names) for target, names in lock.lsp_target_servers.items() if names},
            DeploymentLedgerCodec.mcp_native_hashes(lock),
        ]
    )
