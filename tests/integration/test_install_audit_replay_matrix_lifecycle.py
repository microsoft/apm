"""``apm audit --ci`` replays exactly what ``apm install`` deployed, for every package shape (#3649).

Each case drives one shape through the lifecycle a CI repository sees and
asserts that install and the audit replay agree at every step:

1. ``install`` -> ``audit --ci`` is clean.
2. A second ``install`` is a byte-level no-op (lockfile and tree).
3. ``install --frozen`` keeps the lockfile and ``audit --ci`` stays clean.
4. ``compile`` leaves the lockfile alone and ``audit --ci`` stays clean.
5. A fresh clone with no receipts and no cache audits clean before any
   install, ``install --frozen`` leaves ``git status`` clean, audit stays clean.
6. ``uninstall`` removes every deployed file and ``audit --ci`` is clean.

Local directory dependencies are replayed from the same package image install
lays out under ``apm_modules`` (alias and declared path name, dereferenced
contained links), so name-derived output such as hook script directories and
Claude skill directories cannot diverge.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.integration._install_audit_replay_kit import (
    HOOK_TARGETS,
    NO_POLICY,
    ReplayWorld,
    Shape,
    apm_package,
    claude_plugin,
    claude_skill,
    deployed_files,
    hooks_package,
    linked_package,
    lock_bytes,
    mcp_package,
    skill_bundle,
    tree_digest,
    write,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]


def _git_url(slug: str) -> str:
    return f"https://github.com/{slug}.git"


def _apm_git(world: ReplayWorld) -> Shape:
    return Shape([world.publish("pkg", apm_package)], "test/pkg")


def _apm_git_alias_target_subset(world: ReplayWorld) -> Shape:
    slug = world.publish("pkg", apm_package)
    dep = {"git": _git_url(slug), "alias": "pk", "targets": ["claude", "cursor"]}
    return Shape([dep], slug)


def _apm_local_alias(world: ReplayWorld) -> Shape:
    apm_package(world.sources / "lpkg", "lpkg")
    return Shape([{"path": "../lpkg", "alias": "lp"}], "../lpkg")


def _apm_local_linked(world: ReplayWorld) -> Shape:
    linked_package(world.sources / "linked", "linked")
    return Shape(
        [{"path": "../linked"}],
        "../linked",
        expect_files=(".github/instructions/linked.instructions.md",),
    )


def _apm_git_linked(world: ReplayWorld) -> Shape:
    return Shape(
        [world.publish("linked", linked_package)],
        "test/linked",
        expect_files=(".github/instructions/linked.instructions.md",),
    )


def _monorepo_subdirectory(world: ReplayWorld) -> Shape:
    def mono(root: Path, _name: str) -> None:
        apm_package(root / "packages/a", "a")
        apm_package(root / "packages/b", "b")
        write(root / "README.md", "mono\n")

    world.publish("mono", mono)
    return Shape(["test/mono/packages/a"], "test/mono/packages/a")


def _hooks_git_alias(world: ReplayWorld) -> Shape:
    slug = world.publish("hk", hooks_package)
    return Shape([{"git": _git_url(slug), "alias": "guard"}], slug, targets=HOOK_TARGETS)


def _hooks_local_alias(world: ReplayWorld) -> Shape:
    hooks_package(world.sources / "hk-src", "hk")
    return Shape(
        [{"path": "../hk-src", "alias": "guard"}],
        "../hk-src",
        targets=HOOK_TARGETS,
        expect_files=(".claude/hooks/guard/scripts/check.sh",),
    )


def _hooks_local_symlinked_dir(world: ReplayWorld) -> Shape:
    hooks_package(world.sources / "hk-src", "hk")
    (world.sources / "hk-link").symlink_to("hk-src")
    return Shape(
        [{"path": "../hk-link"}],
        "../hk-link",
        targets=HOOK_TARGETS,
        expect_files=(".claude/hooks/hk-link/scripts/check.sh",),
    )


def _claude_skill_local_alias(world: ReplayWorld) -> Shape:
    claude_skill(world.sources / "solo-src", "solo")
    return Shape(
        [{"path": "../solo-src", "alias": "solo-alias"}],
        "../solo-src",
        expect_files=(".claude/skills/solo-alias/SKILL.md",),
    )


def _claude_skill_git(world: ReplayWorld) -> Shape:
    return Shape([world.publish("solo", claude_skill)], "test/solo")


def _skill_bundle_subset(world: ReplayWorld) -> Shape:
    slug = world.publish("sb", skill_bundle)
    return Shape([{"git": _git_url(slug), "skills": ["alpha"]}], slug)


def _claude_plugin_git(world: ReplayWorld) -> Shape:
    return Shape([world.publish("plug", claude_plugin)], "test/plug", targets=HOOK_TARGETS)


def _claude_plugin_local_alias(world: ReplayWorld) -> Shape:
    claude_plugin(world.sources / "lplug", "lplug")
    return Shape([{"path": "../lplug", "alias": "renamed"}], "../lplug", targets=HOOK_TARGETS)


def _mcp_only_git(world: ReplayWorld) -> Shape:
    slug = world.publish("mc", mcp_package)
    return Shape([slug], slug, install_args=("--trust-transitive-mcp",))


def _single_file_local(world: ReplayWorld) -> Shape:
    write(
        world.sources / "loose/one.instructions.md",
        '---\ndescription: one\napplyTo: "**/*.md"\n---\nOne.\n',
    )
    return Shape(["../loose/one.instructions.md"], "../loose/one.instructions.md")


SHAPES: dict[str, Callable[[ReplayWorld], Shape]] = {
    "apm-git": _apm_git,
    "apm-git-alias-target-subset": _apm_git_alias_target_subset,
    "apm-local-alias": _apm_local_alias,
    "apm-local-contained-links": _apm_local_linked,
    "apm-git-contained-links": _apm_git_linked,
    "monorepo-subdirectory": _monorepo_subdirectory,
    "hooks-git-alias": _hooks_git_alias,
    "hooks-local-alias": _hooks_local_alias,
    "hooks-local-symlinked-dir": _hooks_local_symlinked_dir,
    "claude-skill-local-alias": _claude_skill_local_alias,
    "claude-skill-git": _claude_skill_git,
    "skill-bundle-subset": _skill_bundle_subset,
    "claude-plugin-git": _claude_plugin_git,
    "claude-plugin-local-alias": _claude_plugin_local_alias,
    "mcp-only-git": _mcp_only_git,
    "single-file-local": _single_file_local,
}


_SYMLINK_SHAPES = {
    "apm-local-contained-links",
    "apm-git-contained-links",
    "hooks-local-symlinked-dir",
}


@pytest.mark.parametrize(
    "shape_id",
    [
        pytest.param(
            shape_id,
            marks=pytest.mark.skipif(os.name == "nt", reason="creates OS symlinks")
            if shape_id in _SYMLINK_SHAPES
            else (),
        )
        for shape_id in sorted(SHAPES)
    ],
)
def test_install_and_audit_replay_agree_across_lifecycle(
    tmp_path: Path, apm_binary_path: Path, shape_id: str
) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    shape = SHAPES[shape_id](world)
    world.manifest(shape)
    project = world.project

    # 1. install, then the replay agrees with what was deployed.
    world.apm("install", NO_POLICY, *shape.install_args)
    deployed = deployed_files(project)
    for expected in shape.expect_files:
        assert expected in deployed, sorted(deployed)
        assert (project / expected).is_file(), expected
    world.audit_clean()

    # 2. a second install is a byte-level no-op.
    lock = lock_bytes(project)
    tree = tree_digest(project)
    world.apm("install", NO_POLICY, *shape.install_args)
    assert lock_bytes(project) == lock
    assert tree_digest(project) == tree

    # 3. frozen install keeps the lockfile; audit stays clean.
    world.apm("install", "--frozen", NO_POLICY, *shape.install_args)
    assert lock_bytes(project) == lock
    world.audit_clean()

    # 4. compile never touches the lockfile and never creates drift.
    world.apm("compile")
    assert lock_bytes(project) == lock
    world.audit_clean()

    # 5. cold clone: no receipts, no cache, no apm_modules.
    clone = world.cold_clone()
    world.audit_clean(cwd=clone)
    world.apm("install", "--frozen", NO_POLICY, *shape.install_args, cwd=clone)
    assert world.git(clone, "status", "--porcelain") == ""
    assert lock_bytes(clone) == lock
    world.audit_clean(cwd=clone)

    # 6. uninstall removes exactly what install deployed; audit agrees.
    world.apm("uninstall", shape.uninstall)
    leftovers = sorted(path for path in deployed if (project / path).exists())
    assert leftovers == []
    assert deployed_files(project) == set()
    world.audit_clean()


def _module_bytes(project: Path) -> dict[str, bytes]:
    base = project / "apm_modules"
    return {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in sorted(base.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("command", [("audit", NO_POLICY), ("export-patch",)])
def test_read_only_replay_never_rewrites_installed_local_packages(
    tmp_path: Path, apm_binary_path: Path, command: tuple[str, ...]
) -> None:
    """Drift replay writes local package images to scratch, never to apm_modules."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    apm_package(world.sources / "lpkg", "lpkg")
    claude_plugin(world.sources / "lplug", "lplug")
    shape = Shape([{"path": "../lpkg"}, {"path": "../lplug"}], "../lpkg", targets=HOOK_TARGETS)
    world.manifest(shape)
    world.apm("install", NO_POLICY)
    installed = _module_bytes(world.project)

    rules = world.sources / "lpkg/.apm/instructions/glob.instructions.md"
    rules.write_text(rules.read_text(encoding="ascii") + "Edited upstream.\n", encoding="ascii")
    world.apm(*command, expect=None)

    assert _module_bytes(world.project) == installed
