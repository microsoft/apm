"""Actual resolver source dispatch and logical ownership for root replacements."""

from pathlib import Path

import pytest

from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.replacement import ReplacementCollisionError
from apm_cli.models.manifest_contract import SOURCE_REPLACEMENTS_V1_SCHEMA_URI
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.component

_UPSTREAM = "https://github.com/example/base"


def _root_rules(consumer: Path, rules: list[dict]) -> None:
    manifest = load_yaml(consumer / "apm.yml")
    manifest["$schema"] = SOURCE_REPLACEMENTS_V1_SCHEMA_URI
    manifest["replace"] = rules
    dump_yaml(manifest, consumer / "apm.yml")


def test_actual_resolver_dispatches_local_source_into_original_slot(tmp_path: Path) -> None:
    factory = LocalPackageFactory(tmp_path)
    local = factory.create("local-base")
    consumer = factory.create("consumer", dependencies=({"git": _UPSTREAM, "ref": "a" * 40},))
    _root_rules(consumer.root, [{"for": {"git": _UPSTREAM}, "with": {"path": "../local-base"}}])
    observed = []

    def download(dep: DependencyReference, modules: Path, chain: str, parent_pkg=None) -> Path:
        observed.append(dep)
        assert dep.is_local
        assert dep.get_unique_key() == "example/base"
        assert dep.get_install_path(modules) == modules / "example/base"
        return local.root

    graph = APMDependencyResolver(download_callback=download).resolve_dependencies(consumer.root)
    assert graph.is_valid(), graph.resolution_errors
    assert len(observed) == 1
    assert list(graph.flattened_dependencies.dependencies) == ["example/base"]
    node = graph.dependency_tree.get_node("example/base")
    assert node is not None
    assert node.package.name == "local-base"
    assert node.package.source_path == local.root.resolve()


@pytest.mark.parametrize("missing", [False, True])
def test_read_only_replacement_uses_consumer_anchor_not_nested_parent(
    tmp_path: Path, missing: bool
) -> None:
    factory = LocalPackageFactory(tmp_path)
    local = factory.create("local-base")
    nested = LocalPackageFactory(tmp_path / "nested")
    nested.create("parent", dependencies=({"git": _UPSTREAM},))
    decoy = nested.create("local-base")
    consumer = factory.create("consumer", dependencies=("../nested/parent",))
    _root_rules(
        consumer.root,
        [
            {
                "for": {"git": _UPSTREAM},
                "with": {"path": "../missing" if missing else "../local-base"},
            }
        ],
    )
    graph = APMDependencyResolver(read_only=True).resolve_dependencies(consumer.root)
    assert graph.is_valid() is not missing, graph.resolution_errors
    node = graph.dependency_tree.get_node("example/base")
    assert node is not None
    if missing:
        assert node.package.version == "unknown"
        assert node.package.source_path is None
        assert any("Cannot load replacement" in error for error in graph.resolution_errors)
    else:
        assert node.package is not None
        assert node.package.source_path == local.root.resolve()
        assert node.package.source_path != decoy.root.resolve()
    assert not (consumer.root / "apm_modules").exists()
    assert not (consumer.root / "apm.lock.yaml").exists()


@pytest.mark.parametrize("updating", [False, True])
def test_actual_resolver_replaces_every_diamond_request_before_immutable_admission(
    tmp_path: Path,
    updating: bool,
) -> None:
    factory = LocalPackageFactory(tmp_path)
    local = factory.create("local-base")
    left = factory.create("left", dependencies=({"git": _UPSTREAM, "ref": "a" * 40},))
    right = factory.create("right", dependencies=({"git": _UPSTREAM, "ref": "b" * 40},))
    consumer = factory.create(
        "consumer",
        dependencies=(
            {"git": "https://github.com/example/left"},
            {"git": "https://github.com/example/right"},
        ),
    )
    _root_rules(consumer.root, [{"for": {"git": _UPSTREAM}, "with": {"path": "../local-base"}}])
    seen = []

    def download(dep: DependencyReference, modules: Path, chain: str, parent_pkg=None) -> Path:
        seen.append((dep.get_unique_key(), dep.is_local))
        return {"example/left": left.root, "example/right": right.root, "example/base": local.root}[
            dep.get_unique_key()
        ]

    graph = APMDependencyResolver(
        download_callback=download,
        update_refs=updating,
        update_keys={"example/base"} if updating else None,
    ).resolve_dependencies(consumer.root)
    assert graph.is_valid(), graph.resolution_errors
    assert sorted(seen) == [
        ("example/base", True),
        ("example/left", False),
        ("example/right", False),
    ]
    node = graph.dependency_tree.get_node("example/base")
    assert node is not None
    assert node.package.name == "local-base"
    assert node.package.source_path == local.root.resolve()
    evidence = node.dependency_ref.replacement.to_dict()
    assert {(r["parent"], r["ref"]) for r in evidence["requests"]} == {
        ("example/left", "a" * 40),
        ("example/right", "b" * 40),
    }
    for parent in ("example/left", "example/right"):
        assert graph.dependency_tree.get_node(parent).children == [node]


def test_actual_resolver_inherits_fork_parent_coordinates_without_chaining(tmp_path: Path) -> None:
    factory = LocalPackageFactory(tmp_path)
    fork = factory.create("fork", dependencies=({"git": "parent", "path": "packages/child"},))
    child = factory.create("child")
    consumer = factory.create("consumer", dependencies=({"git": _UPSTREAM, "ref": "old"},))
    _root_rules(
        consumer.root,
        [
            {
                "for": {"git": _UPSTREAM},
                "with": {"git": "https://github.com/fork/base", "ref": "fixed"},
            },
            {"for": {"git": "https://github.com/fork/base"}, "with": {"path": "../must-not-chain"}},
        ],
    )
    seen = []

    def download(dep: DependencyReference, modules: Path, chain: str, parent_pkg=None) -> Path:
        seen.append((dep.get_unique_key(), dep.repo_url, dep.reference, dep.virtual_path))
        return child.root if dep.virtual_path else fork.root

    resolver = APMDependencyResolver(download_callback=download)
    graph = resolver.resolve_dependencies(consumer.root)
    assert graph.is_valid(), graph.resolution_errors
    assert seen == [
        ("example/base", "fork/base", "fixed", None),
        ("fork/base/packages/child", "fork/base", "fixed", "packages/child"),
    ]
    assert len(resolver.replacement_index.unused_rules) == 1


@pytest.mark.parametrize(
    "other",
    ["https://gitlab.com/example/base", "https://github.com:8443/example/base"],
)
def test_actual_resolver_rejects_ambiguous_original_materialization_slots(
    tmp_path: Path, other: str
) -> None:
    factory = LocalPackageFactory(tmp_path)
    consumer = factory.create("consumer", dependencies=({"git": _UPSTREAM}, {"git": other}))
    _root_rules(consumer.root, [{"for": {"git": _UPSTREAM}, "with": {"path": "../local"}}])
    with pytest.raises(ReplacementCollisionError, match="share the existing materialization slot"):
        APMDependencyResolver().resolve_dependencies(consumer.root)


def test_actual_resolver_detects_logical_cycle_through_local_replacement(tmp_path: Path) -> None:
    factory = LocalPackageFactory(tmp_path)
    local = factory.create("local-base", dependencies=({"git": _UPSTREAM, "ref": "another"},))
    consumer = factory.create("consumer", dependencies=({"git": _UPSTREAM},))
    _root_rules(consumer.root, [{"for": {"git": _UPSTREAM}, "with": {"path": "../local-base"}}])

    def download(dep: DependencyReference, modules: Path, chain: str, parent_pkg=None) -> Path:
        return local.root

    graph = APMDependencyResolver(download_callback=download).resolve_dependencies(consumer.root)
    assert not graph.is_valid()
    assert len(graph.circular_dependencies) == 1
    assert len(graph.flattened_dependencies.dependencies) == 1


def test_actual_resolver_ignores_dependency_owned_replacement_rules(tmp_path: Path) -> None:
    factory = LocalPackageFactory(tmp_path)
    local = factory.create(
        "local-base", dependencies=({"git": "https://github.com/example/child"},)
    )
    child = factory.create("child")
    _root_rules(
        local.root,
        [
            {
                "for": {"git": "https://github.com/example/child"},
                "with": {"path": "../must-not-apply"},
            }
        ],
    )
    consumer = factory.create("consumer", dependencies=({"git": _UPSTREAM},))
    _root_rules(consumer.root, [{"for": {"git": _UPSTREAM}, "with": {"path": "../local-base"}}])
    seen = []

    def download(dep: DependencyReference, modules: Path, chain: str, parent_pkg=None) -> Path:
        seen.append((dep.get_unique_key(), dep.is_local))
        return local.root if dep.is_local else child.root

    graph = APMDependencyResolver(download_callback=download).resolve_dependencies(consumer.root)
    assert graph.is_valid(), graph.resolution_errors
    assert seen == [("example/base", True), ("example/child", False)]


def test_actual_resolver_frozen_fork_does_not_reuse_same_ref_upstream_pin(tmp_path: Path) -> None:
    factory = LocalPackageFactory(tmp_path)
    consumer = factory.create("consumer", dependencies=({"git": _UPSTREAM, "ref": "fixed"},))
    _root_rules(
        consumer.root,
        [
            {
                "for": {"git": _UPSTREAM},
                "with": {"git": "https://github.com/fork/base", "ref": "fixed"},
            }
        ],
    )
    locked = LockFile()
    locked.add_dependency(
        LockedDependency(
            repo_url="example/base",
            host="github.com",
            resolved_ref="fixed",
            resolved_commit="a" * 40,
        )
    )
    graph = APMDependencyResolver(existing_lockfile=locked, frozen=True).resolve_dependencies(
        consumer.root
    )
    assert not graph.is_valid()
    assert "selected replacement source" in "\n".join(graph.resolution_errors)
