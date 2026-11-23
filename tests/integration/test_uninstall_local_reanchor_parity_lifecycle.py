"""A re-anchoring uninstall leaves what a fresh install would write (#4999, #4889).

PROMISE: P-INV-I3 -- after ``apm uninstall ./vendor/e`` re-anchors a still-needed
local ``e``, a following ``apm install --no-frozen`` on the same inputs rewrites
neither ``apm.lock.yaml`` nor ``apm_modules/``: the materialization directory moved
to the re-anchored install path, declaring edges follow install's breadth-first
order across production and dev roots, and replacement requests follow the
post-uninstall resolver order (source: #4999 scope record).
CELL: OP=uninstall,install SRC=local PRIM=skill SCOPE=project LOCK=consistent
GRAPH=direct-also-transitive | cross-root diamond | replaced edge
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.integration._install_audit_replay_kit import ReplayWorld, lock_bytes

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I3", "P-INV-I1"),
]

_REPLACEMENTS_SCHEMA = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
)


def _skill_package(root: Path, name: str, deps: tuple[str, ...] = ()) -> None:
    manifest = f"name: {name}\nversion: 1.0.0\n"
    if deps:
        manifest += "dependencies:\n  apm:\n" + "".join(f"    - {dep}\n" for dep in deps)
    root.mkdir(parents=True, exist_ok=True)
    (root / "apm.yml").write_text(manifest, encoding="utf-8")
    skill = root / f".apm/skills/{name}-skill/SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        f"---\nname: {name}-skill\ndescription: Skill of {name}\n---\n# {name}\n",
        encoding="utf-8",
    )


def _modules(project: Path) -> dict[str, bytes]:
    """Every file under ``apm_modules/`` mapped to its bytes."""
    root = project / "apm_modules"
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _outside_modules(project: Path) -> dict[str, bytes]:
    """Every file outside ``apm_modules/`` but apm.yml and apm.lock.yaml, by its bytes."""
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted(project.rglob("*"))
        if path.is_file()
        and path.relative_to(project).parts[0] not in ("apm_modules", ".git")
        and path.name not in ("apm.yml", "apm.lock.yaml")
    }


def _world(tmp_path: Path, apm_binary_path: Path, packages, manifest: dict) -> ReplayWorld:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    for name, deps in packages:
        _skill_package(world.project / "vendor" / name, name, deps)
    dump_yaml(
        {"name": "proj", "version": "1.0.0", "targets": ["claude"], **manifest},
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")
    return world


def _assert_no_frozen_rewrites_nothing(world: ReplayWorld) -> None:
    """A REAL ``apm install --no-frozen`` on the same inputs changes no lock or cache byte."""
    lock, modules = lock_bytes(world.project), _modules(world.project)
    world.apm("install", "--no-frozen")
    assert lock_bytes(world.project) == lock
    assert _modules(world.project) == modules
    world.apm("install", "--frozen")


def test_reanchor_moves_the_materialization_directory(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """e -> f direct, dev d -> e: e's apm_modules copy moves to its re-anchored path."""
    world = _world(
        tmp_path,
        apm_binary_path,
        (("f", ()), ("e", ("../f",)), ("d", ("../e",))),
        {"dependencies": {"apm": ["./vendor/e"]}, "devDependencies": {"apm": ["./vendor/d"]}},
    )
    outside = _outside_modules(world.project)

    world.apm("uninstall", "./vendor/e")

    assert not (world.project / "apm_modules/_local/e").exists()
    assert _outside_modules(world.project) == outside
    _assert_no_frozen_rewrites_nothing(world)


def test_reanchor_follows_breadth_first_declaring_edges_across_roots(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Production p -> q -> e -> f, dev b -> a -> f and d -> e: f's first edge is a's."""
    world = _world(
        tmp_path,
        apm_binary_path,
        (
            ("f", ()),
            ("e", ("../f",)),
            ("q", ("../e",)),
            ("p", ("../q",)),
            ("a", ("../f",)),
            ("b", ("../a",)),
            ("d", ("../e",)),
        ),
        {
            "dependencies": {"apm": ["./vendor/p", "./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/b", "./vendor/d"]},
        },
    )

    world.apm("uninstall", "./vendor/e")

    _assert_no_frozen_rewrites_nothing(world)


def test_reanchor_rebuilds_replacement_requests_in_resolver_order(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Direct e -> replaced r, dev d -> e and a -> r: r's requests follow install's order."""
    world = _world(
        tmp_path,
        apm_binary_path,
        (("r", ()), ("e", ("test/r",)), ("d", ("../e",)), ("a", ("test/r",))),
        {
            "$schema": _REPLACEMENTS_SCHEMA,
            "dependencies": {"apm": ["./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/d", "./vendor/a"]},
            "replace": [
                {"for": {"git": "https://github.com/test/r"}, "with": {"path": "./vendor/r"}}
            ],
        },
    )

    world.apm("uninstall", "./vendor/e")

    _assert_no_frozen_rewrites_nothing(world)


def _aliased_package(root: Path, name: str, child: str, alias: str) -> None:
    _skill_package(root, name)
    (root / "apm.yml").write_text(
        f"name: {name}\nversion: 1.0.0\ndependencies:\n  apm:\n"
        f"    - path: {child}\n      alias: {alias}\n",
        encoding="utf-8",
    )


def test_a_reparented_descendant_under_another_alias_refuses(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """e declares f as old-f, dev a declares f as new-f: f's winning edge installs elsewhere."""
    from tests.integration._install_audit_replay_kit import tree_digest

    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "f", "f")
    _aliased_package(vendor / "e", "e", "../f", "old-f")
    _aliased_package(vendor / "a", "a", "../f", "new-f")
    _skill_package(vendor / "d", "d", ("../e",))
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/d", "./vendor/a"]},
        },
        world.project / "apm.yml",
    )
    (world.project / ".claude").mkdir()
    world.apm("install")
    before = (tree_digest(world.project), _modules(world.project))

    refused = world.apm("uninstall", "./vendor/e", expect=1)

    output = " ".join((refused.stdout + refused.stderr).split())
    assert "under another name (new-f)" in output, output
    assert (tree_digest(world.project), _modules(world.project)) == before
