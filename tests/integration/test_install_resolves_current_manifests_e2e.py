"""Real-binary contracts: transitive resolution reads the manifest this run installs.

The resolver must read each package's ``apm.yml`` from the content the current
command materializes, not from the copy a previous install left in
``apm_modules``. Otherwise a local package that adds or drops a dependency, or a
branch-pinned parent whose new commit re-pins a child, only converges on the
next run (#3401, folding in #3392 and #3501).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_INSTALL = ("install", "--target", "copilot", "--no-policy", "--parallel-downloads", "0")
_UPDATE = ("update", "--yes", "--target", "copilot", "--parallel-downloads", "0")
_PARENT_URL = "https://gitlab.example.invalid/acme/pack.git"
_CHILD_URL = "https://gitlab.example.invalid/acme/child.git"


def _skill(name: str, marker: str) -> str:
    return f"---\nname: {name}\ndescription: Resolution contract skill {name}\n---\n# {marker}\n"


def _runner(apm_binary_path: Path) -> ApmLifecycleRunner:
    return ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=300,
    )


def _set_dependencies(package: LocalPackage, dependencies: list[object]) -> None:
    manifest = load_yaml(package.manifest_path)
    if dependencies:
        manifest["dependencies"] = {"apm": dependencies}
    else:
        manifest.pop("dependencies", None)
    dump_yaml(manifest, package.manifest_path)


def _locked_keys(lock_path: Path) -> dict[str, object]:
    lock = load_yaml(lock_path)
    return {dep["repo_url"]: dep for dep in lock.get("dependencies", [])}


def _local_packages(root: Path) -> tuple[IsolatedApmEnvironment, LocalPackage, LocalPackage]:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    factory = LocalPackageFactory(isolated.package_root)
    child = factory.create("pkg-b")
    factory.add_skill(child, "pkg-b", _skill("pkg-b", "pkg-b"))
    parent = factory.create("pkg-a")
    factory.add_skill(parent, "pkg-a", _skill("pkg-a", "pkg-a"))
    return isolated, parent, child


def test_local_package_that_drops_a_dependency_removes_it_in_one_install(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3401: lock, dependency tree and deployed files lose the dropped child at once."""
    isolated, parent, _child = _local_packages(tmp_path / "drop")
    env = isolated.subprocess_env()
    _set_dependencies(parent, ["../pkg-b"])
    project = LocalPackageFactory(isolated.work_root).create(
        "project",
        dependencies=(os.path.relpath(parent.root, isolated.work_root / "project"),),
        targets=("copilot",),
    )
    runner = _runner(apm_binary_path)

    first = runner.run(_INSTALL, scenario_id="drop-first", cwd=project.root, env=env)
    assert first.returncode == 0, first.stderr
    assert set(_locked_keys(project.root / "apm.lock.yaml")) == {"_local/pkg-a", "_local/pkg-b"}
    assert (project.root / ".agents/skills/pkg-b/SKILL.md").is_file()

    _set_dependencies(parent, [])
    second = runner.run(_INSTALL, scenario_id="drop-second", cwd=project.root, env=env)
    assert second.returncode == 0, second.stderr

    assert set(_locked_keys(project.root / "apm.lock.yaml")) == {"_local/pkg-a"}
    assert not (project.root / ".agents/skills/pkg-b").exists()
    assert (project.root / ".agents/skills/pkg-a/SKILL.md").is_file()
    tree = runner.run(("deps", "tree"), scenario_id="drop-tree", cwd=project.root, env=env)
    assert tree.returncode == 0, tree.stderr
    assert "pkg-a" in tree.stdout
    assert "pkg-b" not in tree.stdout


@pytest.mark.parametrize("scope", ["project", "user"])
def test_local_package_that_adds_a_dependency_installs_it_in_one_install(
    tmp_path: Path, apm_binary_path: Path, scope: str
) -> None:
    """#3501: the new child arrives with the install that sees the edited manifest."""
    isolated, parent, child = _local_packages(tmp_path / scope)
    env = isolated.subprocess_env()
    runner = _runner(apm_binary_path)
    if scope == "user":
        # Relative transitive paths at user scope are out of scope (#2815).
        child_entry = str(child.root)
        cwd = isolated.work_root
        first_args = ("install", "-g", str(parent.root), *_INSTALL[1:])
        again_args = ("install", "-g", *_INSTALL[1:])
        apm_dir = isolated.home / ".apm"
        deployed = isolated.home / ".agents/skills"
    else:
        child_entry = "../pkg-b"
        cwd = (
            LocalPackageFactory(isolated.work_root)
            .create(
                "project",
                dependencies=(os.path.relpath(parent.root, isolated.work_root / "project"),),
                targets=("copilot",),
            )
            .root
        )
        first_args = again_args = _INSTALL
        apm_dir = cwd
        deployed = cwd / ".agents/skills"

    first = runner.run(first_args, scenario_id=f"add-first-{scope}", cwd=cwd, env=env)
    assert first.returncode == 0, first.stderr
    assert set(_locked_keys(apm_dir / "apm.lock.yaml")) == {"_local/pkg-a"}

    _set_dependencies(parent, [child_entry])
    second = runner.run(again_args, scenario_id=f"add-second-{scope}", cwd=cwd, env=env)
    assert second.returncode == 0, second.stderr

    locked = _locked_keys(apm_dir / "apm.lock.yaml")
    assert set(locked) == {"_local/pkg-a", "_local/pkg-b"}
    assert locked["_local/pkg-b"]["resolved_by"] == "_local/pkg-a"
    assert (deployed / "pkg-b/SKILL.md").is_file()


def test_unchanged_local_reinstall_is_byte_identical(tmp_path: Path, apm_binary_path: Path) -> None:
    """Rechecking local sources must not churn the lock or deployed files."""
    isolated, parent, _child = _local_packages(tmp_path / "unchanged")
    env = isolated.subprocess_env()
    _set_dependencies(parent, ["../pkg-b"])
    project = LocalPackageFactory(isolated.work_root).create(
        "project",
        dependencies=(os.path.relpath(parent.root, isolated.work_root / "project"),),
        targets=("copilot",),
    )
    runner = _runner(apm_binary_path)
    first = runner.run(_INSTALL, scenario_id="unchanged-first", cwd=project.root, env=env)
    assert first.returncode == 0, first.stderr
    before = ArtifactSnapshot.capture(project.root)

    again = runner.run(_INSTALL, scenario_id="unchanged-again", cwd=project.root, env=env)
    assert again.returncode == 0, again.stderr
    assert_unchanged(before, ArtifactSnapshot.capture(project.root))


def test_update_resolves_the_child_pinned_by_a_branch_parent_new_commit(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#3392: one ``apm update`` reads the parent's new commit and locks its child pin."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "update", base_env=dict(os.environ))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    sources = LocalPackageFactory(isolated.package_root)

    child_source = sources.create("child")
    sources.add_skill(child_source, "child", _skill("child", "child version one"))
    child = repositories.create("child", source_tree=child_source.root)
    child_a = repositories.commit(child, message="child version one")
    (child.worktree / "skills" / "child" / "SKILL.md").write_text(
        _skill("child", "child version two"), encoding="utf-8"
    )
    child_b = repositories.commit(child, message="child version two")

    parent_source = sources.create(
        "pack",
        dependencies=({"git": _CHILD_URL, "type": "gitlab", "ref": child_a.sha},),
    )
    sources.add_skill(parent_source, "pack", _skill("pack", "pack"))
    parent = repositories.create("pack", source_tree=parent_source.root)
    repositories.commit(parent, message="pin child A")

    env = repositories.url_rewrite_subprocess_env_many(((parent, _PARENT_URL), (child, _CHILD_URL)))
    project = LocalPackageFactory(isolated.work_root).create(
        "project",
        dependencies=({"git": _PARENT_URL, "type": "gitlab", "ref": "main"},),
        targets=("copilot",),
    )
    runner = _runner(apm_binary_path)
    deployed_child = project.root / ".agents/skills/child/SKILL.md"

    first = runner.run(_INSTALL, scenario_id="update-first", cwd=project.root, env=env)
    assert first.returncode == 0, first.stderr
    child_key = "acme/child"
    assert _locked_keys(project.root / "apm.lock.yaml")[child_key]["resolved_commit"] == child_a.sha
    assert "child version one" in deployed_child.read_text(encoding="utf-8")

    parent_manifest = load_yaml(parent.worktree / "apm.yml")
    parent_manifest["dependencies"]["apm"][0]["ref"] = child_b.sha
    dump_yaml(parent_manifest, parent.worktree / "apm.yml")
    parent_b = repositories.commit(parent, message="pin child B")

    updated = runner.run(_UPDATE, scenario_id="update-repin", cwd=project.root, env=env)
    assert updated.returncode == 0, updated.stderr
    locked = _locked_keys(project.root / "apm.lock.yaml")
    assert locked["acme/pack"]["resolved_commit"] == parent_b.sha
    assert locked[child_key]["resolved_commit"] == child_b.sha
    assert "child version two" in deployed_child.read_text(encoding="utf-8")

    after_update = ArtifactSnapshot.capture(project.root)
    converged = runner.run(_UPDATE, scenario_id="update-converged", cwd=project.root, env=env)
    assert converged.returncode == 0, converged.stderr
    assert_unchanged(after_update, ArtifactSnapshot.capture(project.root))
    replay = runner.run(_INSTALL, scenario_id="update-replay", cwd=project.root, env=env)
    assert replay.returncode == 0, replay.stderr
    assert_unchanged(after_update, ArtifactSnapshot.capture(project.root))
