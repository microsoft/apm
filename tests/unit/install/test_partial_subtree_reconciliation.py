"""Partial-install subtree reconciliation edge cases (#3502 review findings)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.package_selection import previous_selected_subtree_keys
from apm_cli.install.phases import cleanup
from apm_cli.install.phases.lockfile import LockfileBuilder
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.component


def _lock(*entries: LockedDependency) -> LockFile:
    lock = LockFile()
    for entry in entries:
        lock.dependencies[entry.get_unique_key()] = entry
    return lock


def _chain_lock() -> LockFile:
    """Parent ``Acme/Parent`` -> ``acme/b`` -> ``acme/c``, recorded with declared casing."""
    return _lock(
        LockedDependency(repo_url="Acme/Parent"),
        LockedDependency(repo_url="acme/b", depth=2, resolved_by="Acme/Parent"),
        LockedDependency(repo_url="acme/c", depth=3, resolved_by="acme/b"),
    )


@pytest.mark.parametrize(
    "declarations",
    [[DependencyReference.parse("Acme/Parent")], []],
    ids=["declared-casing", "locked-casing"],
)
def test_canonical_selector_reaches_children_recorded_under_declared_casing(declarations):
    dropped, rescued = previous_selected_subtree_keys(
        ["acme/parent"], declarations, _chain_lock(), {"acme/parent"}
    )
    assert dropped == {"acme/b", "acme/c"}
    assert rescued == frozenset()


def test_retained_entry_missing_from_graph_drops_nothing():
    """An unselected entry the graph no longer reaches may still need a candidate."""
    lock = _chain_lock()
    lock.dependencies["acme/other"] = LockedDependency(repo_url="acme/other")
    dropped, rescued = previous_selected_subtree_keys(["acme/parent"], [], lock, {"acme/parent"})
    assert dropped == frozenset()
    assert rescued == frozenset()

    dropped, _ = previous_selected_subtree_keys(
        ["acme/parent"], [], lock, {"acme/parent", "acme/other"}
    )
    assert dropped == {"acme/b", "acme/c"}


def test_replacement_bound_candidate_fails_closed():
    lock = _chain_lock()
    child = lock.dependencies["acme/c"]
    child.replacement = SimpleNamespace(requests=[SimpleNamespace(parent="acme/b")])
    assert previous_selected_subtree_keys(["acme/parent"], [], lock, {"acme/parent", "acme/c"}) == (
        frozenset(),
        frozenset(),
    )


def test_retained_descendant_pins_its_dropped_ancestors():
    lock = _chain_lock()
    dropped = frozenset({"acme/b", "acme/c"})
    assert cleanup._pin_retained_ancestors(lock, dropped, frozenset({"acme/c"})) == dropped
    assert cleanup._pin_retained_ancestors(lock, dropped, frozenset()) == frozenset()


def _builder(existing: LockFile, **fields) -> LockfileBuilder:
    ctx = SimpleNamespace(
        orphan_cleanup_retained={},
        partial_pinned_dep_keys=frozenset(),
        partial_dropped_dep_keys=frozenset(),
        partial_rescued_dep_keys=frozenset(),
        dependency_graph=None,
        apm_package=None,
    )
    for name, value in fields.items():
        setattr(ctx, name, value)
    return LockfileBuilder(ctx)


def test_pinned_ancestor_stays_locked_for_retry():
    lock = _chain_lock()
    lock.dependencies["acme/b"].deployed_files = [".claude/skills/b/SKILL.md"]
    lock.dependencies["acme/c"].deployed_files = [".claude/skills/c/SKILL.md"]
    builder = _builder(
        lock,
        partial_dropped_dep_keys=frozenset({"acme/b", "acme/c"}),
        partial_pinned_dep_keys=frozenset({"acme/b", "acme/c"}),
        orphan_cleanup_retained={"acme/c": {".claude/skills/c/SKILL.md": None}},
    )
    builder._reconcile_partial_subtree(lock)
    assert lock.dependencies["acme/b"].deployed_files == []
    assert lock.dependencies["acme/c"].deployed_files == [".claude/skills/c/SKILL.md"]
    assert lock.dependencies["acme/c"].resolved_by == "acme/b"


def test_rescued_local_dependency_takes_new_declaring_coordinates():
    shared = LockedDependency(
        repo_url="_local/shared",
        source="local",
        local_path="../shared",
        depth=2,
        resolved_by="_local/p",
        declaring_parent="/pkgs/p",
        anchored_local_path="/pkgs/shared",
    )
    lock = _lock(shared)
    key = shared.get_unique_key()
    reference = DependencyReference(
        repo_url="_local/shared",
        is_local=True,
        local_path="../../shared",
        declaring_parent="/pkgs/nested/q",
        anchored_local_path="/pkgs/shared",
    )
    parent = SimpleNamespace(dependency_ref=DependencyReference.parse("acme/q"))
    node = SimpleNamespace(dependency_ref=reference, depth=3, parent=parent)
    graph = SimpleNamespace(dependency_tree=MagicMock())
    graph.dependency_tree.get_node.side_effect = lambda k: node if k == key else None
    builder = _builder(lock, partial_rescued_dep_keys=frozenset({key}), dependency_graph=graph)
    builder._reconcile_partial_subtree(lock)
    entry = lock.dependencies[key]
    assert (entry.resolved_by, entry.depth) == ("acme/q", 3)
    assert entry.local_path == "../../shared"
    assert entry.declaring_parent == "/pkgs/nested/q"


def _module_ctx(tmp_path: Path, lock: LockFile, *, graph_refs=(), declared=()) -> SimpleNamespace:
    graph = SimpleNamespace(
        dependency_tree=SimpleNamespace(
            nodes={i: SimpleNamespace(dependency_ref=ref) for i, ref in enumerate(graph_refs)}
        )
    )
    return SimpleNamespace(
        apm_modules_dir=tmp_path,
        existing_lockfile=lock,
        all_apm_deps=list(declared),
        deps_to_install=[],
        dependency_graph=graph,
        diagnostics=MagicMock(),
    )


def _materialize(root: Path, *relative: str) -> None:
    for path in relative:
        (root / path).mkdir(parents=True, exist_ok=True)
        (root / path / "apm.yml").write_text("name: x\n", encoding="utf-8")


def test_pinned_nested_package_survives_parent_module_removal(tmp_path):
    parent = LockedDependency(repo_url="acme/repo", depth=2, resolved_by="acme/top")
    child = LockedDependency(
        repo_url="acme/repo",
        virtual_path="skills/child",
        is_virtual=True,
        depth=3,
        resolved_by="acme/repo",
    )
    lock = _lock(parent, child)
    _materialize(tmp_path, "acme/repo", "acme/repo/skills/child")
    dropped = frozenset({parent.get_unique_key(), child.get_unique_key()})
    ctx = _module_ctx(tmp_path, lock)
    cleanup._remove_dropped_package_dirs(ctx, dropped, frozenset({child.get_unique_key()}))
    assert (tmp_path / "acme/repo/skills/child").is_dir()


def test_unresolved_marketplace_declaration_does_not_block_module_removal(tmp_path):
    dropped_dep = LockedDependency(repo_url="acme/dropped", depth=2, resolved_by="acme/top")
    lock = _lock(dropped_dep)
    _materialize(tmp_path, "acme/dropped")
    unresolved = MagicMock()
    unresolved.get_install_path.side_effect = ValueError("marketplace entry not resolved")
    ctx = _module_ctx(tmp_path, lock, declared=[unresolved])
    cleanup._remove_dropped_package_dirs(
        ctx, frozenset({dropped_dep.get_unique_key()}), frozenset()
    )
    assert not (tmp_path / "acme/dropped").exists()
