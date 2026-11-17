"""One package is declared once across dependencies and devDependencies (#3693).

npm semantics: ``apm install --dev X`` with X in ``dependencies`` moves it to
``devDependencies``; a plain ``apm install X`` keeps X where
``devDependencies`` already declares it.  ``apm uninstall X`` removes every
declaration of X -- both sections and every spelling -- so a hand-written
duplicate is never stuck.

Packages named on the ``apm install`` command line are declared under
``GENERIC_HOST`` so their validation probe never calls the github.com API.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._dependency_graph_world import GENERIC_HOST, NO_POLICY, World, remote

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_C = f"{GENERIC_HOST}/test/c"


def _world(tmp_path: Path, apm_binary_path: Path, *, c_spec: str = _C) -> World:
    world = World.create(tmp_path, apm_binary_path)
    world.serve_on(GENERIC_HOST)
    world.publish("e")
    world.publish("c", [remote("e")])
    world.publish("d")
    world.manifest([c_spec, "test/d"])
    world.apm("install", NO_POLICY)
    return world


def _sections(world: World) -> tuple[list[object], list[object]]:
    manifest = load_yaml(world.project / "apm.yml")
    prod = (manifest.get("dependencies") or {}).get("apm") or []
    dev = (manifest.get("devDependencies") or {}).get("apm") or []
    return prod, dev


def test_install_dev_moves_and_plain_install_keeps(tmp_path: Path, apm_binary_path: Path) -> None:
    world = _world(tmp_path, apm_binary_path)

    result = world.apm("install", "--dev", _C, NO_POLICY)
    assert f"Moved {_C} from dependencies to devDependencies" in result.stdout
    assert _sections(world) == (["test/d"], [_C])
    locked = world.locked()
    assert locked["test/c"]["is_dev"] is True
    assert locked["test/e"]["is_dev"] is True
    lock_before = world.lock_bytes()
    world.apm("install", NO_POLICY)
    assert world.lock_bytes() == lock_before

    # A plain install of a dev-declared package keeps it in devDependencies.
    world.apm("install", _C, NO_POLICY)
    assert _sections(world) == (["test/d"], [_C])
    assert world.lock_bytes() == lock_before
    world.cross_check(present={"test/c", "test/d", "test/e"})

    world.apm("uninstall", _C)
    assert _sections(world) == (["test/d"], [])
    assert world.deployed() == {"d.instructions.md"}
    world.cross_check(present={"test/d"}, absent=frozenset({"test/c", "test/e"}))


def test_uninstall_removes_every_declaration_of_one_package(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _world(tmp_path, apm_binary_path, c_spec="test/c")
    manifest = load_yaml(world.project / "apm.yml")
    manifest["dependencies"]["apm"] = ["test/c", remote("c"), "test/d"]
    manifest["devDependencies"] = {"apm": [{"git": remote("c")}]}
    dump_yaml(manifest, world.project / "apm.yml")
    world.apm("install", NO_POLICY)

    preview = world.apm("uninstall", "https://github.com/test/c", "--dry-run")
    assert "Ambiguous" not in preview.stdout
    assert _sections(world)[1] == [{"git": remote("c")}]

    world.apm("uninstall", "https://github.com/test/c")
    assert _sections(world) == (["test/d"], [])
    assert world.modules() == {"test/d"}
    assert world.deployed() == {"d.instructions.md"}
    lock_before = world.lock_bytes()
    world.apm("install", NO_POLICY)
    assert world.lock_bytes() == lock_before
    world.cross_check(present={"test/d"}, absent=frozenset({"test/c", "test/e"}))


def test_cross_section_moves_keep_the_declared_pin(tmp_path: Path, apm_binary_path: Path) -> None:
    """A move or keep reuses the existing declaration's ref and skills (#3693)."""
    world = World.create(tmp_path, apm_binary_path)
    world.serve_on(GENERIC_HOST)
    rules = world.publish("c") / ".apm" / "instructions" / "c.instructions.md"
    releases = []
    for text in ("c v1", "c v2 on main"):
        rules.write_text(f"---\napplyTo: '**/*.py'\n---\n{text}\n", encoding="ascii")
        releases.append(world.factory.commit(world.repos["c"], message=text))
    world.factory.tag(world.repos["c"], "v1.0.0", releases[0])
    first = releases[0]
    pinned = {"git": remote("c", GENERIC_HOST), "ref": "v1.0.0"}
    world.manifest([pinned])
    world.apm("install", NO_POLICY)
    lock_before = world.lock_bytes()
    assert world.locked()["test/c"]["resolved_commit"] == first.sha

    # --dev moves the pinned declaration, not a bare copy tracking main.
    world.apm("install", "--dev", _C, NO_POLICY)
    assert _sections(world) == ([], [pinned])
    locked = world.locked()["test/c"]
    assert locked["resolved_commit"] == first.sha and locked["is_dev"] is True

    # A bare install keeps the dev declaration and its pin untouched.
    manifest_before = (world.project / "apm.yml").read_bytes()
    lock_dev = world.lock_bytes()
    world.apm("install", _C, NO_POLICY)
    assert (world.project / "apm.yml").read_bytes() == manifest_before
    assert world.lock_bytes() == lock_dev
    assert lock_dev != lock_before
    world.cross_check(present={"test/c"})
