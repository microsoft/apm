"""Uninstalling a direct dependency another package still needs keeps its lock row (#4749).

PROMISE: P-INV-I3 -- after ``apm uninstall e``, where a remaining direct
dependency ``d`` also depends on ``e``, ``apm install --frozen`` and
``apm audit --ci`` still agree the project is current, because ``e``'s lock
row and deployed files stay, re-attributed to ``d``
(source: #4749 scope record).
CELL: OP=uninstall SRC=git PRIM=skill SCOPE=project LOCK=consistent
GRAPH=direct-also-transitive | control: GRAPH=direct-unreachable

A dependency no other package reaches is still removed in full.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._install_audit_replay_kit import (
    ReplayWorld,
    deployed_files,
    lock_bytes,
    tree_digest,
    write,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I3", "P-INV-I1", "P-INV-I2"),
]


def _skill_package(root: Path, name: str, deps: tuple[str, ...] = ()) -> None:
    """One skill named after the package, so packages never share a deployed path."""
    manifest = f"name: {name}\nversion: 1.0.0\n"
    if deps:
        manifest += "dependencies:\n  apm:\n" + "".join(f"    - {dep}\n" for dep in deps)
    write(root / "apm.yml", manifest)
    write(
        root / f".apm/skills/{name}-skill/SKILL.md",
        f"---\nname: {name}-skill\ndescription: Skill of {name}\n---\n# {name}\n",
    )


def _parent_of(child: str):
    """Author a package that declares ``test/<child>`` as its own dependency."""

    def _author(root: Path, name: str) -> None:
        _skill_package(root, name, (f"test/{child}",))

    return _author


def _world(tmp_path: Path, apm_binary_path: Path) -> ReplayWorld:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    world.publish("e", _skill_package)
    world.publish("d", _parent_of("e"))
    world.publish("f", _skill_package)
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["test/d", "test/e", "test/f"]},
        },
        world.project / "apm.yml",
    )
    world.apm("install")
    world.apm("install", "--frozen")
    return world


def _rows(world: ReplayWorld) -> dict[str, dict]:
    lock = load_yaml(world.project / "apm.lock.yaml")
    return {row["repo_url"]: row for row in lock["dependencies"]}


def _row_files(world: ReplayWorld, repo_url: str) -> set[str]:
    return set(_rows(world)[repo_url].get("deployed_files") or [])


def test_uninstall_keeps_a_direct_dependency_another_package_still_needs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _world(tmp_path, apm_binary_path)
    before = _rows(world)
    assert before["test/e"].get("resolved_by") is None
    e_files = _row_files(world, "test/e")
    assert e_files
    tree_before = tree_digest(world.project)

    # The preview names the declaration-only removal and writes nothing.
    preview = world.apm("uninstall", "test/e", "--dry-run")
    assert (
        "Would remove test/e from apm.yml only; it stays installed because "
        "test/d still depends on it"
    ) in " ".join(preview.stdout.split())
    assert tree_digest(world.project) == tree_before

    result = world.apm("uninstall", "test/e")
    assert "test/e stays installed because test/d still depends on it" in " ".join(
        result.stdout.split()
    )

    manifest = load_yaml(world.project / "apm.yml")
    assert manifest["dependencies"]["apm"] == ["test/d", "test/f"]
    after = _rows(world)
    # e stays locked, now attributed to the package that still needs it.
    assert set(after) == {"test/d", "test/e", "test/f"}
    assert after["test/e"]["resolved_by"] == "test/d"
    # No longer direct: one level below its new parent, as install records it.
    assert before["test/e"].get("depth", 1) == 1
    assert after["test/e"]["depth"] == 2
    assert after["test/e"]["resolved_commit"] == before["test/e"]["resolved_commit"]
    assert set(after["test/e"].get("deployed_files") or []) == e_files
    assert (world.project / "apm_modules" / "test" / "e" / "apm.yml").is_file()
    # Only apm.yml and the lockfile changed; every deployed file is intact.
    tree_after = tree_digest(world.project)
    changed = {
        path
        for path in set(tree_before) | set(tree_after)
        if tree_before.get(path) != tree_after.get(path)
    }
    assert changed == {"apm.yml", "apm.lock.yaml"}

    # I3: both readers agree the uninstalled-but-still-needed state is current.
    lock_after = lock_bytes(world.project)
    world.apm("install", "--frozen")
    world.apm("audit", "--ci")
    assert lock_bytes(world.project) == lock_after
    assert tree_digest(world.project) == tree_after
    # I2: a full install re-resolves the graph and writes the same lockfile
    # (--no-frozen: under CI=true an install with a lockfile defaults to frozen).
    world.apm("install", "--no-frozen")
    assert lock_bytes(world.project) == lock_after


def test_uninstall_still_removes_a_dependency_nothing_else_needs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = _world(tmp_path, apm_binary_path)
    f_files = _row_files(world, "test/f")
    assert f_files

    world.apm("uninstall", "test/f")

    assert load_yaml(world.project / "apm.yml")["dependencies"]["apm"] == ["test/d", "test/e"]
    assert set(_rows(world)) == {"test/d", "test/e"}
    assert not (world.project / "apm_modules" / "test" / "f").exists()
    assert not any((world.project / path).exists() for path in f_files)
    assert not (f_files & deployed_files(world.project))
    world.apm("install", "--frozen")


def _graph_world(
    tmp_path: Path,
    apm_binary_path: Path,
    packages: tuple[tuple[str, str | None], ...],
    direct: list[str],
    dev: list[str] | None = None,
) -> ReplayWorld:
    """Publish ``(name, child)`` packages in order and install the project (#4804)."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    for name, child in packages:
        world.publish(name, _parent_of(child) if child else _skill_package)
    manifest = {
        "name": "proj",
        "version": "1.0.0",
        "targets": ["claude"],
        "dependencies": {"apm": direct},
    }
    if dev:
        manifest["devDependencies"] = {"apm": dev}
    dump_yaml(manifest, world.project / "apm.yml")
    world.apm("install")
    return world


def _assert_install_rewrites_nothing(world: ReplayWorld) -> None:
    """I2: the repaired lockfile is what a full install of the same graph writes."""
    lock_after_uninstall = lock_bytes(world.project)
    world.apm("install", "--no-frozen")
    assert lock_bytes(world.project) == lock_after_uninstall


def test_uninstall_reparents_to_the_nearest_parent_by_lowest_key(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4804: two parents at equal depth; install's tie rule picks the lowest key."""
    world = _graph_world(
        tmp_path,
        apm_binary_path,
        (("e", None), ("b", "e"), ("a", "e")),
        ["test/b", "test/a", "test/e"],
    )
    world.apm("uninstall", "test/e")
    assert _rows(world)["test/e"]["resolved_by"] == "test/a"
    _assert_install_rewrites_nothing(world)


def test_uninstall_of_chained_still_needed_roots_derives_depth_parent_first(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4804: d -> e -> f with all three direct; removing e and f nests f below e."""
    world = _graph_world(
        tmp_path,
        apm_binary_path,
        (("f", None), ("e", "f"), ("d", "e")),
        ["test/d", "test/e", "test/f"],
    )
    world.apm("uninstall", "test/e", "test/f")
    rows = _rows(world)
    assert (rows["test/e"]["resolved_by"], rows["test/e"]["depth"]) == ("test/d", 2)
    assert (rows["test/f"]["resolved_by"], rows["test/f"]["depth"]) == ("test/e", 3)
    _assert_install_rewrites_nothing(world)


def test_uninstall_under_a_dev_only_parent_moves_the_subtree_to_dev(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4804: production e -> f; once only dev d needs e, f is dev-only too."""
    world = _graph_world(
        tmp_path,
        apm_binary_path,
        (("f", None), ("e", "f"), ("d", "e")),
        ["test/e"],
        dev=["test/d"],
    )
    assert not _rows(world)["test/f"].get("is_dev")
    world.apm("uninstall", "test/e")
    rows = _rows(world)
    assert rows["test/e"].get("is_dev") is True
    assert rows["test/f"].get("is_dev") is True
    _assert_install_rewrites_nothing(world)


def test_uninstall_tie_rule_spans_production_and_dev_parents(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4804: production b and dev a both need e; install's tie ignores dev status."""
    world = _graph_world(
        tmp_path,
        apm_binary_path,
        (("e", None), ("b", "e"), ("a", "e")),
        ["test/b", "test/e"],
        dev=["test/a"],
    )
    world.apm("uninstall", "test/e")
    assert not _rows(world)["test/e"].get("is_dev")
    _assert_install_rewrites_nothing(world)


def test_uninstall_of_a_virtual_sibling_never_parents_it_to_itself(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4823: test/mono/d declares test/mono/e; both share the legacy identity test/mono."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)

    def mono(root: Path, _name: str) -> None:
        _skill_package(root / "d", "d", ("test/mono/e",))
        _skill_package(root / "e", "e")

    world.publish("mono", mono)
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["test/mono/d", "test/mono/e"]},
        },
        world.project / "apm.yml",
    )
    world.apm("install")
    world.apm("uninstall", "test/mono/e")
    lock = load_yaml(world.project / "apm.lock.yaml")
    e_row = next(row for row in lock["dependencies"] if row.get("virtual_path") == "e")
    assert e_row["depth"] == 2
    _assert_install_rewrites_nothing(world)


def test_uninstall_reparents_to_a_surviving_local_parent_by_its_lock_key(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4823: local ./vendor/d declares test/e; e lands one level below d's lock row."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    world.publish("e", _skill_package)
    _skill_package(world.project / "vendor" / "d", "d", ("test/e",))
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/d", "test/e"]},
        },
        world.project / "apm.yml",
    )
    world.apm("install")
    world.apm("uninstall", "test/e")
    assert _rows(world)["test/e"]["depth"] == 2
    _assert_install_rewrites_nothing(world)


def test_uninstall_reparents_a_descendant_to_its_nearest_surviving_parent(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4833: d -> e -> f and g -> f; once e is not direct, f sits under g at depth 2."""
    world = _graph_world(
        tmp_path,
        apm_binary_path,
        (("f", None), ("e", "f"), ("d", "e"), ("g", "f")),
        ["test/d", "test/e", "test/g"],
    )
    world.apm("uninstall", "test/e")
    rows = _rows(world)
    assert (rows["test/e"]["resolved_by"], rows["test/e"]["depth"]) == ("test/d", 2)
    assert (rows["test/f"]["resolved_by"], rows["test/f"]["depth"]) == ("test/g", 2)
    _assert_install_rewrites_nothing(world)


def test_uninstall_takes_the_shorter_dev_path_through_an_expanded_node(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4833: p -> q -> x -> e and b -> c -> e, with x also a direct dev dependency."""
    world = _graph_world(
        tmp_path,
        apm_binary_path,
        (("e", None), ("x", "e"), ("q", "x"), ("p", "q"), ("c", "e"), ("b", "c")),
        ["test/p", "test/b", "test/e"],
        dev=["test/x"],
    )
    world.apm("uninstall", "test/e")
    row = _rows(world)["test/e"]
    assert (row["resolved_by"], row["depth"], row.get("is_dev")) == ("test/x", 2, None)
    _assert_install_rewrites_nothing(world)


def test_uninstall_makes_a_shared_descendant_dev_only_graph_wide(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4833: dev d -> e, e -> f, dev a -> f; once e is not direct, f is dev-only."""
    world = _graph_world(
        tmp_path,
        apm_binary_path,
        (("f", None), ("e", "f"), ("d", "e"), ("a", "f")),
        ["test/e"],
        dev=["test/d", "test/a"],
    )
    world.apm("uninstall", "test/e")
    rows = _rows(world)
    assert rows["test/e"].get("is_dev") is True
    assert rows["test/f"].get("is_dev") is True
    _assert_install_rewrites_nothing(world)


def test_uninstall_refuses_a_local_dependency_another_package_still_needs(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """#4882: dev ./vendor/d declares ../e; uninstalling direct ./vendor/e refuses.

    A kept local dependency would change lock identity (its anchored key), so
    uninstall refuses before any write instead of leaving a lock that
    ``install --frozen`` rejects.
    """
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    vendor = world.project / "vendor"
    _skill_package(vendor / "f", "f")
    _skill_package(vendor / "e", "e", ("../f",))
    _skill_package(vendor / "d", "d", ("../e",))
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": ["./vendor/e"]},
            "devDependencies": {"apm": ["./vendor/d"]},
        },
        world.project / "apm.yml",
    )
    world.apm("install")
    before = tree_digest(world.project)

    refused = world.apm("uninstall", "./vendor/e", expect=1)

    assert refused.returncode == 1
    output = " ".join((refused.stdout + refused.stderr).split())
    assert (
        "./vendor/e stays: ./vendor/d still needs it; apm.yml and apm.lock.yaml were not changed."
    ) in output
    assert "Uninstall the package that still needs it first" in output
    assert tree_digest(world.project) == before
    world.apm("install", "--frozen")
