"""Installed-CLI regression: a targeted install reconciles the selected package's subtree.

Issue #3502: after a local package drops a dependency, ``apm install PKG``
(project scope) and ``apm install -g PKG`` (user scope) must remove the dropped
dependency's lock entry, ``apm_modules`` copy and deployed skill, keep a
dependency another declared package still needs, and leave unrelated packages'
lock entries unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]


@dataclass
class _Scenario:
    """Roots and process inputs for one isolated real-CLI lifecycle."""

    runner: ApmLifecycleRunner
    environment: dict[str, str]
    cwd: Path
    apm_dir: Path
    skills_dir: Path
    global_scope: bool

    def run(self, *arguments: str) -> CommandResult:
        result = self.runner.run(
            arguments,
            scenario_id="targeted-install-dropped-dependency",
            cwd=self.cwd,
            env=self.environment,
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        return result

    def install(self, package: LocalPackage) -> CommandResult:
        scope = ("-g",) if self.global_scope else ()
        return self.run(
            "install",
            *scope,
            str(package.root),
            "--target",
            "claude",
            "--no-policy",
            "--parallel-downloads",
            "0",
        )

    def deps_tree(self) -> str:
        scope = ("--global",) if self.global_scope else ()
        return self.run("deps", "tree", *scope).stdout

    def lock_entries(self) -> dict[str, dict[str, object]]:
        lock = load_yaml(self.apm_dir / "apm.lock.yaml")
        return {entry["name"]: entry for entry in lock["dependencies"]}

    def module_dirs(self) -> dict[str, Path]:
        """Map each materialized local package name to its apm_modules copy."""
        found: dict[str, Path] = {}
        local_root = self.apm_dir / "apm_modules" / "_local"
        # Direct locals live at _local/<name>; transitive ones at _local/<anchor>/<name>.
        for pattern in ("*/apm.yml", "*/*/apm.yml"):
            for manifest in local_root.glob(pattern):
                found[str(load_yaml(manifest)["name"])] = manifest.parent
        return found

    def deployed_skills(self) -> set[str]:
        return {path.name for path in self.skills_dir.iterdir() if path.is_dir()}


def _package(factory: LocalPackageFactory, name: str, deps: tuple[Path, ...] = ()) -> LocalPackage:
    package = factory.create(name, dependencies=tuple(str(dep) for dep in deps))
    factory.add_skill(
        package,
        name,
        f"---\nname: {name}\ndescription: Skill {name}\n---\n# {name}\n",
    )
    return package


@pytest.mark.parametrize("global_scope", [True, False], ids=["user-scope", "project-scope"])
def test_targeted_install_removes_dependency_dropped_by_selected_package(
    tmp_path: Path, apm_binary_path: Path, global_scope: bool
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "lifecycle", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    environment["APM_NO_CACHE"] = "1"
    environment.pop("CLAUDE_CONFIG_DIR", None)

    factory = LocalPackageFactory(isolated.package_root)
    dropped = _package(factory, "child-dropped")
    shared = _package(factory, "child-shared")
    parent = _package(factory, "parent", (dropped.root, shared.root))
    sibling = _package(factory, "sibling", (shared.root,))
    bystander = _package(factory, "bystander")

    if global_scope:
        cwd = isolated.work_root
        apm_dir = isolated.home / ".apm"
        skills_dir = isolated.home / ".claude" / "skills"
    else:
        cwd = isolated.work_root / "consumer"
        cwd.mkdir(parents=True)
        (cwd / "apm.yml").write_text("name: consumer\nversion: 1.0.0\n", encoding="utf-8")
        apm_dir = cwd
        skills_dir = cwd / ".claude" / "skills"
    scenario = _Scenario(
        ApmLifecycleRunner((str(apm_binary_path),)),
        environment,
        cwd,
        apm_dir,
        skills_dir,
        global_scope,
    )

    # Parent is installed first so the shared child is recorded under it.
    for package in (parent, sibling, bystander):
        scenario.install(package)
    before_entries = scenario.lock_entries()
    before_modules = scenario.module_dirs()
    assert before_entries["child-shared"]["resolved_by"] == before_entries["parent"]["repo_url"]
    assert {"child-dropped", "child-shared"} <= set(before_entries)
    assert "child-dropped" in before_modules
    assert {"parent", "child-dropped", "child-shared", "sibling", "bystander"} <= (
        scenario.deployed_skills()
    )

    factory.replace_apm_dependencies(parent, ())
    # The first run may still read the previously materialized parent manifest
    # (#3401); the second run must have reconciled the parent's subtree.
    scenario.install(parent)
    scenario.install(parent)

    after_entries = scenario.lock_entries()
    assert "child-dropped" not in after_entries
    assert "child-dropped" not in (apm_dir / "apm.lock.yaml").read_text(encoding="utf-8")
    assert "child-shared" in after_entries
    assert after_entries["child-shared"]["resolved_by"] == after_entries["sibling"]["repo_url"]
    for name in ("sibling", "bystander"):
        assert after_entries[name] == before_entries[name]

    after_modules = scenario.module_dirs()
    assert "child-dropped" not in after_modules
    assert not before_modules["child-dropped"].exists()
    assert not before_modules["child-dropped"].parent.exists()
    assert after_modules["child-shared"] == before_modules["child-shared"]

    skills = scenario.deployed_skills()
    assert "child-dropped" not in skills
    assert {"parent", "child-shared", "sibling", "bystander"} <= skills

    tree = scenario.deps_tree()
    assert "child-dropped" not in tree
    assert "child-shared" in tree
