"""Resolver selection merge across diamond edges (#3735).

Every edge that declares one dependency node contributes its ``skills:`` and
``targets:`` selection. The node carries the sorted union, where an
unrestricted edge absorbs any narrowing, independent of manifest order.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apm_cli.deps.apm_resolver import APMDependencyResolver

pytestmark = pytest.mark.component

BUNDLE = "org/bundle"


def _write_pkg(modules: Path, name: str, deps: list | None = None) -> None:
    pkg_dir = modules / name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"name": name.split("/")[-1], "version": "1.0.0"}
    if deps:
        manifest["dependencies"] = {"apm": deps}
    (pkg_dir / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")


def _edge(skills: list[str] | None = None, targets: list[str] | None = None, **extra) -> dict:
    entry: dict = {"git": f"https://github.com/{BUNDLE}.git", **extra}
    if skills is not None:
        entry["skills"] = skills
    if targets is not None:
        entry["targets"] = targets
    return entry


def _resolve(tmp_path: Path, root_deps: list, packages: dict[str, list]) -> object:
    modules = tmp_path / "apm_modules"
    for name, deps in packages.items():
        _write_pkg(modules, name, deps)
    _write_pkg(modules, BUNDLE)
    (tmp_path / "apm.yml").write_text(
        yaml.safe_dump(
            {"name": "consumer", "version": "1.0.0", "dependencies": {"apm": root_deps}}
        ),
        encoding="utf-8",
    )

    def callback(dep_ref, mods_dir, parent_chain="", parent_pkg=None):
        return dep_ref.get_install_path(mods_dir)

    resolver = APMDependencyResolver(apm_modules_dir=modules, download_callback=callback)
    return resolver.resolve_dependencies(tmp_path)


def _bundle_ref(graph):
    matches = [
        dep
        for dep in graph.flattened_dependencies.get_installation_list()
        if dep.get_unique_key() == f"github.com/{BUNDLE}" or dep.repo_url == BUNDLE
    ]
    assert len(matches) == 1, [d.get_unique_key() for d in matches]
    return matches[0]


SIBLINGS = {
    "org/a": [_edge(["beta", "alpha"])],
    "org/b": [_edge(["gamma", "delta"])],
}


@pytest.mark.parametrize("order", [["org/a", "org/b"], ["org/b", "org/a"]])
def test_sibling_parents_union_independent_of_order(tmp_path: Path, order: list[str]) -> None:
    graph = _resolve(tmp_path, order, SIBLINGS)
    assert _bundle_ref(graph).skill_subset == ["alpha", "beta", "delta", "gamma"]


def test_depth_three_chain_unions_every_level(tmp_path: Path) -> None:
    graph = _resolve(
        tmp_path,
        ["org/b"],
        {"org/b": ["org/a", _edge(["gamma", "delta"])], "org/a": [_edge(["alpha", "beta"])]},
    )
    assert _bundle_ref(graph).skill_subset == ["alpha", "beta", "delta", "gamma"]


def test_unrestricted_edge_absorbs_narrowing(tmp_path: Path) -> None:
    graph = _resolve(
        tmp_path,
        ["org/a", "org/b"],
        {"org/a": [_edge(["alpha"])], "org/b": [_edge()]},
    )
    assert _bundle_ref(graph).skill_subset is None


def test_root_declaration_unions_with_transitive(tmp_path: Path) -> None:
    graph = _resolve(
        tmp_path,
        [_edge(["alpha"]), "org/b"],
        {"org/b": [_edge(["gamma"])]},
    )
    ref = _bundle_ref(graph)
    assert ref.skill_subset == ["alpha", "gamma"]
    assert [edge.declared_by() for edge in ref.selection_contributors] == ["apm.yml", "org/b"]


def test_target_subsets_union(tmp_path: Path) -> None:
    graph = _resolve(
        tmp_path,
        ["org/a", "org/b"],
        {"org/a": [_edge(targets=["claude"])], "org/b": [_edge(targets=["copilot"])]},
    )
    assert _bundle_ref(graph).target_subset == ["claude", "copilot"]


def test_dev_edge_still_contributes_before_prod_wins(tmp_path: Path) -> None:
    modules = tmp_path / "apm_modules"
    _write_pkg(modules, BUNDLE)
    (tmp_path / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "consumer",
                "version": "1.0.0",
                "dependencies": {"apm": [_edge(["alpha"])]},
                "devDependencies": {"apm": [_edge(["beta"])]},
            }
        ),
        encoding="utf-8",
    )
    resolver = APMDependencyResolver(
        apm_modules_dir=modules,
        download_callback=lambda dep_ref, mods_dir, parent_chain="", parent_pkg=None: (
            dep_ref.get_install_path(mods_dir)
        ),
    )
    graph = resolver.resolve_dependencies(tmp_path)
    ref = _bundle_ref(graph)
    assert ref.skill_subset == ["alpha", "beta"]
    node = next(n for n in graph.dependency_tree.nodes.values() if n.dependency_ref is ref)
    assert node.is_dev is False


def test_divergent_alias_keeps_shallowest_and_warns(tmp_path: Path) -> None:
    graph = _resolve(
        tmp_path,
        ["org/a", "org/b"],
        {"org/a": [_edge(alias="bun-a")], "org/b": [_edge(alias="bun-b")]},
    )
    assert _bundle_ref(graph).alias == "bun-a"
    warnings = graph.dependency_tree.resolution_warnings
    assert len(warnings) == 1
    assert "bun-a" in warnings[0] and "bun-b" in warnings[0]


def test_matching_alias_does_not_warn(tmp_path: Path) -> None:
    graph = _resolve(
        tmp_path,
        ["org/a", "org/b"],
        {"org/a": [_edge(alias="bun")], "org/b": [_edge(alias="bun")]},
    )
    assert graph.dependency_tree.resolution_warnings == []


def test_selection_merge_never_changes_identity_key(tmp_path: Path) -> None:
    graph = _resolve(tmp_path, ["org/a", "org/b"], SIBLINGS)
    keys = [dep.get_unique_key() for dep in graph.flattened_dependencies.get_installation_list()]
    assert len(keys) == len(set(keys)) == 3
