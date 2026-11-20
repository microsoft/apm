"""Preview graph contracts: ``--dry-run`` projects the install pipeline's own plan.

The preview runs ``run_install_pipeline(dry_run=True)`` (#4085), which resolves
into rollback staging and changes nothing on disk.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.install.dry_run_plan import ProspectiveInstallPlan
from apm_cli.install.package_selection import selected_subtree_dependencies
from apm_cli.install.phases.resolve import _apply_only_filter
from apm_cli.install.pipeline_entry import pipeline_preview
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
    package: APMPackage | None = None,
) -> ProspectiveInstallPlan:
    """Project the install pipeline's dry-run plan through the preview owner."""
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.install.pipeline import run_install_pipeline
    from apm_cli.install.transaction import InstallTransaction

    package = package or APMPackage.from_apm_yml(root / "apm.yml")
    logger = InstallLogger(verbose=False, dry_run=True)
    transaction = InstallTransaction(
        manifest_path=root / "apm.yml",
        apm_modules_dir=root / "apm_modules",
        validation=None,
        logger=logger,
        acquire_lock=False,
    )
    previous = Path.cwd()
    os.chdir(root)
    try:

        def run():
            return run_install_pipeline(
                package,
                update_refs=update,
                only_packages=only_packages,
                logger=logger,
                no_policy=True,
                install_mcp=False,
                transaction=transaction,
                dry_run=True,
            )

        preview = pipeline_preview(run, logger)
        transaction.rollback()
    finally:
        os.chdir(previous)
    return ProspectiveInstallPlan.from_apm_package(
        package,
        should_install_apm=True,
        should_install_mcp=False,
        only_packages=only_packages,
    ).with_pipeline_preview(preview, lockfile_dir=root)


@pytest.mark.parametrize("only_packages", [None, ["../parent"]])
def test_local_descendants_share_resolver_identity_without_materialization(
    tmp_path: Path, only_packages: list[str] | None
) -> None:
    factory = LocalPackageFactory(tmp_path)
    factory.create("child")
    factory.create("parent", dependencies=("../child",))
    consumer = factory.create("consumer", dependencies=("../parent",))
    before = ArtifactSnapshot.capture(tmp_path)
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
    ["../missing", {"name": "plugin", "marketplace": "unregistered"}],
)
def test_unresolvable_subtrees_do_not_claim_completeness(
    tmp_path: Path, dependency: str | dict[str, str]
) -> None:
    consumer = LocalPackageFactory(tmp_path).create("consumer", dependencies=(dependency,))
    before = ArtifactSnapshot.capture(tmp_path)
    plan = _preview(consumer.root)
    assert plan.dependency_preview_errors
    assert plan.apm_dependency_count == 1
    assert ArtifactSnapshot.capture(tmp_path) == before


def test_unreadable_lockfile_is_explicitly_incomplete(tmp_path: Path) -> None:
    consumer = LocalPackageFactory(tmp_path).create("consumer")
    (consumer.root / "apm.lock.yaml").write_text("[broken", encoding="utf-8")
    plan = _preview(consumer.root)
    assert plan.dependency_preview_errors


def test_dev_root_and_diamond_dependencies_are_deduplicated(tmp_path: Path) -> None:
    factory = LocalPackageFactory(tmp_path)
    factory.create("child")
    factory.create("left", dependencies=("../child",))
    factory.create("right", dependencies=("../child",))
    consumer = factory.create("consumer", dependencies=("../left", "../right"))
    package = APMPackage.from_apm_yml(consumer.manifest_path)
    package.dev_dependencies = {"apm": [DependencyReference.parse("../right")]}
    plan = _preview(consumer.root, package=package)
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


def test_two_hosts_one_slot_is_a_declaration_error(tmp_path: Path) -> None:
    """The #3895 shared-directory refusal reaches the preview as a declaration error."""
    consumer = LocalPackageFactory(tmp_path).create(
        "consumer", dependencies=("acme/tool", "gitlab.com/acme/tool")
    )
    plan = _preview(consumer.root)
    assert len(plan.dependency_declaration_errors) == 1
    assert "would both install to" in plan.dependency_declaration_errors[0]
    assert set(plan.dependency_declaration_errors) <= set(plan.dependency_preview_errors)
