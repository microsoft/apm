"""Installed-binary lifecycle contracts for portable local uninstall aliases."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_INITIAL_SENTINEL = "Portable alias initial content."
_UPDATED_SENTINEL = "Portable alias updated content."


def _instruction_text(sentinel: str) -> str:
    return (
        "---\n"
        "applyTo: '**'\n"
        "description: Portable local uninstall alias proof\n"
        "---\n"
        "# Portable local uninstall alias proof\n"
        f"{sentinel}\n"
    )


def _run(
    runner: ApmLifecycleRunner,
    args: tuple[str, ...],
    *,
    scenario_id: str,
    cwd: Path,
    env: dict[str, str],
) -> CommandResult:
    return runner.run(args, scenario_id=scenario_id, cwd=cwd, env=env)


def _assert_success(result: CommandResult) -> None:
    assert result.returncode == 0, (
        f"command={result.command!r}\nstdout={result.stdout}\nstderr={result.stderr}"
    )


def _displayed_local_key(result: CommandResult, package_name: str) -> str:
    output = result.stdout + result.stderr
    matches = set(re.findall(rf"(_local/{re.escape(package_name)})", output))
    assert matches == {f"_local/{package_name}"}, output
    return matches.pop()


def _manifest_dependencies(path: Path) -> list[object]:
    manifest = load_yaml(path)
    return list(manifest.get("dependencies", {}).get("apm") or [])


def _local_lock_entries(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    lockfile = load_yaml(path)
    return [
        entry
        for entry in lockfile.get("dependencies", [])
        if isinstance(entry, dict) and entry.get("source") == "local"
    ]


def _assert_removed_state(
    *,
    manifest_path: Path,
    lockfile_path: Path,
    materialized_path: Path,
    integration_path: Path,
) -> None:
    assert _manifest_dependencies(manifest_path) == []
    assert _local_lock_entries(lockfile_path) == []
    assert not materialized_path.exists()
    if integration_path.exists():
        assert _INITIAL_SENTINEL not in integration_path.read_text(encoding="utf-8")
        assert _UPDATED_SENTINEL not in integration_path.read_text(encoding="utf-8")


def _create_project_scenario(
    isolated: IsolatedApmEnvironment,
) -> tuple[LocalPackage, LocalPackage, Path]:
    factory = LocalPackageFactory(isolated.work_root)
    dependency = factory.create("Mixed Case Package", version="1.0.0", targets=("copilot",))
    instruction = factory.add_instruction(
        dependency,
        "alias-probe",
        _instruction_text(_INITIAL_SENTINEL),
    )
    project = factory.create("consumer-project", targets=("copilot",))
    factory.add_relative_dependency(project, dependency)
    return dependency, project, instruction


def test_project_relative_list_key_drives_update_and_uninstall(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Project local aliases round-trip through a real installed CLI."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "project-alias",
        base_env=os.environ,
    )
    env = isolated.subprocess_env()
    dependency, project, instruction = _create_project_scenario(isolated)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    install = _run(
        runner,
        ("install", "--target", "copilot", "--no-policy"),
        scenario_id="project-install",
        cwd=project.root,
        env=env,
    )
    _assert_success(install)
    instruction.write_text(_instruction_text(_UPDATED_SENTINEL), encoding="utf-8")
    reinstalled = _run(
        runner,
        ("install", "--target", "copilot", "--no-policy"),
        scenario_id="project-reinstall",
        cwd=project.root,
        env=env,
    )
    _assert_success(reinstalled)
    updated = _run(
        runner,
        ("update", "--yes", "--target", "copilot"),
        scenario_id="project-update",
        cwd=project.root,
        env=env,
    )
    _assert_success(updated)
    materialized = (
        project.root
        / "apm_modules"
        / "_local"
        / dependency.name
        / ".apm"
        / "instructions"
        / "alias-probe.instructions.md"
    )
    integrated = project.root / ".github" / "instructions" / "alias-probe.instructions.md"
    assert _UPDATED_SENTINEL in materialized.read_text(encoding="utf-8")
    assert _UPDATED_SENTINEL in integrated.read_text(encoding="utf-8")
    listed = _run(
        runner,
        ("deps", "list"),
        scenario_id="project-list",
        cwd=project.root,
        env=env,
    )
    _assert_success(listed)
    displayed_key = _displayed_local_key(listed, dependency.name)
    assert str(dependency.root) not in listed.stdout + listed.stderr

    uninstalled = _run(
        runner,
        ("uninstall", displayed_key),
        scenario_id="project-uninstall",
        cwd=project.root,
        env=env,
    )
    _assert_success(uninstalled)
    assert str(dependency.root) not in uninstalled.stdout + uninstalled.stderr
    _assert_removed_state(
        manifest_path=project.manifest_path,
        lockfile_path=project.root / "apm.lock.yaml",
        materialized_path=project.root / "apm_modules" / "_local" / dependency.name,
        integration_path=integrated,
    )

    before_repeat = ArtifactSnapshot.capture(project.root)
    repeated = _run(
        runner,
        ("uninstall", displayed_key),
        scenario_id="project-repeat",
        cwd=project.root,
        env=env,
    )
    assert repeated.returncode != 0
    assert_unchanged(before_repeat, ArtifactSnapshot.capture(project.root))


def test_global_absolute_list_key_drives_update_and_uninstall(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Global local aliases avoid exposing or requiring the absolute path."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "global-alias",
        base_env=os.environ,
    )
    env = isolated.subprocess_env()
    factory = LocalPackageFactory(isolated.package_root)
    dependency = factory.create("Mixed Case Package", version="1.0.0", targets=("copilot",))
    instruction = factory.add_instruction(
        dependency,
        "alias-probe",
        _instruction_text(_INITIAL_SENTINEL),
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    install = _run(
        runner,
        (
            "install",
            "--global",
            str(dependency.root),
            "--target",
            "copilot",
            "--no-policy",
        ),
        scenario_id="global-install",
        cwd=isolated.work_root,
        env=env,
    )
    _assert_success(install)
    instruction.write_text(_instruction_text(_UPDATED_SENTINEL), encoding="utf-8")
    reinstalled = _run(
        runner,
        (
            "install",
            "--global",
            "--target",
            "copilot",
            "--no-policy",
        ),
        scenario_id="global-reinstall",
        cwd=isolated.work_root,
        env=env,
    )
    _assert_success(reinstalled)
    updated = _run(
        runner,
        ("update", "--global", "--yes", "--target", "copilot"),
        scenario_id="global-update",
        cwd=isolated.work_root,
        env=env,
    )
    _assert_success(updated)
    materialized = (
        isolated.config_root
        / "apm_modules"
        / "_local"
        / dependency.name
        / ".apm"
        / "instructions"
        / "alias-probe.instructions.md"
    )
    integrated = isolated.home / ".copilot" / "copilot-instructions.md"
    assert _UPDATED_SENTINEL in materialized.read_text(encoding="utf-8")
    assert _UPDATED_SENTINEL in integrated.read_text(encoding="utf-8")
    listed = _run(
        runner,
        ("deps", "list", "--global"),
        scenario_id="global-list",
        cwd=isolated.work_root,
        env=env,
    )
    _assert_success(listed)
    displayed_key = _displayed_local_key(listed, dependency.name)
    assert str(dependency.root) not in listed.stdout + listed.stderr

    uninstalled = _run(
        runner,
        ("uninstall", "--global", displayed_key),
        scenario_id="global-uninstall",
        cwd=isolated.work_root,
        env=env,
    )
    _assert_success(uninstalled)
    assert str(dependency.root) not in uninstalled.stdout + uninstalled.stderr
    _assert_removed_state(
        manifest_path=isolated.config_root / "apm.yml",
        lockfile_path=isolated.config_root / "apm.lock.yaml",
        materialized_path=isolated.config_root / "apm_modules" / "_local" / dependency.name,
        integration_path=integrated,
    )

    before_repeat = ArtifactSnapshot.capture(isolated.home)
    repeated = _run(
        runner,
        ("uninstall", "--global", displayed_key),
        scenario_id="global-repeat",
        cwd=isolated.work_root,
        env=env,
    )
    assert repeated.returncode != 0
    assert_unchanged(before_repeat, ArtifactSnapshot.capture(isolated.home))


def test_duplicate_local_alias_fails_closed_and_exact_path_remains_accepted(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """One portable alias cannot choose between same-basename declarations."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "ambiguous-alias",
        base_env=os.environ,
    )
    env = isolated.subprocess_env()
    first_factory = LocalPackageFactory(isolated.root / "source-one")
    second_factory = LocalPackageFactory(isolated.root / "source-two")
    third_factory = LocalPackageFactory(isolated.root / "source-three")
    first = first_factory.create("Shared Package", targets=("copilot",))
    second = second_factory.create("Shared Package", targets=("copilot",))
    third = third_factory.create("Shared Package", targets=("copilot",))
    first_factory.add_instruction(first, "first", _instruction_text("First package."))
    second_factory.add_instruction(second, "second", _instruction_text("Second package."))
    third_factory.add_instruction(third, "third", _instruction_text("Third package."))
    project_factory = LocalPackageFactory(isolated.work_root)
    project = project_factory.create("consumer-project", targets=("copilot",))
    first_path = Path(os.path.relpath(first.root, project.root)).as_posix()
    second_path = Path(os.path.relpath(second.root, project.root)).as_posix()
    third_path = Path(os.path.relpath(third.root, project.root)).as_posix()
    project_factory.replace_apm_dependencies(
        project,
        (first_path, second_path, third_path),
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    install = _run(
        runner,
        ("install", "--target", "copilot", "--no-policy"),
        scenario_id="ambiguous-install",
        cwd=project.root,
        env=env,
    )
    _assert_success(install)
    listed = _run(
        runner,
        ("deps", "list"),
        scenario_id="ambiguous-list",
        cwd=project.root,
        env=env,
    )
    _assert_success(listed)
    displayed_key = _displayed_local_key(listed, first.name)
    before = ArtifactSnapshot.capture(project.root)

    ambiguous = _run(
        runner,
        ("uninstall", displayed_key),
        scenario_id="ambiguous-uninstall",
        cwd=project.root,
        env=env,
    )
    assert ambiguous.returncode != 0
    assert "ambiguous" in (ambiguous.stdout + ambiguous.stderr).lower()
    assert str(first.root) not in ambiguous.stdout + ambiguous.stderr
    assert str(second.root) not in ambiguous.stdout + ambiguous.stderr
    assert str(third.root) not in ambiguous.stdout + ambiguous.stderr
    assert_unchanged(before, ArtifactSnapshot.capture(project.root))

    exact = _run(
        runner,
        ("uninstall", "--dry-run", second_path),
        scenario_id="exact-path-dry-run",
        cwd=project.root,
        env=env,
    )
    _assert_success(exact)
    assert_unchanged(before, ArtifactSnapshot.capture(project.root))

    first_factory.add_instruction(
        first,
        "refreshed",
        _instruction_text("Refreshed survivor content."),
    )
    exact_removal = _run(
        runner,
        ("uninstall", second_path, third_path),
        scenario_id="exact-path-uninstall",
        cwd=project.root,
        env=env,
    )
    _assert_success(exact_removal)
    assert _manifest_dependencies(project.manifest_path) == [first_path]
    local_entries = _local_lock_entries(project.root / "apm.lock.yaml")
    assert len(local_entries) == 1
    assert local_entries[0]["local_path"] == first_path
    materialized = project.root / "apm_modules" / "_local" / first.name
    assert (materialized / ".apm/instructions/first.instructions.md").is_file()
    assert (materialized / ".apm/instructions/refreshed.instructions.md").is_file()
    assert not (materialized / ".apm/instructions/second.instructions.md").exists()
    assert not (materialized / ".apm/instructions/third.instructions.md").exists()
    assert (project.root / ".github/instructions/first.instructions.md").is_file()
    refreshed_path = ".github/instructions/refreshed.instructions.md"
    assert (project.root / refreshed_path).is_file()
    assert not (project.root / ".github/instructions/second.instructions.md").exists()
    assert not (project.root / ".github/instructions/third.instructions.md").exists()
    assert refreshed_path in local_entries[0]["deployed_files"]
    assert refreshed_path in local_entries[0]["deployed_file_hashes"]
    assert not list((materialized.parent).glob(".apm-uninstall-*"))


def test_global_duplicate_alias_preserves_exact_path_survivor(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Global shared slots re-integrate from the surviving absolute declaration."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "global-ambiguous-alias",
        base_env=os.environ,
    )
    env = isolated.subprocess_env()
    first_factory = LocalPackageFactory(isolated.root / "global-source-one")
    second_factory = LocalPackageFactory(isolated.root / "global-source-two")
    first = first_factory.create("Shared Package", targets=("copilot",))
    second = second_factory.create("Shared Package", targets=("copilot",))
    first_factory.add_instruction(first, "first", _instruction_text("First global package."))
    second_factory.add_instruction(
        second,
        "second",
        _instruction_text("Second global package."),
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    install = _run(
        runner,
        (
            "install",
            "--global",
            str(first.root),
            str(second.root),
            "--target",
            "copilot",
            "--no-policy",
        ),
        scenario_id="global-ambiguous-install",
        cwd=isolated.work_root,
        env=env,
    )
    _assert_success(install)
    listed = _run(
        runner,
        ("deps", "list", "--global"),
        scenario_id="global-ambiguous-list",
        cwd=isolated.work_root,
        env=env,
    )
    _assert_success(listed)
    displayed_key = _displayed_local_key(listed, first.name)
    before = ArtifactSnapshot.capture(isolated.home)

    ambiguous = _run(
        runner,
        ("uninstall", "--global", displayed_key),
        scenario_id="global-ambiguous-uninstall",
        cwd=isolated.work_root,
        env=env,
    )
    assert ambiguous.returncode != 0
    assert_unchanged(before, ArtifactSnapshot.capture(isolated.home))

    exact = _run(
        runner,
        ("uninstall", "--global", str(second.root)),
        scenario_id="global-exact-uninstall",
        cwd=isolated.work_root,
        env=env,
    )
    _assert_success(exact)
    assert str(second.root) not in exact.stdout + exact.stderr
    assert _manifest_dependencies(isolated.config_root / "apm.yml") == [str(first.root)]
    local_entries = _local_lock_entries(isolated.config_root / "apm.lock.yaml")
    assert len(local_entries) == 1
    assert local_entries[0]["local_path"] == str(first.root)
    materialized = isolated.config_root / "apm_modules" / "_local" / first.name
    assert (materialized / ".apm/instructions/first.instructions.md").is_file()
    assert not (materialized / ".apm/instructions/second.instructions.md").exists()
    integrated = isolated.home / ".copilot/copilot-instructions.md"
    integrated_text = integrated.read_text(encoding="utf-8")
    assert "First global package." in integrated_text
    assert "Second global package." not in integrated_text
