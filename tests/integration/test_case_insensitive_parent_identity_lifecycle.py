"""A parent declared with different owner/repo casing keeps its graph edges (#3649).

GitHub owner and repository names are case-insensitive, so ``Test/C`` and
``test/c`` name one package and one lock entry.  The transitive ``test/e``
must record that entry as its parent, so ``deps tree`` places it,
``deps why`` walks the chain, and switching the declared casing churns only
the recorded spelling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import NO_POLICY, World, remote

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]


def _world(tmp_path: Path, apm_binary_path: Path) -> World:
    world = World.create(tmp_path, apm_binary_path)
    world.publish("e")
    world.publish("c", [remote("e")])
    routes = [(repo, remote(name)) for name, repo in world.repos.items()]
    routes.append((world.repos["c"], "https://github.com/Test/C.git"))
    world.env = world.factory.url_rewrite_subprocess_env_many(routes)
    return world


def _assert_e_placed_under_c(world: World) -> None:
    locked = world.locked()
    assert set(locked) == {"test/c", "test/e"}
    assert locked["test/e"]["resolved_by"] == "test/c"
    tree = world.apm("deps", "tree").stdout
    assert "could not place" not in tree, tree
    why = world.apm("deps", "why", "test/e").stdout
    assert "test/c" in why and "declared in apm.yml" in why, why
    # deps list names packages by their source-cased apm_modules path.
    listing = world.apm("deps", "list").stdout.lower()
    assert "test/c" in listing and "test/e" in listing and "orphaned" not in listing
    assert "No orphaned packages found" in world.apm("prune", "--dry-run").stdout
    world.apm("audit", "--ci", NO_POLICY)


def test_mixed_case_parent_records_canonical_parent_identity(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _world(tmp_path, apm_binary_path)
    world.manifest(["Test/C"])

    world.apm("install", NO_POLICY)
    assert world.locked()["test/c"]["materialization_repo_url"] == "Test/C"
    _assert_e_placed_under_c(world)
    lock_before = world.lock_bytes()
    world.apm("install", NO_POLICY)
    assert world.lock_bytes() == lock_before

    # Re-declaring the same package in lowercase drops only the spelling.
    world.manifest(["test/c"])
    world.apm("install", NO_POLICY)
    assert "materialization_repo_url" not in world.locked()["test/c"]
    _assert_e_placed_under_c(world)
    lock_before = world.lock_bytes()
    world.apm("install", NO_POLICY)
    assert world.lock_bytes() == lock_before

    # Uninstall by the other casing removes the package and its transitive.
    world.apm("uninstall", "Test/C")
    assert world.deployed() == set()
    assert not world.modules()
