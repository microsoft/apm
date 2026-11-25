"""``apm update`` refreshes a changed local dependency like ``apm install`` (#5463).

PROMISE: P-INV-I1 -- after ``apm update`` the lockfile matches the deployed
files: a local dependency whose content changed is re-snapshotted and
redeployed, so ``install --frozen`` then succeeds (source:
install/helpers/plan_inputs.py build_resolved_update_plan).
PROMISE: P-INV-I2 -- an update with no local change writes nothing.
CELL: OP=update SRC=local-path PRIM=skill SCOPE=project LOCK=consistent UPDATE
"""

from __future__ import annotations

import shutil

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.world.contract import World
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I1", "P-INV-I2", "P-INV-I7", "P-INV-I8"),
]

_NO_OP = "All dependencies already at their latest matching refs."
_REMOTE = "lifecycle/p-skill"


def _text(result) -> str:
    return result.stdout + result.stderr


def _ok(result, label: str) -> None:
    assert result.returncode == 0, f"{label}: {_text(result)}"


def _body(name: str) -> str:
    return f"---\nname: {name}\ndescription: Local skill {name}.\n---\n\nUse {name} v1.\n"


def _vendor(world: World, *names: str) -> None:
    factory = LocalPackageFactory(world.project / "vendor")
    for name in names:
        factory.add_skill(factory.create(name, version="1.0.0"), name, _body(name))


def _skill_source(world: World, name: str):
    matches = sorted((world.project / "vendor" / name).rglob("SKILL.md"))
    assert len(matches) == 1, matches
    return matches[0]


def _deployed(world: World, name: str) -> str:
    return (world.project / ".agents" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def _bump(world: World, name: str, version: str = "1.1.0") -> None:
    source = _skill_source(world, name)
    source.write_text(
        source.read_text(encoding="utf-8").replace(" v1.", f" v{version}."), encoding="utf-8"
    )
    manifest = world.project / "vendor" / name / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["version"] = version
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _row(world: World, name: str) -> dict:
    lock = yaml.safe_load((world.project / "apm.lock.yaml").read_text(encoding="utf-8"))
    wanted = name if name == _REMOTE else f"./vendor/{name}"
    rows = [
        row
        for row in lock["dependencies"]
        if wanted in (row.get("local_path"), row.get("repo_url"))
    ]
    assert len(rows) == 1, lock["dependencies"]
    return rows[0]


def _snapshot(world: World) -> LifecycleStateSnapshot:
    return LifecycleStateSnapshot.capture(world.project)


def test_local_version_bump_through_outdated_and_update(lifecycle_world: World) -> None:
    """A changed local package is redeployed by ``update -y``; a remote one is untouched."""
    world = lifecycle_world
    _vendor(world, "a")
    _ok(driver.run(world, ["install", "./vendor/a", _REMOTE]), "install")
    assert "Use a v1." in _deployed(world, "a")
    old_local = _row(world, "a")
    old_remote = _row(world, _REMOTE)

    _bump(world, "a")
    outdated = driver.run(world, ["outdated"])
    assert outdated.returncode in (0, 1), _text(outdated)

    frozen = driver.run(world, ["install", "--frozen"])
    assert frozen.returncode == 1, _text(frozen)
    assert "local dependency content does not match apm.lock.yaml" in _text(frozen)

    updated = driver.run(world, ["update", "-y"])
    _ok(updated, "update")
    assert _NO_OP not in _text(updated), _text(updated)
    assert "Use a v1.1.0." in _deployed(world, "a"), _text(updated)
    new_local = _row(world, "a")
    assert new_local["content_hash"] != old_local["content_hash"]
    assert new_local.get("version") == "1.1.0", new_local
    assert _row(world, _REMOTE) == old_remote

    _ok(driver.run(world, ["install", "--frozen"]), "install --frozen after update")
    # The rows update wrote are the ones install writes for the same tree.
    updated_state = _snapshot(world)
    _ok(driver.run(world, ["install"]), "install after update")
    assert _snapshot(world) == updated_state

    converged = _snapshot(world)
    again = driver.run(world, ["update", "-y"])
    _ok(again, "converged update")
    assert _NO_OP in _text(again), _text(again)
    assert _snapshot(world) == converged


def test_update_without_local_change_writes_nothing(lifecycle_world: World) -> None:
    world = lifecycle_world
    _vendor(world, "a")
    _ok(driver.run(world, ["install", "./vendor/a"]), "install")
    before = _snapshot(world)

    result = driver.run(world, ["update", "-y"])

    _ok(result, "update")
    assert _NO_OP in _text(result), _text(result)
    assert _snapshot(world) == before


def test_update_named_local_refreshes_only_that_dependency(lifecycle_world: World) -> None:
    world = lifecycle_world
    _vendor(world, "a", "b")
    _ok(driver.run(world, ["install", "./vendor/a", "./vendor/b"]), "install")
    old_b = _row(world, "b")
    _bump(world, "a")
    _bump(world, "b")

    result = driver.run(world, ["update", "-y", "./vendor/a"])

    _ok(result, "update ./vendor/a")
    assert "Use a v1.1.0." in _deployed(world, "a"), _text(result)
    assert "Use b v1." in _deployed(world, "b"), _text(result)
    assert "v1.1.0" not in _deployed(world, "b"), _text(result)
    assert _row(world, "b") == old_b


def test_update_with_deleted_local_source_names_the_fix(lifecycle_world: World) -> None:
    world = lifecycle_world
    _vendor(world, "a")
    _ok(driver.run(world, ["install", "./vendor/a"]), "install")
    shutil.rmtree(world.project / "vendor" / "a")
    before = _snapshot(world)

    result = driver.run(world, ["update", "-y"])

    assert result.returncode == 1, _text(result)
    assert _NO_OP not in _text(result), _text(result)
    assert "./vendor/a" in _text(result), _text(result)
    assert "Restore the source or correct the dependency path" in _text(result)
    assert "'apm install'" in _text(result), _text(result)
    assert _snapshot(world) == before


def test_update_keeps_an_unchanged_legacy_plugin_and_refreshes_a_transitive_local(
    lifecycle_world: World,
) -> None:
    """A plugin without apm.yml is normalized after its copy; that is no change.

    A local package's own local dependency is refreshed when it changes.
    """
    world = lifecycle_world
    plugin = world.project / "vendor" / "plug"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "plug", "version": "1.0.0"}', encoding="utf-8"
    )
    factory = LocalPackageFactory(world.project / "vendor")
    factory.add_skill(factory.create("u", version="1.0.0"), "u", _body("u"))
    parent = factory.create("t", version="1.0.0", dependencies=["../u"])
    factory.add_skill(parent, "t", _body("t"))
    plugin_skill = plugin / "skills" / "ps" / "SKILL.md"
    plugin_skill.parent.mkdir(parents=True)
    plugin_skill.write_text(_body("ps"), encoding="utf-8")
    _ok(driver.run(world, ["install", "./vendor/plug", "./vendor/t"]), "install")
    before = _snapshot(world)

    unchanged = driver.run(world, ["update", "-y"])

    _ok(unchanged, "unchanged update")
    assert _NO_OP in _text(unchanged), _text(unchanged)
    assert _snapshot(world) == before

    _bump(world, "u")
    changed = driver.run(world, ["update", "-y"])

    _ok(changed, "update")
    assert "Use u v1.1.0." in _deployed(world, "u"), _text(changed)
    assert "Updated 1 APM dependency." in _text(changed), _text(changed)
    _ok(driver.run(world, ["install", "--frozen"]), "install --frozen after update")


@pytest.mark.parametrize("named", [False, True], ids=["all", "named-parent"])
def test_update_with_deleted_transitive_local_names_the_fix(
    lifecycle_world: World, named: bool
) -> None:
    """A deleted local dependency of an unchanged local package fails the update."""
    world = lifecycle_world
    factory = LocalPackageFactory(world.project / "vendor")
    factory.add_skill(factory.create("u", version="1.0.0"), "u", _body("u"))
    parent = factory.create("t", version="1.0.0", dependencies=["../u"])
    factory.add_skill(parent, "t", _body("t"))
    _ok(driver.run(world, ["install", "./vendor/t"]), "install")
    shutil.rmtree(world.project / "vendor" / "u")
    before = _snapshot(world)

    result = driver.run(world, ["update", "-y", *(["./vendor/t"] if named else [])])

    assert result.returncode == 1, _text(result)
    assert _NO_OP not in _text(result), _text(result)
    assert "Local package path does not exist: ../u" in _text(result), _text(result)
    assert "'apm install'" in _text(result), _text(result)
    assert _snapshot(world) == before
