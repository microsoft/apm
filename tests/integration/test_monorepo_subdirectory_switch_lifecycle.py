"""Monorepo siblings and whole-repo <-> subdirectory switches converge (#3649).

``packages/b`` depends on its sibling ``../a`` in the same repository.  The
tree places ``a`` under ``b`` (#3537), and switching between a whole-repo
dependency and subdirectory dependencies leaves no orphan files, keeps
user-owned files, and every read command agrees (#3612).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import NO_POLICY, World, remote, write, write_package

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_SKILL = "---\nname: tool\ndescription: Tool skill\n---\nUse it.\n"


def _mono(world: World) -> None:
    source = world.isolated.package_root / "mono"
    write_package(source, "mono")
    write_package(source / "packages" / "a", "a")
    write_package(source / "packages" / "b", "b", ["../a"])
    write(source / "skills" / "tool" / "SKILL.md", _SKILL)
    repository = world.factory.create("mono", source_tree=source)
    world.factory.commit(repository, message="mono")
    world.repos["mono"] = repository
    world.env = world.factory.url_rewrite_subprocess_env(repository, remote("mono"))


def test_whole_repo_to_sibling_subdirectories_and_back(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = World.create(tmp_path, apm_binary_path)
    _mono(world)
    write(world.project / ".github/instructions/mine.instructions.md", "mine\n")
    whole = [remote("mono")]
    only_b = [{"git": remote("mono"), "path": "packages/b"}]

    world.manifest(whole)
    world.apm("install", NO_POLICY)
    assert world.deployed() == {"mine.instructions.md", "mono.instructions.md"}
    assert (world.project / ".agents/skills/tool/SKILL.md").exists()
    world.cross_check(present={"test/mono"})

    world.manifest(only_b)
    world.apm("install", NO_POLICY)
    locked = world.locked()
    assert set(locked) == {"test/mono/packages/a", "test/mono/packages/b"}
    assert locked["test/mono/packages/a"]["depth"] == 2
    assert world.deployed() == {"a.instructions.md", "b.instructions.md", "mine.instructions.md"}
    assert not (world.project / ".agents/skills/tool").exists()
    tree = world.apm("deps", "tree").stdout
    assert "could not place" not in tree
    assert tree.index("test/mono/packages/b") < tree.index("test/mono/packages/a")
    why = world.apm("deps", "why", "test/mono/packages/a").stdout
    assert "test/mono/packages/b" in why
    world.cross_check(present={"test/mono/packages/a", "test/mono/packages/b"})
    lock_before = world.lock_bytes()
    world.apm("install", NO_POLICY, "--frozen")
    assert world.lock_bytes() == lock_before

    world.manifest(whole)
    world.apm("install", NO_POLICY)
    assert set(world.locked()) == {"test/mono"}
    assert world.deployed() == {"mine.instructions.md", "mono.instructions.md"}
    world.cross_check(present={"test/mono"})

    world.apm("uninstall", "test/mono")
    assert world.deployed() == {"mine.instructions.md"}
    assert not world.modules()


def test_manifestless_skill_bundle_whole_repo_to_skill_paths_and_back(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The #2926 shape: skills bundled in a repo without apm.yml."""
    world = World.create(tmp_path, apm_binary_path)
    source = world.isolated.package_root / "difit"
    for name in ("x", "y"):
        write(
            source / "skills" / name / "SKILL.md",
            f"---\nname: {name}\ndescription: Skill {name}\n---\nBody {name}.\n",
        )
    write(source / "README.md", "# difit\n")
    repository = world.factory.create("difit", source_tree=source)
    world.factory.commit(repository, message="skills")
    world.env = world.factory.url_rewrite_subprocess_env(repository, remote("difit"))
    write(world.project / ".agents/skills/mine/SKILL.md", "user skill\n")
    skills = world.project / ".agents" / "skills"

    def deployed_skills() -> set[str]:
        return {p.parent.name for p in skills.glob("*/SKILL.md")}

    def agree(*rows: str) -> None:
        listing = world.apm("deps", "list").stdout
        assert "orphaned" not in listing.lower(), listing
        for row in rows:
            assert row in listing, listing
        assert "No orphaned packages found" in world.apm("prune", "--dry-run").stdout
        world.apm("audit", "--ci", NO_POLICY)

    world.manifest([remote("difit")], targets=("copilot", "claude"))
    world.apm("install", NO_POLICY)
    assert deployed_skills() == {"mine", "x", "y"}
    agree("test/difit/skills/x", "test/difit/skills/y")

    world.manifest(
        [{"git": remote("difit"), "path": f"skills/{name}"} for name in ("x", "y")],
        targets=("copilot", "claude"),
    )
    world.apm("install", NO_POLICY)
    assert set(world.locked()) == {"test/difit/skills/x", "test/difit/skills/y"}
    assert deployed_skills() == {"mine", "x", "y"}
    agree("test/difit/skills/x", "test/difit/skills/y")
    lock_before = world.lock_bytes()
    world.apm("install", NO_POLICY)
    assert world.lock_bytes() == lock_before

    world.manifest([remote("difit")], targets=("copilot", "claude"))
    world.apm("install", NO_POLICY)
    assert set(world.locked()) == {"test/difit"}
    assert deployed_skills() == {"mine", "x", "y"}
    agree("test/difit/skills/x", "test/difit/skills/y")

    world.apm("uninstall", "test/difit")
    assert deployed_skills() == {"mine"}
    assert not (world.project / ".claude" / "skills" / "x").exists()
