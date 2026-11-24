"""Prune repairs survivor lock metadata in multi-path graphs (#5126).

PROMISE: P-OWN-orphan-package-selection -- after ``apm prune`` removes a
transitive local orphan, every surviving row it walks records the depth,
dev status and declaring parent a fresh resolve of the same manifest would
record, so a following ``apm install --no-frozen`` changes no lockfile bytes
and ``apm install --frozen`` still passes (source: #5126 scope record).
CELL: OP=prune SRC=local PRIM=skill SCOPE=project LOCK=consistent
GRAPH=multi-path (prod and dev chains share a survivor).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, lock_bytes

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-orphan-package-selection", "P-INV-I2", "P-INV-I3"),
]


def _skill_package(root: Path, name: str, deps: tuple[str, ...] = ()) -> None:
    manifest: dict = {"name": name, "version": "1.0.0"}
    if deps:
        manifest["dependencies"] = {"apm": list(deps)}
    root.mkdir(parents=True, exist_ok=True)
    dump_yaml(manifest, root / "apm.yml")
    skill = root / ".apm" / "skills" / f"{name}-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        f"---\nname: {name}-skill\ndescription: Skill of {name}\n---\n# {name}\n",
        encoding="utf-8",
    )


def _manifest(world: ReplayWorld, prod: list[str], dev: list[str]) -> None:
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": prod},
            "devDependencies": {"apm": dev},
        },
        world.project / "apm.yml",
    )


def _row(world: ReplayWorld, name: str) -> dict:
    lock = load_yaml(world.project / "apm.lock.yaml")
    return next(row for row in lock["dependencies"] if row.get("name") == name)


def _world(tmp_path: Path, apm_binary_path: Path, packages, prod, dev) -> ReplayWorld:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    for name, deps in packages:
        _skill_package(world.project / "vendor" / name, name, deps)
    _manifest(world, prod, dev)
    (world.project / ".claude").mkdir()
    world.apm("install")
    return world


def _assert_prune_converges(world: ReplayWorld) -> None:
    """Frozen replay keeps the pruned lock, and so does a fresh resolve."""
    pruned = lock_bytes(world.project)
    world.apm("install", "--frozen")
    assert lock_bytes(world.project) == pruned
    world.apm("install", "--no-frozen")
    assert lock_bytes(world.project) == pruned, (world.project / "apm.lock.yaml").read_text()


def test_prune_recomputes_depth_and_dev_status_of_a_survivor_left_on_the_dev_chain(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """prod d -> e -> f -> g and dev a -> z -> w -> f; dropping d leaves f and g dev-only."""
    world = _world(
        tmp_path,
        apm_binary_path,
        (
            ("g", ()),
            ("f", ("../g",)),
            ("e", ("../f",)),
            ("d", ("../e",)),
            ("w", ("../f",)),
            ("z", ("../w",)),
            ("a", ("../z",)),
        ),
        ["./vendor/d"],
        ["./vendor/a"],
    )
    assert _row(world, "g")["depth"] == 4
    _manifest(world, [], ["./vendor/a"])

    world.apm("prune")

    assert _row(world, "f")["depth"] == 4
    assert _row(world, "g")["depth"] == 5
    assert _row(world, "f").get("is_dev") is True
    assert _row(world, "g").get("is_dev") is True
    assert (world.project / ".claude/skills/g-skill/SKILL.md").is_file()
    _assert_prune_converges(world)


def test_prune_reparents_a_survivor_whose_declaring_parent_was_pruned(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """prod d -> z -> f and dev a -> y -> b -> f: f was recorded under z; after prune, under b."""
    world = _world(
        tmp_path,
        apm_binary_path,
        (
            ("f", ()),
            ("z", ("../f",)),
            ("d", ("../z",)),
            ("b", ("../f",)),
            ("y", ("../b",)),
            ("a", ("../y",)),
        ),
        ["./vendor/d"],
        ["./vendor/a"],
    )
    assert _row(world, "f")["resolved_by"] == "_local/z"
    _manifest(world, [], ["./vendor/a"])

    world.apm("prune")

    assert _row(world, "f")["resolved_by"] == "_local/b"
    assert _row(world, "f")["depth"] == 4
    assert _row(world, "f").get("is_dev") is True
    assert (world.project / ".claude/skills/f-skill/SKILL.md").is_file()
    _assert_prune_converges(world)
