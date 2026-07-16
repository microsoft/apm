"""Real lifecycle contracts for configuration and dependency graph state."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]


def _runner(apm_binary_path: Path) -> ApmLifecycleRunner:
    """Return the bounded real-binary runner for one lifecycle scenario."""
    return ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=300,
    )


def _instruction(name: str) -> str:
    """Return a valid observable instruction source document."""
    return (
        "---\n"
        "applyTo: '**'\n"
        f"description: Lifecycle contract fixture {name}\n"
        "---\n"
        f"# {name}\n"
    )


def _dependency_rows(project_root: Path) -> dict[str, dict[str, object]]:
    """Read lockfile dependencies by their canonical repository key."""
    lock = load_yaml(project_root / "apm.lock.yaml")
    dependencies = lock["dependencies"]
    assert isinstance(dependencies, list)
    rows: dict[str, dict[str, object]] = {}
    for dependency in dependencies:
        assert isinstance(dependency, dict)
        key = dependency["repo_url"]
        assert isinstance(key, str)
        rows[key] = dependency
    return rows


def test_uninstalling_one_shared_root_retains_shared_dependency_ownership(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """One removed root must not orphan a dependency still reached by another."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "shared-transitive",
        base_env=dict(os.environ),
    )
    factory = LocalPackageFactory(isolated.work_root)
    project = factory.create("consumer", targets=("copilot",))
    root_a = factory.create("root-a")
    root_b = factory.create("root-b")
    shared = factory.create("shared")
    factory.add_relative_dependency(project, root_a)
    factory.add_relative_dependency(project, root_b)
    factory.add_relative_dependency(root_a, shared)
    factory.add_relative_dependency(root_b, shared)
    factory.add_instruction(root_a, "root-a", _instruction("root-a"))
    factory.add_instruction(root_b, "root-b", _instruction("root-b"))
    factory.add_instruction(shared, "shared", _instruction("shared"))
    environment = isolated.subprocess_env()

    _runner(apm_binary_path).run_sequence(
        (("install", "--target", "copilot", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="shared-transitive-install",
        cwd=project.root,
        env=environment,
    )

    before = _dependency_rows(project.root)
    assert set(before) == {"_local/root-a", "_local/root-b", "_local/shared"}
    assert before["_local/shared"]["resolved_by"] in {"_local/root-a", "_local/root-b"}
    shared_instruction = project.root / ".github" / "instructions" / "shared.instructions.md"
    assert shared_instruction.is_file()

    _runner(apm_binary_path).run_sequence(
        (("uninstall", "../root-a"),),
        expected_returncodes=(0,),
        scenario_id="shared-transitive-uninstall-first-root",
        cwd=project.root,
        env=environment,
    )

    after_first_uninstall = _dependency_rows(project.root)
    assert set(after_first_uninstall) == {"_local/root-b", "_local/shared"}
    assert shared_instruction.is_file()

    _runner(apm_binary_path).run_sequence(
        (("audit", "--ci", "--no-policy", "--format", "json"),),
        expected_returncodes=(0,),
        scenario_id="shared-transitive-audit-after-first-uninstall",
        cwd=project.root,
        env=environment,
    )
