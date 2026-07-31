from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import tomllib

RATCHET_TEST_SCOPE = "repository"

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
DOCS = REPO_ROOT / "docs" / "src" / "content" / "docs" / "contributing" / ("integration-testing.md")
APM_INSTRUCTIONS = REPO_ROOT / ".apm" / "instructions" / "tests.instructions.md"
GITHUB_INSTRUCTIONS = REPO_ROOT / ".github" / "instructions" / "tests.instructions.md"
EXPECTED_MARKERS = {
    "unit": "pure logic with no filesystem and no CLI",
    "component": "in-process behavior that touches a filesystem or one command boundary",
    "e2e": "a real installed CLI crossing at least one command boundary",
}
BEHAVIORAL_MARKERS = frozenset(EXPECTED_MARKERS)
INVENTORY_PLUGIN = "tests.quality.taxonomy_inventory_plugin"
AUTHORITY_PROSE = "Module-level `pytestmark` is the sole behavioral classification authority."


@dataclass(frozen=True)
class TaxonomyInventory:
    """Collected module declarations and effective node marker sets."""

    modules: dict[str, tuple[str, ...]]
    nodes: dict[str, tuple[str, ...]]


def _declared_markers() -> dict[str, str]:
    """Return pytest marker definitions from their canonical registry."""
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    entries = data["tool"]["pytest"]["ini_options"]["markers"]
    declared: dict[str, str] = {}
    for entry in entries:
        name, separator, description = entry.partition(":")
        if separator:
            declared[name.strip()] = description.strip()
    return declared


def _documented_marker_definitions(path: Path) -> dict[str, str]:
    """Read behavioral marker definitions from one documentation table."""
    definitions: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 4:
            continue
        marker = cells[1].strip("`")
        if marker in EXPECTED_MARKERS:
            definition = cells[2].rstrip(".")
            definitions[marker] = definition[:1].lower() + definition[1:]
    return definitions


def _string_tuple_map(value: object, field: str) -> dict[str, tuple[str, ...]]:
    """Validate one JSON mapping of strings to string arrays."""
    assert isinstance(value, dict), f"taxonomy inventory {field} must be an object"
    normalized: dict[str, tuple[str, ...]] = {}
    for key, entries in value.items():
        assert isinstance(key, str), f"taxonomy inventory {field} keys must be strings"
        assert isinstance(entries, list), f"taxonomy inventory {field}[{key!r}] must be an array"
        assert all(isinstance(entry, str) for entry in entries)
        normalized[key] = tuple(entries)
    return normalized


def _collect_inventory(root: Path, output: Path) -> TaxonomyInventory:
    """Collect the repository through the dedicated pytest inventory plugin."""
    env = os.environ.copy()
    env["APM_TAXONOMY_INVENTORY"] = str(output)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            INVENTORY_PLUGIN,
            "-o",
            "addopts=",
            "--strict-markers",
            "--collect-only",
            "-q",
            "tests",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert set(data) == {"modules", "nodes"}
    return TaxonomyInventory(
        modules=_string_tuple_map(data["modules"], "modules"),
        nodes=_string_tuple_map(data["nodes"], "nodes"),
    )


def _behavioral(markers: tuple[str, ...]) -> tuple[str, ...]:
    """Return behavioral markers from one collected marker sequence."""
    return tuple(marker for marker in markers if marker in BEHAVIORAL_MARKERS)


def _assert_marker_only_taxonomy(inventory: TaxonomyInventory) -> None:
    """Enforce distributed module classification without a central whitelist."""
    classified_modules: dict[str, str] = {}
    for module, markers in inventory.modules.items():
        behavioral = _behavioral(markers)
        if not behavioral:
            continue
        assert len(behavioral) == 1, (
            f"classified module {module!r} must declare exactly one behavioral marker; "
            f"got {sorted(behavioral)}"
        )
        classified_modules[module] = behavioral[0]

    assert classified_modules, "classified taxonomy must be non-empty"
    nodes_by_module: dict[str, dict[str, tuple[str, ...]]] = {}
    for nodeid, markers in inventory.nodes.items():
        module = nodeid.split("::", 1)[0]
        nodes_by_module.setdefault(module, {})[nodeid] = markers

    for module, expected in classified_modules.items():
        module_nodes = nodes_by_module.get(module, {})
        assert module_nodes, f"classified module {module!r} must collect at least one node"
        for nodeid, markers in module_nodes.items():
            behavioral = _behavioral(markers)
            assert len(behavioral) == 1, (
                f"classified node {nodeid!r} must have exactly one behavioral marker; "
                f"got {sorted(behavioral)}"
            )
            assert behavioral == (expected,), (
                f"classified module {module!r} must be uniform; "
                f"{nodeid!r} has {sorted(behavioral)}, expected {expected!r}"
            )

    for module, module_nodes in nodes_by_module.items():
        if module in classified_modules:
            continue
        node_classifications = {
            nodeid: sorted(_behavioral(markers))
            for nodeid, markers in module_nodes.items()
            if _behavioral(markers)
        }
        assert node_classifications == {}, (
            "node-level behavioral markers require one module-level pytestmark: "
            f"{node_classifications}"
        )


@pytest.fixture(scope="module")
def marker_inventory(
    tmp_path_factory: pytest.TempPathFactory,
) -> TaxonomyInventory:
    output = tmp_path_factory.mktemp("taxonomy") / "inventory.json"
    return _collect_inventory(REPO_ROOT, output)


def _classified_module_with_nodes(
    inventory: TaxonomyInventory,
    minimum_nodes: int,
) -> tuple[str, str, list[str]]:
    """Return a classified module with enough collected nodes for mutation tests."""
    for module, markers in sorted(inventory.modules.items()):
        behavioral = _behavioral(markers)
        nodes = sorted(nodeid for nodeid in inventory.nodes if nodeid.split("::", 1)[0] == module)
        if len(behavioral) == 1 and len(nodes) >= minimum_nodes:
            return module, next(iter(behavioral)), nodes
    raise AssertionError(f"no classified module has at least {minimum_nodes} nodes")


def test_tm001_behavioral_marker_definitions_match_spec() -> None:
    declared = _declared_markers()
    actual = {marker: declared.get(marker) for marker in EXPECTED_MARKERS}
    assert actual == EXPECTED_MARKERS


def test_tm002_marker_only_taxonomy_is_non_empty_and_uniform(
    marker_inventory: TaxonomyInventory,
) -> None:
    _assert_marker_only_taxonomy(marker_inventory)


def test_tm002_inventory_includes_configured_deselection_families(
    marker_inventory: TaxonomyInventory,
) -> None:
    assert any(
        nodeid.startswith("tests/benchmarks/test_audit_benchmarks.py::")
        for nodeid in marker_inventory.nodes
    )


def test_tm003_multiple_node_classifications_fail(
    marker_inventory: TaxonomyInventory,
) -> None:
    _module, marker, nodes = _classified_module_with_nodes(marker_inventory, 1)
    second = next(candidate for candidate in sorted(BEHAVIORAL_MARKERS) if candidate != marker)
    mutated_nodes = dict(marker_inventory.nodes)
    mutated_nodes[nodes[0]] = tuple(sorted((marker, second)))

    with pytest.raises(AssertionError, match="exactly one behavioral marker"):
        _assert_marker_only_taxonomy(
            TaxonomyInventory(
                modules=dict(marker_inventory.modules),
                nodes=mutated_nodes,
            )
        )


def test_tm003_duplicate_same_marker_classifications_fail(
    marker_inventory: TaxonomyInventory,
) -> None:
    _module, marker, nodes = _classified_module_with_nodes(marker_inventory, 1)
    mutated_nodes = dict(marker_inventory.nodes)
    mutated_nodes[nodes[0]] = (marker, marker)

    with pytest.raises(AssertionError, match="exactly one behavioral marker"):
        _assert_marker_only_taxonomy(
            TaxonomyInventory(
                modules=dict(marker_inventory.modules),
                nodes=mutated_nodes,
            )
        )


def test_tm003_mixed_module_classifications_fail(
    marker_inventory: TaxonomyInventory,
) -> None:
    module, marker, nodes = _classified_module_with_nodes(marker_inventory, 2)
    second = next(candidate for candidate in sorted(BEHAVIORAL_MARKERS) if candidate != marker)
    mutated_nodes = dict(marker_inventory.nodes)
    mutated_nodes[nodes[1]] = (second,)

    with pytest.raises(AssertionError, match="must be uniform"):
        _assert_marker_only_taxonomy(
            TaxonomyInventory(
                modules={**marker_inventory.modules, module: (marker,)},
                nodes=mutated_nodes,
            )
        )


def test_tm004_new_module_classification_needs_no_whitelist() -> None:
    _assert_marker_only_taxonomy(
        TaxonomyInventory(
            modules={"tests/test_new_contract.py": ("component",)},
            nodes={
                "tests/test_new_contract.py::test_first": ("component",),
                "tests/test_new_contract.py::test_second": ("component",),
            },
        )
    )


def test_tm005_behavioral_marker_prose_mirrors_canonical_definitions() -> None:
    assert APM_INSTRUCTIONS.read_bytes() == GITHUB_INSTRUCTIONS.read_bytes()
    for path in (APM_INSTRUCTIONS, GITHUB_INSTRUCTIONS, DOCS):
        assert _documented_marker_definitions(path) == EXPECTED_MARKERS
        text = path.read_text(encoding="utf-8")
        assert AUTHORITY_PROSE in text
        assert "uv run --frozen python scripts/check_test_assertions.py" in text
        assert "uv run --frozen python scripts/check_exact_test_duplicates.py" in text
        assert "--allow-provisional" not in text
