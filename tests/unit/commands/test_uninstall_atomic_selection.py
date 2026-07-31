"""Command-boundary tests for uninstall selection failures."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.uninstall.cli import uninstall
from apm_cli.commands.uninstall.engine import (
    LocalSlotRefresh,
    _activate_staged_local_refresh,
    _recover_local_refresh_backups,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.utils.path_security import PathTraversalError
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged


def _write_manifest(project: Path, dependencies: Sequence[object]) -> None:
    dump_yaml(
        {
            "name": "uninstall-selection-test",
            "version": "1.0.0",
            "dependencies": {"apm": list(dependencies)},
        },
        project / "apm.yml",
    )


def test_uninstall_help_points_to_actionable_dependency_keys() -> None:
    """The terminal discovery surface names the inventory source."""
    result = CliRunner().invoke(uninstall, ["--help"])

    assert result.exit_code == 0
    normalized_output = " ".join(result.output.split())
    assert "direct locked keys from 'apm deps list'" in normalized_output
    assert "pre-uninstall scripts still run" in normalized_output


def test_missing_identifier_exits_nonzero_without_scripts_or_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-match result is a scriptable failure and an atomic no-op."""
    project = tmp_path / "project"
    project.mkdir()
    _write_manifest(project, ["owner/installed"])
    before = ArtifactSnapshot.capture(project)
    monkeypatch.chdir(project)

    with patch("apm_cli.commands.uninstall.cli._fire_uninstall_scripts") as fire_scripts:
        result = CliRunner().invoke(uninstall, ["owner/missing"])

    assert result.exit_code != 0
    assert "no changes were made" in result.output.lower()
    assert_unchanged(before, ArtifactSnapshot.capture(project))
    fire_scripts.assert_not_called()


def test_partial_batch_failure_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One missing requested package prevents removal of every matched package."""
    project = tmp_path / "project"
    project.mkdir()
    _write_manifest(project, ["owner/installed"])
    before = ArtifactSnapshot.capture(project)
    monkeypatch.chdir(project)

    with patch("apm_cli.commands.uninstall.cli._fire_uninstall_scripts") as fire_scripts:
        result = CliRunner().invoke(
            uninstall,
            ["owner/installed", "owner/missing"],
        )

    assert result.exit_code != 0
    assert "no changes were made" in result.output.lower()
    assert_unchanged(before, ArtifactSnapshot.capture(project))
    fire_scripts.assert_not_called()


def test_portable_alias_without_lock_has_actionable_read_only_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing alias mapping explains how to restore or bypass the lock."""
    project = tmp_path / "project"
    project.mkdir()
    _write_manifest(project, ["../packages/Local Package"])
    before = ArtifactSnapshot.capture(project)
    monkeypatch.chdir(project)

    with patch("apm_cli.commands.uninstall.cli._fire_uninstall_scripts") as fire_scripts:
        result = CliRunner().invoke(uninstall, ["_local/Local Package"])

    normalized_output = " ".join(result.output.split())
    assert result.exit_code != 0
    assert "regenerate apm.lock.yaml" in normalized_output
    assert "exact path from apm.yml" in normalized_output
    assert str(tmp_path) not in result.output
    assert_unchanged(before, ArtifactSnapshot.capture(project))
    fire_scripts.assert_not_called()


def test_ambiguous_alias_exits_nonzero_without_leaking_paths_or_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate local aliases fail closed and direct users to the manifest."""
    project = tmp_path / "project"
    project.mkdir()
    first = "../one/Shared Package"
    second = "../two/Shared Package"
    alias = "_local/Shared Package"
    _write_manifest(project, [first, second])
    locked_dependencies = {}
    for path in (first, second):
        dependency = LockedDependency(
            repo_url=alias,
            source="local",
            local_path=path,
        )
        locked_dependencies[dependency.get_unique_key()] = dependency
    LockFile(dependencies=locked_dependencies).write(project / "apm.lock.yaml")
    before = ArtifactSnapshot.capture(project)
    monkeypatch.chdir(project)

    with patch("apm_cli.commands.uninstall.cli._fire_uninstall_scripts") as fire_scripts:
        result = CliRunner().invoke(uninstall, [alias])

    assert result.exit_code != 0
    assert "ambiguous" in result.output.lower()
    assert "exact path from apm.yml" in result.output.lower()
    assert str(tmp_path) not in result.output
    assert_unchanged(before, ArtifactSnapshot.capture(project))
    fire_scripts.assert_not_called()


def test_windows_exact_ambiguity_reports_only_portable_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate exact Windows declaration is named without host path leakage."""
    project = tmp_path / "project"
    project.mkdir()
    declared_path = r"C:\Users\runner\Shared Package"
    _write_manifest(project, [declared_path, declared_path])
    monkeypatch.chdir(project)

    result = CliRunner().invoke(uninstall, [declared_path])

    assert result.exit_code != 0
    assert "_local/Shared Package" in result.output
    assert declared_path not in result.output


@pytest.mark.parametrize(
    "declared_path_factory",
    (
        lambda root: str(root / "source" / "Local Package"),
        lambda _root: r"C:\Users\runner\Local Package",
    ),
)
def test_successful_alias_uses_portable_lifecycle_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared_path_factory: Callable[[Path], str],
) -> None:
    """Pre/post events and diagnostics never expose a selected absolute path."""
    project = tmp_path / "project"
    project.mkdir()
    declared_path = declared_path_factory(tmp_path)
    alias = "_local/Local Package"
    _write_manifest(project, [declared_path])
    dependency = LockedDependency(
        repo_url=alias,
        source="local",
        local_path=declared_path,
    )
    LockFile(dependencies={dependency.get_unique_key(): dependency}).write(
        project / "apm.lock.yaml"
    )
    monkeypatch.chdir(project)

    with (
        patch("apm_cli.commands.uninstall.cli._fire_uninstall_scripts") as fire_scripts,
        patch(
            "apm_cli.commands.uninstall.cli._remove_packages_from_disk",
            return_value=0,
        ),
        patch(
            "apm_cli.commands.uninstall.cli._cleanup_transitive_orphans",
            return_value=(0, set()),
        ),
        patch(
            "apm_cli.commands.uninstall.cli._sync_integrations_after_uninstall",
            return_value=({}, {}),
        ),
        patch("apm_cli.commands.uninstall.cli._cleanup_stale_mcp"),
    ):
        result = CliRunner().invoke(uninstall, [alias])

    assert result.exit_code == 0, result.output
    assert declared_path not in result.output
    assert fire_scripts.call_count == 2
    assert all(call.kwargs["packages"] == (alias,) for call in fire_scripts.call_args_list)


@pytest.mark.parametrize("survivor_mode", ("missing", "malformed"))
def test_shared_slot_staging_failure_is_nonzero_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    survivor_mode: str,
) -> None:
    """An unavailable survivor aborts before scripts or manifest mutation."""
    project = tmp_path / "project"
    project.mkdir()
    removed_path = "../one/Shared Package"
    missing_survivor = "../missing/Shared Package"
    alias = "_local/Shared Package"
    _write_manifest(project, [removed_path, missing_survivor])
    dependencies = {}
    for declared_path in (removed_path, missing_survivor):
        dependency = LockedDependency(
            repo_url=alias,
            source="local",
            local_path=declared_path,
        )
        dependencies[dependency.get_unique_key()] = dependency
    LockFile(dependencies=dependencies).write(project / "apm.lock.yaml")
    installed = project / "apm_modules" / "_local" / "Shared Package"
    installed.mkdir(parents=True)
    (installed / "apm.yml").write_text(
        "name: stale-shared\nversion: 1.0.0\n",
        encoding="ascii",
    )
    if survivor_mode == "malformed":
        survivor = tmp_path / "missing" / "Shared Package"
        survivor.mkdir(parents=True)
        (survivor / "apm.yml").write_text("name: [\n", encoding="ascii")
    before = ArtifactSnapshot.capture(project)
    monkeypatch.chdir(project)

    with patch("apm_cli.commands.uninstall.cli._fire_uninstall_scripts") as fire_scripts:
        result = CliRunner().invoke(uninstall, [removed_path])

    assert result.exit_code != 0
    assert "could not be staged" in " ".join(result.output.split())
    assert str(tmp_path) not in result.output
    assert_unchanged(before, ArtifactSnapshot.capture(project))
    assert fire_scripts.call_count == 1
    assert fire_scripts.call_args.args[0] == "pre-uninstall"


def test_exact_uninstall_fails_when_multiple_shared_slot_survivors_remain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact selection does not guess which of two physical survivors wins."""
    project = tmp_path / "project"
    project.mkdir()
    declared_paths = [
        "../one/Shared Package",
        "../two/Shared Package",
        "../three/Shared Package",
    ]
    alias = "_local/Shared Package"
    _write_manifest(project, declared_paths)
    dependencies = {}
    for declared_path in declared_paths:
        dependency = LockedDependency(
            repo_url=alias,
            source="local",
            local_path=declared_path,
        )
        dependencies[dependency.get_unique_key()] = dependency
    LockFile(dependencies=dependencies).write(project / "apm.lock.yaml")
    before = ArtifactSnapshot.capture(project)
    monkeypatch.chdir(project)

    result = CliRunner().invoke(uninstall, [declared_paths[0]])

    assert result.exit_code != 0
    normalized = " ".join(result.output.split())
    assert "at most one declaration remains" in normalized
    assert str(tmp_path) not in result.output
    assert_unchanged(before, ArtifactSnapshot.capture(project))


def test_pre_uninstall_manifest_edits_are_reloaded_and_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre hook may add unrelated dependencies without stale overwrite."""
    project = tmp_path / "project"
    project.mkdir()
    _write_manifest(project, ["owner/installed"])
    dependency = LockedDependency(repo_url="owner/installed")
    LockFile(dependencies={dependency.get_unique_key(): dependency}).write(
        project / "apm.lock.yaml"
    )
    monkeypatch.chdir(project)

    def fire_script(event_name: str, **_kwargs: object) -> None:
        if event_name != "pre-uninstall":
            return
        manifest = load_yaml(project / "apm.yml")
        manifest["dependencies"]["apm"].append("owner/added-by-hook")
        dump_yaml(manifest, project / "apm.yml")

    with (
        patch(
            "apm_cli.commands.uninstall.cli._fire_uninstall_scripts",
            side_effect=fire_script,
        ),
        patch(
            "apm_cli.commands.uninstall.cli._remove_packages_from_disk",
            return_value=0,
        ),
        patch(
            "apm_cli.commands.uninstall.cli._cleanup_transitive_orphans",
            return_value=(0, set()),
        ),
        patch(
            "apm_cli.commands.uninstall.cli._sync_integrations_after_uninstall",
            return_value=({}, {}),
        ),
        patch("apm_cli.commands.uninstall.cli._cleanup_stale_mcp"),
    ):
        result = CliRunner().invoke(uninstall, ["owner/installed"])

    assert result.exit_code == 0, result.output
    assert load_yaml(project / "apm.yml")["dependencies"]["apm"] == ["owner/added-by-hook"]


def test_duplicate_identifier_removes_manifest_entry_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated equivalent arguments remain idempotent within one command."""
    project = tmp_path / "project"
    project.mkdir()
    package = "owner/installed"
    _write_manifest(project, [package])
    dependency = LockedDependency(repo_url=package)
    LockFile(dependencies={dependency.get_unique_key(): dependency}).write(
        project / "apm.lock.yaml"
    )
    monkeypatch.chdir(project)

    with (
        patch("apm_cli.commands.uninstall.cli._fire_uninstall_scripts") as fire_scripts,
        patch(
            "apm_cli.commands.uninstall.cli._remove_packages_from_disk",
            return_value=0,
        ),
        patch(
            "apm_cli.commands.uninstall.cli._cleanup_transitive_orphans",
            return_value=(0, set()),
        ),
        patch(
            "apm_cli.commands.uninstall.cli._sync_integrations_after_uninstall",
            return_value=({}, {}),
        ),
        patch("apm_cli.commands.uninstall.cli._cleanup_stale_mcp"),
    ):
        result = CliRunner().invoke(uninstall, [package, package])

    assert result.exit_code == 0, result.output
    assert load_yaml(project / "apm.yml")["dependencies"]["apm"] == []
    assert fire_scripts.call_count == 2
    assert all(call.kwargs["packages"] == (package,) for call in fire_scripts.call_args_list)


def test_interrupted_local_refresh_backup_is_recovered(
    tmp_path: Path,
) -> None:
    """A deterministic backup restores the package slot on the next run."""
    modules_dir = tmp_path / "apm_modules"
    local_root = modules_dir / "_local"
    backup = local_root / ".Shared Package.apm-uninstall-backup"
    backup.mkdir(parents=True)
    (backup / "apm.yml").write_text(
        "name: shared\nversion: 1.0.0\n",
        encoding="ascii",
    )
    logger = MagicMock()

    _recover_local_refresh_backups(modules_dir, logger)

    restored = local_root / "Shared Package"
    assert restored.is_dir()
    assert not backup.exists()
    logger.progress.assert_called_once()


def test_local_refresh_activation_rejects_outside_install_path(
    tmp_path: Path,
) -> None:
    """Rename activation reasserts containment at its own boundary."""
    modules_dir = tmp_path / "apm_modules"
    staged = modules_dir / "_local" / ".apm-uninstall-refresh-test"
    staged.mkdir(parents=True)
    refresh = LocalSlotRefresh(
        install_path=tmp_path.parent / "outside",
        staged_path=staged,
        survivor_keys=("survivor",),
    )

    with pytest.raises(PathTraversalError):
        _activate_staged_local_refresh(refresh, modules_dir)
