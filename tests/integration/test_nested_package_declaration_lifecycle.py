"""A repository and its own subdirectory package cannot both be declared (#3699).

The subdirectory package would be installed inside the repository package's
directory, so uninstalling one deletes the other and ``deps list`` hides it.
Install and update reject the combination before anything is written
(dry-run names it as an incomplete preview); a repository whose own
manifest depends on its subdirectory package still installs, and keeping
only one declaration converges.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import NO_POLICY, World, remote, write_package

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_REJECTED = "test/mono and its subdirectory package test/mono/packages/x cannot both be declared"


def _world(tmp_path: Path, apm_binary_path: Path, *, root_deps: list[object]) -> World:
    world = World.create(tmp_path, apm_binary_path)
    source = world.isolated.package_root / "mono"
    write_package(source, "mono", root_deps)
    write_package(source / "packages" / "x", "x")
    repository = world.factory.create("mono", source_tree=source)
    world.factory.commit(repository, message="publish mono")
    world.repos["mono"] = repository
    world.env = world.factory.url_rewrite_subprocess_env_many(
        [(repo, remote(name)) for name, repo in world.repos.items()]
    )
    return world


def test_repository_with_its_subdirectory_package_is_rejected(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _world(tmp_path, apm_binary_path, root_deps=[])
    world.manifest(["test/mono"])
    world.apm("install", NO_POLICY)
    lock_before = world.lock_bytes()
    modules_before = world.modules()

    world.manifest(["test/mono", "test/mono/packages/x"])
    for command, code in (
        (("install", NO_POLICY), 1),
        (("update", "--yes"), 1),
        # Dry-run reports resolution errors as an incomplete preview.
        (("install", "--dry-run", "--verbose", NO_POLICY), 0),
    ):
        result = world.apm(*command, expect=code)
        assert _REJECTED in " ".join((result.stdout + result.stderr).split()), command
        assert world.lock_bytes() == lock_before
        assert world.modules() == modules_before

    # Keeping only the subdirectory package converges in one run.
    world.manifest(["test/mono/packages/x"])
    world.apm("install", NO_POLICY)
    assert world.modules() == {"test/mono/packages/x"}
    assert world.deployed() == {"x.instructions.md"}
    lock_after = world.lock_bytes()
    world.apm("install", NO_POLICY)
    assert world.lock_bytes() == lock_after
    world.cross_check(present={"test/mono/packages/x"})


def test_repository_depending_on_its_own_subdirectory_still_installs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _world(tmp_path, apm_binary_path, root_deps=["./packages/x"])
    world.manifest(["test/mono"])

    world.apm("install", NO_POLICY)
    assert world.modules() == {"test/mono", "test/mono/packages/x"}
    assert world.locked()["test/mono/packages/x"]["resolved_by"] == "test/mono"
    world.cross_check(present={"test/mono"})
