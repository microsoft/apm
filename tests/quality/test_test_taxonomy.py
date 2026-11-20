from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.quality.taxonomy_ratchets import (
    CATALOG,
    UPDATE_COMMAND,
    MergeBaseUnavailable,
    RatchetBaseline,
    TaxonomyInventory,
    baseline_growth,
    catalog_payload,
    catalog_problems,
    catalog_source_ids,
    collect_inventory,
    load_catalog_ids,
    load_ratchet_baseline,
    merge_base_text,
    placement_violations,
    promise_violations,
    stale_entries,
    unclassified_growth,
)

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
AUTHORITY_PROSE = "Module-level `pytestmark` is the sole behavioral classification authority."


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
    return collect_inventory(REPO_ROOT, output)


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
        assert "marker-only behavioral taxonomy" in text
        assert "critical_suite.toml" not in text
        assert "finite manifest" not in text.lower()
        assert "uv run --frozen python scripts/check_test_assertions.py" in text
        assert "uv run --frozen python scripts/check_exact_test_duplicates.py" in text
        assert "--allow-provisional" not in text


NEW_MODULE = "tests/unit/test_brand_new_contract.py"
NEW_INTEGRATION_MODULE = "tests/integration/test_brand_new_contract.py"
KNOWN_PROMISE = "req-mf-001"
EMPTY_BASELINE = RatchetBaseline(legacy_modules=frozenset(), unclassified_modules=frozenset())


@pytest.fixture(scope="module")
def ratchet_baseline() -> RatchetBaseline:
    return load_ratchet_baseline(REPO_ROOT)


@pytest.fixture(scope="module")
def catalog_ids() -> frozenset[str]:
    return load_catalog_ids(REPO_ROOT)


def _single_module(
    module: str, markers: tuple[str, ...], promises: tuple[str, ...] = ()
) -> TaxonomyInventory:
    return TaxonomyInventory(
        modules={module: markers},
        nodes={f"{module}::test_value": markers},
        promises={module: promises} if promises else {},
    )


def _write_module(root: Path, module: str, source: str) -> None:
    path = root / module
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_tm006_unclassified_modules_do_not_grow(
    marker_inventory: TaxonomyInventory,
    ratchet_baseline: RatchetBaseline,
) -> None:
    assert unclassified_growth(marker_inventory, ratchet_baseline) == []


def test_tm006_new_unclassified_module_fails() -> None:
    problems = unclassified_growth(_single_module(NEW_MODULE, ()), EMPTY_BASELINE)

    assert len(problems) == 1
    assert problems[0].startswith(f"{NEW_MODULE}: unclassified test module")
    assert "add exactly one module-level behavioral marker" in problems[0]


def test_tm006_baselined_unclassified_module_passes() -> None:
    baseline = RatchetBaseline(
        legacy_modules=frozenset({NEW_MODULE}),
        unclassified_modules=frozenset({NEW_MODULE}),
    )

    assert unclassified_growth(_single_module(NEW_MODULE, ()), baseline) == []


def test_tm007_new_modules_are_placed_by_behavior(
    marker_inventory: TaxonomyInventory,
    ratchet_baseline: RatchetBaseline,
) -> None:
    assert placement_violations(marker_inventory, ratchet_baseline, REPO_ROOT) == []


def test_tm007_new_unit_module_under_integration_fails(tmp_path: Path) -> None:
    _write_module(tmp_path, NEW_INTEGRATION_MODULE, "def test_value():\n    pass\n")

    problems = placement_violations(
        _single_module(NEW_INTEGRATION_MODULE, ("unit",)), EMPTY_BASELINE, tmp_path
    )

    assert len(problems) == 1
    assert problems[0].startswith(f"{NEW_INTEGRATION_MODULE}: new module under tests/integration")
    assert "move it to tests/unit" in problems[0]


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\n\ndef test_value():\n    subprocess.run(['git'])\n",
        "from subprocess import check_output as co\n\ndef test_value():\n    co(['git'])\n",
        "import os\n\ndef test_value():\n    os.execv('/bin/true', [])\n",
    ],
)
def test_tm007_new_unit_module_spawning_a_process_fails(tmp_path: Path, source: str) -> None:
    _write_module(tmp_path, NEW_MODULE, source)

    problems = placement_violations(_single_module(NEW_MODULE, ("unit",)), EMPTY_BASELINE, tmp_path)

    assert len(problems) == 1
    assert problems[0].startswith(f"{NEW_MODULE}: new module under tests/unit spawns a process")
    assert "line 4:" in problems[0]
    assert "move the module to tests/integration" in problems[0]


def test_tm007_mocked_process_and_legacy_modules_pass(tmp_path: Path) -> None:
    _write_module(
        tmp_path,
        NEW_MODULE,
        "import subprocess\n\n"
        "def test_value(monkeypatch):\n"
        "    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: None)\n",
    )
    _write_module(tmp_path, NEW_INTEGRATION_MODULE, "import subprocess\nsubprocess.run(['git'])\n")
    inventory = TaxonomyInventory(
        modules={NEW_MODULE: ("unit",), NEW_INTEGRATION_MODULE: ("unit",)},
        nodes={},
    )
    baseline = RatchetBaseline(
        legacy_modules=frozenset({NEW_INTEGRATION_MODULE}), unclassified_modules=frozenset()
    )

    assert placement_violations(inventory, baseline, tmp_path) == []


def test_tm008_new_modules_link_known_promises(
    marker_inventory: TaxonomyInventory,
    ratchet_baseline: RatchetBaseline,
    catalog_ids: frozenset[str],
) -> None:
    assert promise_violations(marker_inventory, ratchet_baseline, catalog_ids) == []


def test_tm008_new_module_without_promise_fails(catalog_ids: frozenset[str]) -> None:
    problems = promise_violations(
        _single_module(NEW_MODULE, ("unit",)), EMPTY_BASELINE, catalog_ids
    )

    assert len(problems) == 1
    assert problems[0].startswith(f"{NEW_MODULE}: new test module declares no promise")
    assert f"naming ids from {CATALOG.as_posix()}" in problems[0]


def test_tm008_unknown_promise_id_fails(catalog_ids: frozenset[str]) -> None:
    inventory = _single_module(NEW_MODULE, ("unit",), (KNOWN_PROMISE, "req-zz-999"))

    problems = promise_violations(inventory, EMPTY_BASELINE, catalog_ids)

    assert len(problems) == 1
    assert problems[0].startswith(f"{NEW_MODULE}: pytest.mark.promise names unknown id(s)")
    assert "'req-zz-999'" in problems[0]


def test_tm008_new_module_with_known_promise_passes(catalog_ids: frozenset[str]) -> None:
    inventory = _single_module(NEW_MODULE, ("unit",), (KNOWN_PROMISE,))

    assert promise_violations(inventory, EMPTY_BASELINE, catalog_ids) == []


def test_tm008_inventory_records_module_promises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalog_ids: frozenset[str],
) -> None:
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    unit: unit\n    promise: promise\n",
        encoding="utf-8",
    )
    _write_module(
        tmp_path,
        "tests/test_linked.py",
        "import pytest\n\n"
        f"pytestmark = [pytest.mark.unit, pytest.mark.promise({KNOWN_PROMISE!r})]\n\n"
        "def test_value():\n    pass\n",
    )
    _write_module(
        tmp_path,
        "tests/test_unlinked.py",
        "import pytest\n\npytestmark = pytest.mark.unit\n\ndef test_value():\n    pass\n",
    )
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))

    inventory = collect_inventory(tmp_path, tmp_path / "inventory.json")
    problems = promise_violations(inventory, EMPTY_BASELINE, catalog_ids)

    assert inventory.promises == {"tests/test_linked.py": (KNOWN_PROMISE,)}
    assert [problem.split(":", 1)[0] for problem in problems] == ["tests/test_unlinked.py"]


def test_tm009_taxonomy_baseline_has_no_stale_entries(
    marker_inventory: TaxonomyInventory,
    ratchet_baseline: RatchetBaseline,
) -> None:
    stale_legacy, stale_unclassified = stale_entries(marker_inventory, ratchet_baseline, REPO_ROOT)

    assert (stale_legacy, stale_unclassified) == (set(), set()), (
        f"taxonomy ratchet baseline is stale; run '{UPDATE_COMMAND}'"
    )


def test_tm009_stale_entries_are_detected(tmp_path: Path) -> None:
    classified = "tests/unit/test_classified.py"
    _write_module(tmp_path, classified, "def test_value():\n    pass\n")
    baseline = RatchetBaseline(
        legacy_modules=frozenset({classified, NEW_MODULE}),
        unclassified_modules=frozenset({classified, NEW_MODULE}),
    )

    stale = stale_entries(_single_module(classified, ("unit",)), baseline, tmp_path)

    assert stale == ({NEW_MODULE}, {classified, NEW_MODULE})


def test_tm010_promise_catalog_resolves_to_sources() -> None:
    payload = json.loads((REPO_ROOT / CATALOG).read_text(encoding="utf-8"))

    assert catalog_problems(payload, catalog_source_ids(REPO_ROOT)) == []


def test_tm010_dangling_catalog_id_fails() -> None:
    payload = {
        "description": "fixture",
        "promises": [{"id": "req-zz-999", "source": "spec"}],
        "schema_version": 1,
    }

    problems = catalog_problems(payload, {"architecture-owner": frozenset(), "spec": frozenset()})

    assert len(problems) == 1
    assert "'req-zz-999' no longer exists in its spec source" in problems[0]


def test_tm010_promise_marker_is_registered() -> None:
    assert "promise" in _declared_markers()


def test_tm010_promise_catalog_matches_its_builder() -> None:
    payload = json.loads((REPO_ROOT / CATALOG).read_text(encoding="utf-8"))

    assert payload == catalog_payload(catalog_source_ids(REPO_ROOT))


def test_tm011_hand_added_baseline_entries_fail() -> None:
    base = RatchetBaseline(
        legacy_modules=frozenset({NEW_MODULE}), unclassified_modules=frozenset({NEW_MODULE})
    )
    grown = RatchetBaseline(
        legacy_modules=frozenset({NEW_MODULE, NEW_INTEGRATION_MODULE}),
        unclassified_modules=frozenset({NEW_MODULE, NEW_INTEGRATION_MODULE}),
    )

    problems = baseline_growth(base, grown)

    assert len(problems) == 2
    assert (
        f"legacy_modules grew relative to the merge-base: ['{NEW_INTEGRATION_MODULE}']"
        in (problems[0])
    )
    assert baseline_growth(grown, base) == []
    assert baseline_growth(None, grown) == []


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=ratchet",
            "-c",
            "user.email=ratchet@example.invalid",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_tm011_merge_base_copy_is_read_from_the_base_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative = Path("tests") / "quality" / "baseline.json"
    _write_module(tmp_path, relative.as_posix(), "base\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    _write_module(tmp_path, relative.as_posix(), "feature\n")
    _git(tmp_path, "commit", "-q", "-am", "feature")
    monkeypatch.setenv("APM_RATCHET_BASE_REF", "main")

    assert merge_base_text(tmp_path, relative) == "base\n"
    assert merge_base_text(tmp_path, Path("missing.json")) is None
    monkeypatch.setenv("APM_RATCHET_BASE_REF", "no-such-ref")
    with pytest.raises(MergeBaseUnavailable, match="cannot find the merge-base"):
        merge_base_text(tmp_path, relative)
