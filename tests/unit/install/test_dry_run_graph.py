"""Preview graph contracts reuse the resolver without side effects or remote work."""

from __future__ import annotations

from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.dry_run_plan import ProspectiveInstallPlan
from apm_cli.install.package_selection import selected_subtree_dependencies
from apm_cli.install.phases.resolve import _apply_only_filter
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency import DependencyReference
from tests.utils.artifact_snapshot import ArtifactSnapshot
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.component


def _preview(
    root: Path,
    *,
    update: bool = False,
    only_packages: list[str] | None = None,
) -> ProspectiveInstallPlan:
    """Interpret the real root manifest through the canonical preview owner."""
    package = APMPackage.from_apm_yml(root / "apm.yml")
    return ProspectiveInstallPlan.from_apm_package(
        package,
        should_install_apm=True,
        should_install_mcp=False,
        only_packages=only_packages,
    ).with_resolved_dependencies(
        package,
        project_root=root,
        apm_modules_dir=root / "apm_modules",
        lockfile_dir=root,
        update=update,
    )


@pytest.mark.parametrize("only_packages", [None, ["../parent"]])
def test_local_descendants_share_resolver_identity_without_materialization(
    tmp_path: Path, only_packages: list[str] | None
) -> None:
    factory = LocalPackageFactory(tmp_path)
    factory.create("child")
    factory.create("parent", dependencies=("../child",))
    consumer = factory.create("consumer", dependencies=("../parent",))
    before = ArtifactSnapshot.capture(tmp_path)
    with patch("apm_cli.deps.apm_resolver.materialize_marketplace_manifest") as materialize:
        plan = _preview(consumer.root, only_packages=only_packages)
    assert not plan.dependency_preview_errors
    assert {dep.local_path for dep in plan.selected_apm_dependencies} == {"../parent", "../child"}
    assert plan.apm_dependency_count == 2
    graph = APMDependencyResolver(
        apm_modules_dir=consumer.root / "apm_modules", read_only=True
    ).resolve_dependencies(consumer.root)
    assert plan.intended_dependency_keys == frozenset(
        dep.get_unique_key() for dep in graph.flattened_dependencies.get_installation_list()
    )
    materialize.assert_not_called()
    assert ArtifactSnapshot.capture(tmp_path) == before


def test_selected_subtree_expands_each_graph_node_once() -> None:
    """Count actual graph expansion rather than comparing noisy elapsed times."""
    expansions: list[int] = []

    class ObservedChildren(list):
        def __iter__(self) -> Iterator:
            expansions.append(id(self))
            return super().__iter__()

    refs = [DependencyReference.parse(f"owner/package-{index}") for index in range(25)]
    nodes = [SimpleNamespace(dependency_ref=ref, children=ObservedChildren()) for ref in refs]
    for parent, child in pairwise(nodes):
        parent.children.append(child)
    tree = SimpleNamespace(nodes=dict(enumerate(nodes)))
    result = selected_subtree_dependencies(["owner/package-0"], tree, refs)
    assert result == refs
    assert len(expansions) == len(nodes)


def test_selected_subtree_keeps_distinct_nodes_with_the_same_identity() -> None:
    """Shared traversal state must not discard children of another ref claimant."""
    first = DependencyReference.parse("owner/shared#v1")
    second = DependencyReference.parse("owner/shared#v2")
    leaf = DependencyReference.parse("owner/leaf")
    root = DependencyReference.parse("owner/root")
    leaf_node = SimpleNamespace(dependency_ref=leaf, children=[])
    first_node = SimpleNamespace(dependency_ref=first, children=[])
    second_node = SimpleNamespace(dependency_ref=second, children=[leaf_node])
    root_node = SimpleNamespace(dependency_ref=root, children=[first_node, second_node])
    tree = SimpleNamespace(nodes=dict(enumerate([root_node, first_node, second_node, leaf_node])))
    assert selected_subtree_dependencies(["owner/root"], tree, [root, first, leaf]) == [
        root,
        first,
        leaf,
    ]


@pytest.mark.parametrize(
    "dependency",
    ["../missing", "owner/missing", {"name": "plugin", "marketplace": "unregistered"}],
)
def test_unknown_subtrees_do_not_fetch_or_claim_completeness(
    tmp_path: Path, dependency: str | dict[str, str]
) -> None:
    consumer = LocalPackageFactory(tmp_path).create("consumer", dependencies=(dependency,))
    before = ArtifactSnapshot.capture(tmp_path)
    with patch("apm_cli.marketplace.resolver.resolve_marketplace_plugin") as lookup:
        plan = _preview(consumer.root)
    assert plan.dependency_preview_errors
    assert plan.apm_dependency_count == 1
    lookup.assert_not_called()
    assert ArtifactSnapshot.capture(tmp_path) == before


@pytest.mark.parametrize("update,reference", [(False, "v1"), (True, "v1"), (False, "v2")])
def test_installed_remote_metadata_requires_unchanged_reference(
    tmp_path: Path, update: bool, reference: str
) -> None:
    consumer = LocalPackageFactory(tmp_path).create(
        "consumer", dependencies=(f"owner/parent#{reference}",)
    )
    modules = consumer.root / "apm_modules"
    factory = LocalPackageFactory(modules / "owner")
    factory.create("parent", dependencies=("owner/child",))
    factory.create("child")
    lock = LockFile()
    for name in ("parent", "child"):
        lock.add_dependency(
            LockedDependency(
                repo_url=f"owner/{name}",
                resolved_ref="v1" if name == "parent" else None,
            )
        )
    lock.write(consumer.root / "apm.lock.yaml")
    before = ArtifactSnapshot.capture(tmp_path)
    plan = _preview(consumer.root, update=update)
    assert bool(plan.dependency_preview_errors) == (update or reference != "v1")
    if not plan.dependency_preview_errors:
        assert plan.intended_dependency_keys == {"owner/parent", "owner/child"}
    assert ArtifactSnapshot.capture(tmp_path) == before


def test_unreadable_lockfile_is_explicitly_incomplete(tmp_path: Path) -> None:
    consumer = LocalPackageFactory(tmp_path).create("consumer")
    (consumer.root / "apm.lock.yaml").write_text("[broken", encoding="utf-8")
    plan = _preview(consumer.root)
    assert len(plan.dependency_preview_errors) == 1
    assert "Cannot read lockfile" in plan.dependency_preview_errors[0]


def test_dev_root_and_diamond_dependencies_are_deduplicated(tmp_path: Path) -> None:
    factory = LocalPackageFactory(tmp_path)
    factory.create("child")
    factory.create("left", dependencies=("../child",))
    factory.create("right", dependencies=("../child",))
    consumer = factory.create("consumer", dependencies=("../left", "../right"))
    package = APMPackage.from_apm_yml(consumer.manifest_path)
    package.dev_dependencies = {"apm": [DependencyReference.parse("../right")]}
    plan = ProspectiveInstallPlan.from_apm_package(
        package, should_install_apm=True, should_install_mcp=False, only_packages=None
    ).with_resolved_dependencies(
        package,
        project_root=consumer.root,
        apm_modules_dir=consumer.root / "apm_modules",
        lockfile_dir=consumer.root,
        update=False,
    )
    assert not plan.dependency_preview_errors
    assert plan.apm_dependency_count == 3
    assert {dep.local_path for dep in plan.selected_apm_dependencies} == {
        "../left",
        "../right",
        "../child",
    }


def test_read_only_resolver_rejects_write_capabilities() -> None:
    with pytest.raises(ValueError, match="Read-only resolution cannot"):
        APMDependencyResolver(read_only=True, download_callback=Mock())


@pytest.mark.parametrize("missing_unselected", [False, True])
def test_positional_preview_resolves_full_graph_before_selection(
    tmp_path: Path, missing_unselected: bool
) -> None:
    factory = LocalPackageFactory(tmp_path)
    factory.create("parent", dependencies=("../child",))
    factory.create("child")
    if not missing_unselected:
        factory.create("unselected")
    consumer = factory.create("consumer", dependencies=("../parent", "../unselected"))
    modules = consumer.root / "apm_modules"
    before = ArtifactSnapshot.capture(tmp_path)
    graph = APMDependencyResolver(apm_modules_dir=modules, read_only=True).resolve_dependencies(
        consumer.root
    )
    assert bool(graph.resolution_errors) == missing_unselected
    install = SimpleNamespace(
        dependency_graph=graph,
        deps_to_install=graph.flattened_dependencies.get_installation_list(),
        only_packages=["../parent"],
    )
    _apply_only_filter(install)
    plan = _preview(consumer.root, only_packages=install.only_packages)
    assert bool(plan.dependency_preview_errors) == missing_unselected
    assert [dep.to_canonical() for dep in plan.selected_apm_dependencies] == [
        dep.to_canonical() for dep in install.deps_to_install
    ]
    assert ArtifactSnapshot.capture(tmp_path) == before
