"""Component contract for uninstall hook-cleanup failures."""

from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from apm_cli.commands.uninstall.cli import uninstall
from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.yaml_io import dump_yaml

pytestmark = pytest.mark.component


def _prepare_hook_install(
    tmp_path: Path,
    *,
    hook_content: str = '{"version": 1}\n',
) -> tuple[str, Path]:
    """Create one lock-backed Copilot hook installation."""
    package = "owner/installed"
    hook_path = ".github/hooks/installed-hooks.json"
    hook_file = tmp_path / hook_path
    hook_file.parent.mkdir(parents=True)
    hook_file.write_text(hook_content, encoding="ascii")
    dump_yaml(
        {
            "name": "hook-cleanup-failure",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {"apm": [package]},
        },
        tmp_path / "apm.yml",
    )
    dependency = LockedDependency(repo_url=package, deployed_files=[hook_path])
    dependency_key = dependency.get_unique_key()
    locator = DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="copilot",
        value=hook_path,
        runtime=None,
        scope="project",
    )
    record = DeploymentRecord(
        locator=locator,
        owners=(dependency_key,),
        active_owner=dependency_key,
        content_hash=compute_file_hash(hook_file),
    )
    LockFile(
        dependencies={dependency_key: dependency},
        deployment_ledger=DeploymentLedger(records={locator.key: record}),
        _deployments_present=True,
    ).write(tmp_path / "apm.lock.yaml")
    return package, hook_file


def _assert_incomplete_cleanup(result: Result, hook_file: Path) -> None:
    """Assert the user-visible incomplete-cleanup contract."""
    assert result.exit_code == 1
    assert hook_file.exists()
    assert "Preserved managed hook path" in result.output
    assert "managed hook cleanup is incomplete" in result.output
    assert "Uninstall complete" not in result.output


def test_uninstall_reports_real_managed_hook_unlink_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A HookIntegrator unlink failure reaches the CLI as a nonzero outcome."""
    package, hook_file = _prepare_hook_install(tmp_path)
    original_unlink = Path.unlink
    failure_active = False

    def fail_hook_unlink(path: Path, *args, **kwargs) -> None:
        if failure_active and path == hook_file:
            raise OSError("simulated hook unlink failure")
        original_unlink(path, *args, **kwargs)

    def recreate_hook_after_preflight(*_args, **_kwargs) -> int:
        nonlocal failure_active
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        hook_file.write_text('{"version": 1}\n', encoding="ascii")
        failure_active = True
        return 0

    monkeypatch.setattr(Path, "unlink", fail_hook_unlink)
    monkeypatch.setattr(
        "apm_cli.commands.uninstall.cli._remove_packages_from_disk",
        recreate_hook_after_preflight,
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(uninstall, [package])

    _assert_incomplete_cleanup(result, hook_file)


def test_uninstall_preserves_hook_replaced_after_provenance_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user replacement racing cleanup fails the final provenance gate."""
    package, hook_file = _prepare_hook_install(tmp_path)
    user_content = '{"user": "replacement"}\n'

    def replace_hook_after_preflight(*_args, **_kwargs) -> int:
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        hook_file.write_text(user_content, encoding="ascii")
        return 0

    monkeypatch.setattr(
        "apm_cli.commands.uninstall.cli._remove_packages_from_disk",
        replace_hook_after_preflight,
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(uninstall, [package])

    _assert_incomplete_cleanup(result, hook_file)
    assert hook_file.read_text(encoding="ascii") == user_content
    assert "edited since APM deployed it" in result.output


def test_uninstall_preserves_symlink_replaced_after_provenance_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user symlink racing cleanup must pass through the provenance gate."""
    package, hook_file = _prepare_hook_install(tmp_path, hook_content="")
    outside_file = tmp_path / "user-hook.json"
    user_content = '{"user": "outside"}\n'
    outside_file.write_text(user_content, encoding="ascii")

    def replace_hook_with_symlink(*_args, **_kwargs) -> int:
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        hook_file.symlink_to(outside_file)
        return 0

    monkeypatch.setattr(
        "apm_cli.commands.uninstall.cli._remove_packages_from_disk",
        replace_hook_with_symlink,
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(uninstall, [package])

    _assert_incomplete_cleanup(result, hook_file)
    assert hook_file.is_symlink()
    assert outside_file.read_text(encoding="ascii") == user_content
    assert "replaced by a symlink" in result.output


def test_uninstall_reports_integration_cleanup_exception_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An integration exception is actionable and never reports success."""
    package, _hook_file = _prepare_hook_install(tmp_path)

    def fail_integration_cleanup(*_args, **_kwargs) -> None:
        raise OSError("sensitive internal detail")

    monkeypatch.setattr(
        "apm_cli.commands.uninstall.cli._sync_integrations_after_uninstall",
        fail_integration_cleanup,
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(uninstall, [package])

    assert result.exit_code == 1
    assert "Integration cleanup did not finish" in result.output
    assert "Run 'apm install' to resync remaining integrations" in result.output
    assert "sensitive internal detail" not in result.output
    assert "Uninstall complete" not in result.output


def test_cleanup_snapshot_prefers_canonical_ledger_hash() -> None:
    """Divergent compatibility hashes cannot override canonical provenance."""
    hook_path = ".github/hooks/installed-hooks.json"
    dependency = LockedDependency(
        repo_url="owner/installed",
        deployed_files=[hook_path],
        deployed_file_hashes={hook_path: "sha256:legacy"},
    )
    dependency_key = dependency.get_unique_key()
    locator = DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="copilot",
        value=hook_path,
        runtime=None,
        scope="project",
    )
    record = DeploymentRecord(
        locator=locator,
        owners=(dependency_key,),
        active_owner=dependency_key,
        content_hash="sha256:canonical",
    )
    lockfile = LockFile(
        dependencies={dependency_key: dependency},
        deployment_ledger=DeploymentLedger(records={locator.key: record}),
        _deployments_present=True,
    )

    snapshot = DeploymentLedgerCodec.cleanup_snapshot(lockfile, {dependency_key})

    assert snapshot.paths == frozenset({hook_path})
    assert snapshot.hashes == {hook_path: "sha256:canonical"}
