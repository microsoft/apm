"""Reconcile MCP and LSP services before native Claude registration (#4329).

Native Claude registration flushes the staged lockfile before it declares
plugins outside APM's transaction. Running the caller's MCP/LSP
reconciliation first makes that flush the install's single lockfile commit,
holding the final service records alongside the native ones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apm_cli.deps.lockfile_write_authority import commits_on_failure

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def run(ctx: InstallContext) -> None:
    """Run ``ctx.reconcile_services`` with the install's pre-commit result.

    An install finalize will fail reconciles nothing, as when services ran
    after the pipeline. A reconciliation failure is kept on the context: the
    pipeline finishes (native registration included when the lockfile
    authority commits on that failure) and the caller raises it.
    """
    reconcile = ctx.reconcile_services
    if reconcile is None:
        return
    from apm_cli.install.outcome import result_from_install_context
    from apm_cli.models.results import InstallDisposition

    from .finalize import kept_overwrites_fail_install

    result = result_from_install_context(ctx)
    if result.disposition is InstallDisposition.FAILED or kept_overwrites_fail_install(ctx):
        return
    # Reconciliation may prompt or print; no Live region may be running.
    if ctx.tui is not None:
        ctx.tui.__exit__()
    try:
        reconcile(result)
    except Exception as error:
        ctx.service_failure = error
        _restore_after_failed_write(ctx, error)


def _restore_after_failed_write(ctx: InstallContext, error: Exception) -> None:
    """A failed MCP or LSP write restores the deploys and the staged lockfile (#4594).

    Reconciliation runs while the lockfile is still staged and the install
    transaction still journals its deploys, so rolling back leaves deployed
    files, ``apm.lock.yaml``, ``apm.yml`` and the MCP configuration written so
    far (journaled at the write seam) as they were. An MCP or LSP collision
    the install preflight could not see keeps the writes that reached disk,
    as documented (#4268).
    """
    if _keeps_writes(error) or ctx.transaction is None:
        return
    ctx.transaction.rollback()


def _keeps_writes(error: BaseException | None) -> bool:
    """A collision preserves the user's entry and keeps the other writes."""
    from apm_cli.install.errors import LSPUserEntryConflictError, MCPNativeConflictError

    return isinstance(error, MCPNativeConflictError | LSPUserEntryConflictError)


def native_registration_allowed(ctx: InstallContext) -> bool:
    """Whether native Claude registration may commit the lockfile now.

    After a failed reconciliation, registration runs only when the lockfile
    authority still commits on that failure (an MCP collision after other
    servers were written), so the committed native records are on disk; a
    refusal discards the staged lockfile and registers nothing.
    """
    failure = ctx.service_failure
    # Any other failure rolled the install back (#4594): nothing to register.
    return failure is None or (_keeps_writes(failure) and commits_on_failure(failure))
