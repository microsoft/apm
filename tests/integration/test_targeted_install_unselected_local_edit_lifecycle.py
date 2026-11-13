"""Targeted install and update ignore an unselected local package mid-edit (#3649).

``apm install PKG``, ``apm install --update PKG`` and ``apm update PKG``
re-read only the selected subtree (#3571 scope).  An unrelated local package
whose apm.yml is being edited -- invalid YAML, or a dependency that is not
published yet -- must neither fail the targeted command nor be fetched into
apm_modules behind the lockfile's back.  The next full install then
reconciles the edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import (
    NO_POLICY,
    World,
    remote,
    set_deps,
    write_package,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]


def _world(tmp_path: Path, apm_binary_path: Path) -> World:
    world = World.create(tmp_path, apm_binary_path)
    for name in ("c", "d", "f", "g"):
        world.publish(name)
    write_package(world.project / "packages" / "a", "a", [remote("c")])
    write_package(world.project / "packages" / "b", "b", [remote("d")])
    world.manifest(["./packages/a", "./packages/b", remote("f")])
    world.apm("install", NO_POLICY)
    return world


def _assert_untouched(world: World, lock_before: bytes, modules_before: set[str]) -> None:
    assert world.lock_bytes() == lock_before
    assert world.modules() == modules_before
    assert "No orphaned packages found" in world.apm("prune", "--dry-run").stdout


@pytest.mark.parametrize(
    "edit",
    ["new-dependency", "invalid-yaml", "unpublished-dependency"],
)
def test_targeted_commands_skip_unselected_local_edit(
    tmp_path: Path, apm_binary_path: Path, edit: str
) -> None:
    world = _world(tmp_path, apm_binary_path)
    world.cross_check(present={"test/c", "test/d", "test/f"})
    lock_before = world.lock_bytes()
    modules_before = world.modules()

    sibling = world.project / "packages" / "b"
    if edit == "new-dependency":
        set_deps(sibling, [remote("g")])
    elif edit == "invalid-yaml":
        (sibling / "apm.yml").write_text("name: b\ndependencies: [\n", encoding="ascii")
    else:
        set_deps(sibling, ["https://github.com/test/unpublished.git"])

    for command in (
        ("install", "./packages/a", NO_POLICY),
        ("install", "test/f", NO_POLICY),
        ("install", "--update", "test/f", NO_POLICY),
        ("update", "test/f", "--yes"),
    ):
        world.apm(*command)
        _assert_untouched(world, lock_before, modules_before)
    assert "test/g" not in world.modules()

    if edit != "new-dependency":
        # A full install still reads every local manifest and fails closed.
        world.apm("install", NO_POLICY, expect=1)
        assert world.lock_bytes() == lock_before
        return

    # The next full install reconciles the sibling's edit in one run.
    world.apm("install", NO_POLICY)
    locked = world.locked()
    assert "test/d" not in locked
    assert locked["test/g"]["resolved_by"] == "_local/b"
    lock_after = world.lock_bytes()
    world.apm("install", NO_POLICY)
    assert world.lock_bytes() == lock_after
    world.apm("prune")
    world.cross_check(present={"test/c", "test/f", "test/g"}, absent=frozenset({"test/d"}))


def test_targeted_install_rereads_selected_local_package(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The selected package's own edit is still picked up in one run (#3571)."""
    world = _world(tmp_path, apm_binary_path)
    set_deps(world.project / "packages" / "a", [remote("g")])
    set_deps(world.project / "packages" / "b", [remote("c")])

    world.apm("install", "./packages/a", NO_POLICY)
    locked = world.locked()
    assert locked["test/g"]["resolved_by"] == "_local/a"
    # The unselected sibling keeps its locked children.
    assert locked["test/d"]["resolved_by"] == "_local/b"
    assert "g.instructions.md" in world.deployed()
