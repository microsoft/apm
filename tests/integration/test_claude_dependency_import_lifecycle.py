"""Installed-CLI evidence for metadata-backed dependency memory imports."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.lifecycle_merge_group,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_ADO = "https://dev.azure.com/contoso/platform/_git/parent"
_GITLAB = "https://gitlab.com/group/subgroup/team/leaf"
_GITHUB = "https://github.com/FixtureOrg/Standards"
_INSTALL = ("install", "--target", "claude", "--no-policy", "--parallel-downloads", "0")


def _imports(output: Path) -> list[str]:
    imports = [
        line
        for line in (output / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("@")
    ]
    assert imports == sorted(set(imports))
    for line in imports:
        assert "\\" not in line
        assert (output / line[1:]).is_file(), f"Unresolvable dependency import: {line}"
    return imports


@pytest.mark.parametrize("redirect_install", [False, True])
def test_deep_dependency_memory_install_compile_replay_update(
    tmp_path: Path, apm_binary_path: Path, redirect_install: bool
) -> None:
    """Actual ADO/GitLab/GitHub materializations survive lock replay and update."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "lifecycle", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.package_root)
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    leaf = packages.create("leaf", targets=("claude",))
    (leaf.root / "CLAUDE.md").write_text("# Leaf v1\n", encoding="utf-8")
    packages.add_instruction(
        leaf, "leaf", "---\napplyTo: '**'\ndescription: Leaf rule\n---\nUse leaf rules.\n"
    )
    nested = LocalPackageFactory(leaf.root / "nested").create("rules", targets=("claude",))
    (nested.root / "CLAUDE.md").write_text("# Nested package memory\n", encoding="utf-8")
    (nested.root / "SKILL.md").write_text(
        "---\nname: nested-rules\ndescription: Nested rules\n---\n# Rules\n", encoding="utf-8"
    )
    leaf_repo = repositories.create("leaf", source_tree=leaf.root)
    leaf_v1 = repositories.commit(leaf_repo, message="seed deep leaf")

    github = packages.create("github", targets=("claude",))
    (github.root / "CLAUDE.md").write_text("# GitHub memory\n", encoding="utf-8")
    packages.add_instruction(
        github, "github", "---\napplyTo: '**'\ndescription: GitHub rule\n---\nUse GitHub rules.\n"
    )
    github_repo = repositories.create("github", source_tree=github.root)
    repositories.commit(github_repo, message="seed GitHub memory")
    parent = packages.create(
        "parent",
        dependencies=({"git": _GITLAB, "ref": "main"}, {"git": _GITHUB, "ref": "main"}),
        targets=("claude",),
    )
    (parent.root / "CLAUDE.md").write_text("# ADO memory\n", encoding="utf-8")
    (parent.root / "docs").mkdir()
    (parent.root / "docs/CLAUDE.md").write_text("# Not a dependency\n", encoding="utf-8")
    parent_repo = repositories.create("parent", source_tree=parent.root)
    repositories.commit(parent_repo, message="seed ADO parent")
    environment = repositories.url_rewrite_subprocess_env_many(
        ((parent_repo, _ADO), (leaf_repo, _GITLAB), (github_repo, _GITHUB))
    )
    environment["APM_TIERED_RESOLVER"] = "0"
    global_memory = Path(environment["HOME"]) / ".claude/CLAUDE.md"
    global_memory.parent.mkdir(parents=True, exist_ok=True)
    global_memory.write_text("# Global personal memory\n", encoding="utf-8")
    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer",
        dependencies=(
            {"git": _ADO, "ref": "main"},
            {"git": _GITLAB, "ref": "main", "path": "nested/rules"},
        ),
        targets=("claude",),
    )
    output = isolated.work_root / "deployed" if redirect_install else consumer.root
    output.mkdir(parents=True, exist_ok=True)
    root_args = ("--root", str(output)) if redirect_install else ()
    user_rules = output / ".claude/rules/personal.md"
    user_rules.parent.mkdir(parents=True)
    user_rules.write_text("# Personal rule\n", encoding="utf-8")
    settings = output / ".claude/settings.json"
    settings.write_text('{"permissions":{"deny":["Read(private.txt)"]}}\n', encoding="utf-8")
    settings_before = settings.read_bytes()
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    def run(*args: str) -> None:
        result = runner.run(
            args, scenario_id="deep-claude-memory", cwd=consumer.root, env=environment
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    compile_args = ("compile", "--target", "claude", *root_args)
    run(*_INSTALL, *root_args)
    run(*compile_args)
    expected = [
        "@apm_modules/FixtureOrg/Standards/CLAUDE.md",
        "@apm_modules/contoso/platform/parent/CLAUDE.md",
        "@apm_modules/group/subgroup/team/leaf/CLAUDE.md",
        "@apm_modules/group/subgroup/team/leaf/nested/rules/CLAUDE.md",
    ]
    assert _imports(output) == expected
    initial = LifecycleStateSnapshot.capture(output, targets=("claude",))
    lock_bytes = (output / "apm.lock.yaml").read_bytes()
    run(*_INSTALL, "--frozen", *root_args)
    run(*compile_args)
    assert _imports(output) == expected
    assert (output / "apm.lock.yaml").read_bytes() == lock_bytes
    assert (
        LifecycleStateSnapshot.capture(output, targets=("claude",)).semantic_bytes
        == initial.semantic_bytes
    )

    if redirect_install:
        source_lock = consumer.root / "apm.lock.yaml"
        selected_lock = output / "apm.lock.yaml"
        source_lock.write_bytes(lock_bytes)
        assert Path(environment["APM_HOME"]).is_relative_to(Path(environment["HOME"]))
        roots = {
            "source": consumer.root,
            "deploy": output,
            "home": Path(environment["HOME"]),
        }

        def reject_without_writes(args: tuple[str, ...], diagnostic: str) -> None:
            before = ArtifactSnapshotSet.capture(roots)
            rejected = runner.run(
                args, scenario_id="deep-claude-rejection", cwd=consumer.root, env=environment
            )
            assert rejected.returncode == 1, f"{rejected.stdout}\n{rejected.stderr}"
            assert diagnostic in (rejected.stdout + rejected.stderr).replace("\n", "")
            assert ArtifactSnapshotSet.capture(roots) == before

        selected_lock.unlink()
        reject_without_writes((*_INSTALL, "--frozen", *root_args), "requires apm.lock.yaml")
        selected_lock.write_text("[unclosed", encoding="utf-8")
        reject_without_writes(compile_args, "Invalid lockfile")
        selected_lock.unlink()
        for filename in ("apm.lock.yaml", "apm.lock"):
            link = output / filename
            link.symlink_to(output / "missing-lock-target")
            reject_without_writes(compile_args, filename)
            link.unlink()
        selected_lock.write_bytes(lock_bytes)
        memory = output / "apm_modules/contoso/platform/parent/CLAUDE.md"
        memory_bytes = memory.read_bytes()
        memory.unlink()
        memory.symlink_to(global_memory)
        reject_without_writes(compile_args, "outside")
        memory.unlink()
        memory.write_bytes(memory_bytes)
        source_lock.unlink()

    (leaf_repo.worktree / "CLAUDE.md").write_text("# Leaf v2\n", encoding="utf-8")
    leaf_v2 = repositories.commit(leaf_repo, message="advance deep leaf")
    assert leaf_v2.sha != leaf_v1.sha
    run(*_INSTALL, "--update", *root_args)
    run(*compile_args)
    assert _imports(output) == expected
    installed_leaf = output / "apm_modules/group/subgroup/team/leaf/CLAUDE.md"
    assert installed_leaf.read_text(encoding="utf-8") == "# Leaf v2\n"
    lock = LockFile.read(output / "apm.lock.yaml")
    assert lock is not None
    assert any(dep.resolved_commit == leaf_v2.sha for dep in lock.get_all_dependencies())
    assert settings.read_bytes() == settings_before
    assert user_rules.read_text(encoding="utf-8") == "# Personal rule\n"

    if not redirect_install:
        redirected_output = isolated.work_root / "compiled"
        run("compile", "--target", "claude", "--root", str(redirected_output))
        assert _imports(redirected_output) == [
            line.replace("@apm_modules/", "@../consumer/apm_modules/") for line in expected
        ]
    hand_authored = output / "CLAUDE.md"
    hand_authored.write_text("# Hand-authored project memory\n", encoding="utf-8")
    run(*compile_args)
    assert hand_authored.read_text(encoding="utf-8") == "# Hand-authored project memory\n"
    assert global_memory.read_text(encoding="utf-8") == "# Global personal memory\n"
