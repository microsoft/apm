"""MCP/LSP reconciliation runs before native Claude registration commits the lockfile (#4329).

Native registration flushes the staged lockfile before it declares plugins,
so services that reconcile after it stage a second write. These tests drive
the real lockfile write authority and the real ``claude_plugins`` phase; only
the Claude registrar (the harness boundary) is replaced, and it records the
lockfile on disk when it declares: that commit must be the final one.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.claude_plugins.admission import ClaudeNativePluginError
from apm_cli.deps.lockfile import LockedDependency, LockFile, get_lockfile_path
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


def _install_tail(ctx: InstallContext, *, declare_fails: bool = False) -> list[str]:
    """Run the pipeline tail and the command's deferred raise.

    Returns the lockfile on disk each time native registration declared.
    """
    lock_path = get_lockfile_path(ctx.apm_dir)
    declared_against: list[str] = []

    def declare(**_kwargs) -> None:
        declared_against.append(lock_path.read_text(encoding="utf-8"))
        if declare_fails:
            raise ClaudeNativePluginError("could not write Claude settings")

    with patch.object(claude_plugins, "reconcile_claude_plugins", side_effect=declare):
        try:
            with lockfile_write_scope(LockfileWriter.INSTALL):
                LockFile().write(lock_path)
                services.run(ctx)
                if services.native_registration_allowed(ctx):
                    claude_plugins.run(ctx)
                if ctx.service_failure is not None:
                    raise ctx.service_failure
        except ClaudeNativePluginError:
            assert declare_fails
        except Exception as error:
            assert error is ctx.service_failure
    return declared_against


def test_services_reconcile_before_native_registration_commits_once(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, None)
    ctx.reconcile_services = _stage_mcp(ctx)

    declared_against = _install_tail(ctx)

    on_disk = get_lockfile_path(tmp_path).read_text(encoding="utf-8")
    assert declared_against == [on_disk]
    assert _SERVER in on_disk


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

    declared_against = _install_tail(ctx)

    assert ctx.service_failure is failure
    on_disk = get_lockfile_path(tmp_path).read_text(encoding="utf-8")
    assert declared_against == [on_disk]
    assert _SERVER in on_disk


def test_a_refused_reconcile_registers_and_commits_nothing(tmp_path: Path) -> None:
    refusal = PolicyViolationError("MCP server blocked by org policy")

    def reconcile(_result) -> None:
        raise refusal

    ctx = _ctx(tmp_path, reconcile)

    declared_against = _install_tail(ctx)

    assert ctx.service_failure is refusal
    assert declared_against == []
    assert not get_lockfile_path(tmp_path).exists()


def test_an_install_finalize_fails_reconciles_nothing(tmp_path: Path) -> None:
    reconcile = MagicMock()
    ctx = _ctx(tmp_path, reconcile)
    ctx.diagnostics.error("integration failed", package="owner/native")

    declared_against = _install_tail(ctx)

    reconcile.assert_not_called()
    on_disk = get_lockfile_path(tmp_path).read_text(encoding="utf-8")
    assert declared_against == [on_disk]
    assert _SERVER not in on_disk


@pytest.mark.parametrize("previous", [False, True], ids=["first-install", "existing-lock"])
def test_a_failed_native_declaration_keeps_the_reconciled_services_recorded(
    tmp_path: Path, previous: bool
) -> None:
    """Rolling back native records keeps the MCP servers reconciliation wrote (req-na-004)."""
    ctx = _ctx(tmp_path, None)
    ctx.reconcile_services = _stage_mcp(ctx)
    if previous:
        ctx.existing_lockfile = LockFile(
            dependencies={"owner/kept": LockedDependency(repo_url="owner/kept")}
        )

    _install_tail(ctx, declare_fails=True)

    rolled_back = get_lockfile_path(tmp_path).read_text(encoding="utf-8")
    assert _SERVER in rolled_back
    assert ("owner/kept" in rolled_back) is previous


def test_a_refused_declaration_with_unchanged_services_restores_the_lock_byte_for_byte(
    tmp_path: Path,
) -> None:
    """#4685: no service record changed, so the rollback is the previous lock as written."""
    ctx = _ctx(tmp_path, None)
    previous = LockFile(dependencies={"owner/kept": LockedDependency(repo_url="owner/kept")})
    ctx.existing_lockfile = previous
    expected = tmp_path / "expected" / "apm.lock.yaml"
    expected.parent.mkdir()
    previous.write(expected)

    _install_tail(ctx, declare_fails=True)

    rolled_back = get_lockfile_path(tmp_path).read_text(encoding="utf-8")
    assert "mcp_target_servers" not in rolled_back
    assert rolled_back == expected.read_text(encoding="utf-8")


def test_a_refused_declaration_restores_the_original_bytes_with_comments(tmp_path: Path) -> None:
    """#4685: a legacy timestamp and comments survive the rollback unchanged."""
    ctx = _ctx(tmp_path, None)
    lock_path = get_lockfile_path(tmp_path)
    previous = LockFile(dependencies={"owner/kept": LockedDependency(repo_url="owner/kept")})
    previous.write(lock_path)
    original = (
        "# kept by the user\n"
        + lock_path.read_text(encoding="utf-8")
        + "generated_at: '2020-01-01T00:00:00Z'\n"
    ).encode("utf-8")
    lock_path.write_bytes(original)
    ctx.existing_lockfile = LockFile.read(lock_path)

    _install_tail(ctx, declare_fails=True)

    assert lock_path.read_bytes() == original


def test_a_type_only_service_change_is_kept_by_the_rollback(tmp_path: Path) -> None:
    """``1`` and ``true`` differ: the rollback carries the committed ``true``."""
    previous = LockFile(mcp_servers=[_SERVER], mcp_configs={_SERVER: {"name": _SERVER, "flag": 1}})
    committed = LockFile(
        mcp_servers=[_SERVER], mcp_configs={_SERVER: {"name": _SERVER, "flag": True}}
    )

    rollback = claude_plugins._with_reconciled_services(previous, committed)
    lock_path = get_lockfile_path(tmp_path)
    rollback.write(lock_path)

    assert "flag: true" in lock_path.read_text(encoding="utf-8")
