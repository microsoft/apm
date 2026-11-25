"""A local diamond records one declaring parent, whatever the traversal order (#5386).

The resolver records a node two parents declare at one depth under the
nearest declaring parent with the lowest key (``resolved_by``, #3735). The
node's ``declaring_parent`` and ``local_path`` come from that same parent's
edge, so the lockfile does not depend on which parent a manifest lists first.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apm_cli.deps.apm_resolver import APMDependencyResolver

pytestmark = [
    pytest.mark.unit,
    pytest.mark.promise("P-INV-I2"),
]


def _write(directory: Path, name: str, deps: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"name": name, "version": "1.0.0"}
    if deps:
        manifest["dependencies"] = {"apm": deps}
    (directory / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")


def _resolve(project: Path, order: list[str]):
    vendor = project / "vendor"
    _write(project, "consumer", ["./vendor/a"])
    _write(vendor / "a", "a", [f"../{name}" for name in order])
    _write(vendor / "b", "b", ["../d"])
    _write(vendor / "c", "c", ["../../vendor/d"])
    _write(vendor / "d", "d", [])

    def callback(dep_ref, modules, parent_chain="", parent_pkg=None):
        base = parent_pkg.source_path if parent_pkg is not None else project
        return (base / dep_ref.local_path).resolve()

    resolver = APMDependencyResolver(
        apm_modules_dir=project / "apm_modules", download_callback=callback
    )
    return resolver.resolve_dependencies(project)


@pytest.mark.parametrize("order", [["b", "c"], ["c", "b"]], ids=["b-first", "c-first"])
def test_a_diamond_node_takes_its_recorded_parents_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, order: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # local paths anchor at the project, as install runs
    graph = _resolve(tmp_path, order)
    [node] = [
        node
        for node in graph.dependency_tree.nodes.values()
        if node.dependency_ref.local_path and Path(node.dependency_ref.local_path).name == "d"
    ]
    ref = node.dependency_ref

    assert node.parent is not None
    assert node.parent.dependency_ref.local_path == "../b"
    assert (ref.declaring_parent, ref.local_path, ref.anchored_local_path) == (
        node.parent.get_id(),
        "../d",
        "vendor/d",
    )


@pytest.mark.parametrize(
    ("first_path", "ranked_path", "one_entry", "takes_ranked"),
    [
        ("../../shared/F-Pkg", "../shared/f-pkg", True, True),
        ("../../shared/f-pkg", "../shared/F-PKG", True, True),
        ("../../shared/caf\u00e9", "../shared/cafe\u0301", True, True),
        ("../../shared/cafe\u0301", "../shared/CAF\u00c9", True, True),
        ("../../shared/F-Pkg", "../shared/f-pkg", False, False),
        ("../../shared/F-Pkg", "../shared/f-pkg", None, False),
        ("../../shared/d", "../shared/d", None, True),
        ("../../shared/d", "../shared/d-alias", True, False),
        ("../../shared/cafe", "../shared/caf\u00e9", True, False),
    ],
    ids=[
        "case",
        "case-upper",
        "nfc-nfd",
        "nfd-nfc-case",
        "case-symlink-alias",
        "case-no-volume-check",
        "same",
        "alias",
        "accent-differs",
    ],
)
def test_a_respelled_leaf_takes_the_ranked_edge(
    first_path, ranked_path, one_entry, takes_ranked
) -> None:
    """#5457: a leaf respelled only by case or Unicode normalization takes the ranked
    parent's edge when the volume stores both as one entry; a symlink alias (two
    entries, #5386), a caller without a volume check, or another leaf keeps the first."""
    from apm_cli.deps.local_identity import recorded_declaring_edge

    first, ranked = {"path": first_path}, {"path": ranked_path}
    same_entry = None if one_entry is None else (lambda _first, _ranked: one_entry)
    chosen = recorded_declaring_edge(first, ranked, lambda edge: edge["path"], same_entry)
    assert chosen is (ranked if takes_ranked else first)


def test_a_case_only_symlink_alias_is_its_own_entry(tmp_path: Path) -> None:
    """#5386 stays: on a case-sensitive volume ``f -> F`` is a second entry, so the
    two spellings are not one stored leaf; the exact spelling is one entry."""
    from apm_cli.deps.local_identity import same_stored_leaf

    (tmp_path / "F").mkdir()
    try:
        (tmp_path / "f").symlink_to(tmp_path / "F", target_is_directory=True)
    except (OSError, FileExistsError):
        pytest.skip("this volume folds case or cannot link")
    assert not same_stored_leaf(tmp_path / "F", tmp_path / "f")
    assert same_stored_leaf(tmp_path / "F", tmp_path / "F")
