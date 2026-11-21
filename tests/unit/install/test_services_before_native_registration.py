"""MCP/LSP reconciliation runs before native Claude registration commits the lockfile (#4329).

Native registration flushes the staged lockfile before it declares plugins,
so services that reconcile after it stage a second write. These tests drive
the real lockfile write authority and the real ``claude_plugins`` phase; only
the Claude registrar (the harness boundary) is replaced.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.deps.lockfile_write_authority import LockfileWriter, lockfile_write_scope
from apm_cli.install.context import InstallContext
from apm_cli.install.errors import InstallFailureAlreadyRendered, PolicyViolationError
from apm_cli.install.phases import claude_plugins, services
from apm_cli.models.results import InstallDisposition
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-INV-I1", "P-LAW-idempotency.byte_stable"),
]

_SERVER = "native-srv"


def _ctx(tmp_path: Path, reconcile) -> InstallContext:
    ctx = InstallContext(project_root=tmp_path, apm_dir=tmp_path)
    ctx.apm_modules_dir = tmp_path / "apm_modules"
    ctx.diagnostics = DiagnosticCollector()
    ctx.reconcile_services = reconcile
    ctx.claude_native_plugins = {"owner/native": MagicMock()}
    return ctx


def _stage_mcp(ctx: InstallContext):
    def reconcile(result) -> None:
        assert result.disposition is not InstallDisposition.FAILED
        lock = LockFile.read(get_lockfile_path(ctx.apm_dir))
        lock.mcp_servers = [_SERVER]
        lock.write(get_lockfile_path(ctx.apm_dir))

    return reconcile


def _install_tail(ctx: InstallContext) -> list[str]:
    """The pipeline tail: services, then native registration; return lock writes."""
    lock_path = get_lockfile_path(ctx.apm_dir)
    writes: list[str] = []
    from apm_cli.utils import atomic_io

    real_write = atomic_io.atomic_write_text

    def counting_write(path, text, *args, **kwargs):
        if Path(path) == lock_path:
            writes.append(text)
        return real_write(path, text, *args, **kwargs)

    with (
        patch.object(atomic_io, "atomic_write_text", counting_write),
        patch.object(claude_plugins, "reconcile_claude_plugins") as registrar,
        lockfile_write_scope(LockfileWriter.INSTALL),
    ):
        LockFile().write(lock_path)
        services.run(ctx)
        if services.native_registration_allowed(ctx):
            claude_plugins.run(ctx)
        ctx.registrar = registrar
    return writes


def test_services_reconcile_before_native_registration_commits_once(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, None)
    ctx.reconcile_services = _stage_mcp(ctx)

    writes = _install_tail(ctx)

    assert len(writes) == 1, writes
    assert _SERVER in writes[0]
    assert ctx.registrar.call_args.kwargs["lockfile"].mcp_servers == [_SERVER]
    assert LockFile.read(get_lockfile_path(tmp_path)).mcp_servers == [_SERVER]
    assert ctx.service_failure is None


def test_a_committing_reconcile_failure_still_registers_and_commits_once(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path, None)
    stage = _stage_mcp(ctx)
    failure = InstallFailureAlreadyRendered("MCP installation incomplete: native-srv (claude)")

    def reconcile(result) -> None:
        stage(result)
        raise failure

    ctx.reconcile_services = reconcile

    writes = _install_tail(ctx)

    assert ctx.service_failure is failure
    ctx.registrar.assert_called_once()
    assert len(writes) == 1, writes
    assert _SERVER in writes[0]


def test_a_refused_reconcile_registers_nothing(tmp_path: Path) -> None:
    refusal = PolicyViolationError("MCP server blocked by org policy")

    def reconcile(_result) -> None:
        raise refusal

    ctx = _ctx(tmp_path, reconcile)

    _install_tail(ctx)

    assert ctx.service_failure is refusal
    ctx.registrar.assert_not_called()


def test_an_install_finalize_fails_reconciles_nothing(tmp_path: Path) -> None:
    reconcile = MagicMock()
    ctx = _ctx(tmp_path, reconcile)
    ctx.diagnostics.error("integration failed", package="owner/native")

    services.run(ctx)

    reconcile.assert_not_called()

