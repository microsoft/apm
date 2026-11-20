from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.test_static_facts import module_markers
from tests.quality.taxonomy import candidate_promises, fix_hint, load_promises, suggested_pytestmark
from tests.quality.taxonomy_inventory_plugin import spawns_processes

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
TAXONOMY_BASELINE = REPO_ROOT / "tests" / "quality" / "test_taxonomy_baseline.json"
PROMISE_CATALOG = REPO_ROOT / "tests" / "quality" / "promise-catalog.json"
SPEC_REQUIREMENT_ID = re.compile(r"^req-[a-z]{2,3}-[0-9]{3}$")
SPEC_FAMILY = "req"
P_FAMILY_ID = re.compile(r"^(P-[A-Z]+)-([A-Za-z0-9][A-Za-z0-9_.-]*)$")
LIFECYCLE_LEDGER = REPO_ROOT / "tests" / "fixtures" / "lifecycle_bug_ledger.json"
BASELINE_LISTS = ("misplaced_modules", "promise_exempt_modules", "unclassified_modules")
AUTHORITY_PROSE = "Module-level `pytestmark` is the sole behavioral classification authority."


@dataclass(frozen=True)
class TaxonomyInventory:
    """Collected module declarations and effective node marker sets."""

    modules: dict[str, tuple[str, ...]]
    nodes: dict[str, tuple[str, ...]]
    promises: dict[str, tuple[str, ...]] = field(default_factory=dict)
    spawning: frozenset[str] = frozenset()


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
    env["PYTHONPATH"] = os.pathsep.join(
        entry for entry in (str(REPO_ROOT), env.get("PYTHONPATH", "")) if entry
    )
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
    assert set(data) == {"modules", "nodes", "promises", "spawning"}
    assert isinstance(data["spawning"], list)
    return TaxonomyInventory(
        modules=_string_tuple_map(data["modules"], "modules"),
        nodes=_string_tuple_map(data["nodes"], "nodes"),
        promises=_string_tuple_map(data["promises"], "promises"),
        spawning=frozenset(data["spawning"]),
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
        assert "marker-only behavioral taxonomy" in text
        assert "critical_suite.toml" not in text
        assert "finite manifest" not in text.lower()
        assert "uv run --frozen python scripts/check_test_assertions.py" in text
        assert "uv run --frozen python scripts/check_exact_test_duplicates.py" in text
        assert "--allow-provisional" not in text


def _load_taxonomy_baseline(path: Path) -> dict[str, frozenset[str]]:
    """Load the sorted, one-entry-per-line taxonomy ratchet baseline."""
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"doc", "schema_version", *BASELINE_LISTS}, sorted(data)
    assert data["schema_version"] == 1
    assert isinstance(data["doc"], str) and data["doc"]
    lists: dict[str, frozenset[str]] = {}
    for key in BASELINE_LISTS:
        entries = data[key]
        assert isinstance(entries, list)
        assert entries == sorted(set(entries)), f"{key} must be sorted and unique"
        lists[key] = frozenset(entries)
    return lists


def _promise_family(promise_id: str) -> str | None:
    """Return the family of a promise id, or None when its prefix is unknown."""
    if SPEC_REQUIREMENT_ID.match(promise_id):
        return SPEC_FAMILY
    match = P_FAMILY_ID.match(promise_id)
    return match.group(1) if match else None


def _catalog_violations(data: object) -> list[str]:
    """Return every reason a promise catalog payload is invalid."""
    catalog_name = PROMISE_CATALOG.relative_to(REPO_ROOT).as_posix()
    if not isinstance(data, dict) or set(data) != {
        "doc",
        "families",
        "promises",
        "schema_version",
    }:
        return [f"{catalog_name}: keys must be doc, families, promises, schema_version"]
    violations: list[str] = []
    families = data["families"]
    if data["schema_version"] != 1:
        violations.append(f"{catalog_name}: schema_version must be 1")
    if not isinstance(families, dict) or not all(
        isinstance(name, str)
        and (name == SPEC_FAMILY or re.fullmatch(r"P-[A-Z]+", name))
        and isinstance(doc, str)
        and doc
        for name, doc in families.items()
    ):
        return [*violations, f"{catalog_name}: families must map P-<FAMILY> (or req) to a doc"]
    promises = data["promises"]
    if not isinstance(promises, dict) or not promises:
        return [*violations, f"{catalog_name}: promises must be a non-empty object"]
    for promise_id, source in sorted(promises.items()):
        family = _promise_family(promise_id)
        if family is None:
            violations.append(
                f"{catalog_name}: promise id {promise_id!r} has an unknown prefix. Fix: use "
                "P-<FAMILY>-<id> with a declared family, or a spec requirement id "
                "(req-xx-NNN) as written in CONFORMANCE.json."
            )
        elif family not in families:
            violations.append(
                f"{catalog_name}: promise id {promise_id!r} uses undeclared family "
                f"{family!r}. Fix: declare {family!r} in the catalog's families, or use "
                f"a declared family ({', '.join(sorted(families))})."
            )
        if not isinstance(source, str) or not source:
            violations.append(f"{catalog_name}: promise id {promise_id!r} needs a description")
    return violations


def _law_violations(catalog: frozenset[str], ledger: dict[str, object]) -> list[str]:
    """Return drift between lifecycle ledger laws and P-LAW catalog ids."""
    catalog_name = PROMISE_CATALOG.relative_to(REPO_ROOT).as_posix()
    rows = ledger["property_catalog"]
    assert isinstance(rows, list) and rows, "lifecycle property_catalog must be non-empty"
    laws = {f"P-LAW-{row['id']}" for row in rows}
    violations = [
        f"{catalog_name}: lifecycle law {law[len('P-LAW-') :]!r} has no promise id. "
        f"Fix: add {law!r} to the catalog."
        for law in sorted(laws - catalog)
    ]
    violations += [
        f"{catalog_name}: {promise_id!r} names no lifecycle law. Fix: remove it, or add "
        "the law to tests/fixtures/lifecycle_bug_ledger.json property_catalog."
        for promise_id in sorted(catalog)
        if promise_id.startswith("P-LAW-") and promise_id not in laws
    ]
    return violations


def _load_promise_catalog(path: Path) -> frozenset[str]:
    """Load promise ids from the catalog, failing on any invalid id or family."""
    data = json.loads(path.read_text(encoding="utf-8"))
    violations = _catalog_violations(data)
    assert not violations, "\n".join(f"[x] {violation}" for violation in violations)
    return frozenset(data["promises"])


def _moves(entries: frozenset[str], collected: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """Map collected unlisted modules to missing entries with the same file name."""
    missing: dict[str, list[str]] = {}
    for entry in sorted(entries - set(collected)):
        missing.setdefault(Path(entry).name, []).append(entry)
    moves: dict[str, str] = {}
    for module in sorted(set(collected) - entries):
        candidates = missing.get(Path(module).name)
        if candidates:
            moves[module] = candidates.pop(0)
    return moves


def _taxonomy_ratchet_findings(
    inventory: TaxonomyInventory,
    baseline: dict[str, frozenset[str]],
    catalog: frozenset[str],
    root: Path = REPO_ROOT,
    promises: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return (new violations, stale baseline entries) for the no-growth ratchets.

    A module moved to another directory under the same file name keeps the
    allowances of its now-missing baseline entry. A missing marker or promise
    names the exact ``pytestmark`` line to add (tests/quality/taxonomy.py).
    """
    described = promises if promises is not None else dict.fromkeys(catalog, "")
    failures: list[str] = []
    moves = {key: _moves(entries, inventory.modules) for key, entries in baseline.items()}
    stale = [
        f"{key} {old} moved to {new}; replace the entry"
        for key in BASELINE_LISTS
        for new, old in sorted(moves[key].items())
    ]
    original = baseline
    baseline = {key: entries | set(moves[key]) for key, entries in baseline.items()}
    unclassified: set[str] = set()
    misplaced: set[str] = set()
    catalog_name = PROMISE_CATALOG.relative_to(REPO_ROOT).as_posix()
    for module, markers in sorted(inventory.modules.items()):
        behavioral = _behavioral(markers)
        if not behavioral:
            unclassified.add(module)
            if module not in baseline["unclassified_modules"]:
                failures.append(
                    f"{module}: unclassified test module. Fix: add exactly one "
                    "module-level pytestmark behavioral marker (unit, component or e2e)."
                    + fix_hint(module, root, described)
                )
        if module.startswith("tests/integration/") and behavioral == ("unit",):
            misplaced.add(module)
            if module not in baseline["misplaced_modules"]:
                failures.append(
                    f"{module}: unit-marked module under tests/integration. Fix: move it "
                    "to tests/unit, or mark it component or e2e if it crosses a boundary."
                )
        if module.startswith("tests/unit/") and module in inventory.spawning:
            misplaced.add(module)
            if module not in baseline["misplaced_modules"]:
                failures.append(
                    f"{module}: tests/unit module spawns processes. Fix: move it to "
                    "tests/integration (marked component or e2e), or mock the process "
                    "boundary instead of spawning."
                )
        promises = inventory.promises.get(module, ())
        if not promises and module not in baseline["promise_exempt_modules"]:
            failures.append(
                f"{module}: new test module carries no pytest.mark.promise. Fix: add "
                'pytest.mark.promise("<id>", ...) to its module-level pytestmark, naming '
                f"ids from {catalog_name}." + fix_hint(module, root, described)
            )
        unknown = sorted(set(promises) - catalog)
        if unknown:
            failures.append(
                f"{module}: unknown promise id(s) {unknown}. Fix: use ids listed in "
                f"{catalog_name}, or add the promise to the catalog."
            )
    observed = {
        "misplaced_modules": misplaced,
        "promise_exempt_modules": set(inventory.modules),
        "unclassified_modules": unclassified,
    }
    stale += [
        f"{key} {module}"
        for key in BASELINE_LISTS
        for module in sorted(original[key] - observed[key] - set(moves[key].values()))
    ]
    return failures, stale


def _assert_taxonomy_ratchets(
    inventory: TaxonomyInventory,
    baseline: dict[str, frozenset[str]],
    catalog: frozenset[str],
    promises: dict[str, str] | None = None,
) -> list[str]:
    """Fail on new violations; return stale baseline entries for info output."""
    failures, stale = _taxonomy_ratchet_findings(inventory, baseline, catalog, promises=promises)
    assert not failures, "\n".join(f"[x] {failure}" for failure in failures)
    return stale


def test_tm006_taxonomy_ratchets_do_not_grow(
    marker_inventory: TaxonomyInventory,
) -> None:
    stale = _assert_taxonomy_ratchets(
        marker_inventory,
        _load_taxonomy_baseline(TAXONOMY_BASELINE),
        _load_promise_catalog(PROMISE_CATALOG),
        load_promises(PROMISE_CATALOG),
    )
    for entry in stale:
        print(f"[i] stale taxonomy baseline entry (may be pruned): {entry}")


def test_tm011_every_lifecycle_law_has_a_promise_id() -> None:
    catalog = _load_promise_catalog(PROMISE_CATALOG)
    ledger = json.loads(LIFECYCLE_LEDGER.read_text(encoding="utf-8"))
    violations = _law_violations(catalog, ledger)
    assert not violations, "\n".join(f"[x] {violation}" for violation in violations)


def _seeded_catalog(*extra_ids: str) -> dict[str, object]:
    return {
        "doc": "seed",
        "families": {"P-OWN": "owners", "req": "spec"},
        "promises": {"P-OWN-seed": "seed", "req-mf-001": "seed", **dict.fromkeys(extra_ids, "x")},
        "schema_version": 1,
    }


def test_tm012_unknown_promise_prefix_fails() -> None:
    assert _catalog_violations(_seeded_catalog()) == []
    assert _catalog_violations(_seeded_catalog("owner:seed")) == [
        "tests/quality/promise-catalog.json: promise id 'owner:seed' has an unknown prefix. "
        "Fix: use P-<FAMILY>-<id> with a declared family, or a spec requirement id "
        "(req-xx-NNN) as written in CONFORMANCE.json."
    ]


def test_tm012_undeclared_promise_family_fails() -> None:
    assert _catalog_violations(_seeded_catalog("P-DOC-001")) == [
        "tests/quality/promise-catalog.json: promise id 'P-DOC-001' uses undeclared family "
        "'P-DOC'. Fix: declare 'P-DOC' in the catalog's families, or use a declared family "
        "(P-OWN, req)."
    ]


def test_tm012_missing_lifecycle_law_promise_fails() -> None:
    ledger = {"property_catalog": [{"id": "outcome.status_matches_state"}, {"id": "new.law"}]}
    assert _law_violations(
        frozenset({"P-LAW-outcome.status_matches_state", "P-LAW-gone.law"}), ledger
    ) == [
        "tests/quality/promise-catalog.json: lifecycle law 'new.law' has no promise id. "
        "Fix: add 'P-LAW-new.law' to the catalog.",
        "tests/quality/promise-catalog.json: 'P-LAW-gone.law' names no lifecycle law. "
        "Fix: remove it, or add the law to tests/fixtures/lifecycle_bug_ledger.json "
        "property_catalog.",
    ]


def test_tm013_static_marker_reader_agrees_with_collection(
    marker_inventory: TaxonomyInventory,
) -> None:
    disagreements = {
        module: (sorted(static), sorted(collected))
        for module, collected in marker_inventory.modules.items()
        if (
            static := set(
                module_markers(ast.parse((REPO_ROOT / module).read_text(encoding="utf-8")))
            )
            & BEHAVIORAL_MARKERS
        )
        != set(_behavioral(collected))
    }
    assert disagreements == {}, (
        "scripts/test_static_facts.py module_markers must read the same module-level "
        f"pytestmark that pytest collects: {disagreements}"
    )


def test_tm006_ratchet_files_are_canonical_json() -> None:
    for path in (TAXONOMY_BASELINE, PROMISE_CATALOG):
        text = path.read_text(encoding="utf-8")
        assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n", (
            f"{path.name} must be sorted JSON with one entry per line"
        )


_SEEDED_PYTEST_INI = """[pytest]
markers =
    unit: unit
    component: component
    e2e: e2e
    promise: promise
"""
_SEEDED_MODULES = {
    "tests/unit/test_legacy.py": "def test_legacy():\n    assert True\n",
    "tests/integration/test_moved.py": "def test_moved():\n    assert True\n",
    "tests/unit/test_new_unclassified.py": (
        'import pytest\n\npytestmark = pytest.mark.promise("P-OWN-seed")\n\n'
        "def test_value():\n    assert True\n"
    ),
    "tests/integration/test_new_unit.py": (
        'import pytest\n\npytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-seed")]\n\n'
        "def test_value():\n    assert True\n"
    ),
    "tests/unit/test_new_spawn.py": (
        "import subprocess\n\nimport pytest\n\n"
        'pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-seed")]\n\n'
        'def test_value():\n    subprocess.run(["true"], check=False)\n'
    ),
    "tests/unit/test_new_promiseless.py": (
        "import pytest\n\npytestmark = pytest.mark.unit\n\ndef test_value():\n    assert True\n"
    ),
    "tests/unit/test_new_unknown_promise.py": (
        'import pytest\n\npytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-nope")]\n\n'
        "def test_value():\n    assert True\n"
    ),
    "tests/unit/test_new_compliant.py": (
        'import pytest\n\npytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-seed")]\n\n'
        "def test_value():\n    assert True\n"
    ),
}


@pytest.fixture(scope="module")
def seeded_findings(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[list[str], list[str]]:
    """Collect a tmp tree that seeds one violation per ratchet."""
    root = tmp_path_factory.mktemp("seeded")
    (root / "pytest.ini").write_text(_SEEDED_PYTEST_INI, encoding="utf-8")
    for relative, source in _SEEDED_MODULES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    inventory = _collect_inventory(root, root / "inventory.json")
    baseline = {
        "misplaced_modules": frozenset({"tests/unit/test_removed_spawn.py"}),
        "promise_exempt_modules": frozenset(
            {"tests/unit/test_legacy.py", "tests/unit/test_moved.py"}
        ),
        "unclassified_modules": frozenset(
            {"tests/unit/test_legacy.py", "tests/unit/test_moved.py", "tests/unit/test_removed.py"}
        ),
    }
    return _taxonomy_ratchet_findings(
        inventory, baseline, frozenset({"P-OWN-seed"}), root, {"P-OWN-seed": "new seed promise"}
    )


def _failures_for(findings: tuple[list[str], list[str]], module: str) -> list[str]:
    return [failure for failure in findings[0] if failure.startswith(f"{module}: ")]


def test_tm007_new_unclassified_module_fails(
    seeded_findings: tuple[list[str], list[str]],
) -> None:
    assert _failures_for(seeded_findings, "tests/unit/test_new_unclassified.py") == [
        "tests/unit/test_new_unclassified.py: unclassified test module. Fix: add exactly one "
        "module-level pytestmark behavioral marker (unit, component or e2e). Suggested: "
        'pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-seed")]'
    ]


def test_tm008_unit_module_under_integration_fails(
    seeded_findings: tuple[list[str], list[str]],
) -> None:
    failures = _failures_for(seeded_findings, "tests/integration/test_new_unit.py")
    assert len(failures) == 1
    assert "unit-marked module under tests/integration. Fix: move it to tests/unit" in (failures[0])


def test_tm008_process_spawning_unit_module_fails(
    seeded_findings: tuple[list[str], list[str]],
) -> None:
    failures = _failures_for(seeded_findings, "tests/unit/test_new_spawn.py")
    assert len(failures) == 1
    assert "tests/unit module spawns processes. Fix: move it to tests/integration" in (failures[0])


def test_tm009_new_module_without_promise_fails(
    seeded_findings: tuple[list[str], list[str]],
) -> None:
    failures = _failures_for(seeded_findings, "tests/unit/test_new_promiseless.py")
    assert len(failures) == 1
    assert "new test module carries no pytest.mark.promise. Fix: add" in failures[0]
    assert "tests/quality/promise-catalog.json" in failures[0]


def test_tm009_unknown_promise_id_fails(
    seeded_findings: tuple[list[str], list[str]],
) -> None:
    failures = _failures_for(seeded_findings, "tests/unit/test_new_unknown_promise.py")
    assert failures == [
        "tests/unit/test_new_unknown_promise.py: unknown promise id(s) ['P-OWN-nope']. "
        "Fix: use ids listed in tests/quality/promise-catalog.json, or add the promise "
        "to the catalog."
    ]


def test_tm010_legacy_moved_compliant_and_removed_entries_do_not_fail(
    seeded_findings: tuple[list[str], list[str]],
) -> None:
    failures, stale = seeded_findings
    assert _failures_for(seeded_findings, "tests/unit/test_legacy.py") == []
    assert _failures_for(seeded_findings, "tests/unit/test_new_compliant.py") == []
    assert _failures_for(seeded_findings, "tests/integration/test_moved.py") == []
    assert len(failures) == 5
    assert stale == [
        "promise_exempt_modules tests/unit/test_moved.py moved to "
        "tests/integration/test_moved.py; replace the entry",
        "unclassified_modules tests/unit/test_moved.py moved to "
        "tests/integration/test_moved.py; replace the entry",
        "misplaced_modules tests/unit/test_removed_spawn.py",
        "unclassified_modules tests/unit/test_removed.py",
    ]
    with pytest.raises(AssertionError, match=r"\[x\] tests/unit/test_new_promiseless\.py"):
        _assert_taxonomy_ratchets(
            TaxonomyInventory(
                modules={"tests/unit/test_new_promiseless.py": ("unit",)},
                nodes={"tests/unit/test_new_promiseless.py::test_value": ("unit",)},
            ),
            {key: frozenset() for key in BASELINE_LISTS},
            frozenset(),
        )


@pytest.mark.parametrize(
    ("source", "spawns"),
    [
        pytest.param(
            "def test_a():\n    import subprocess as api\n    api.run(['x'])\n\n"
            "def test_b():\n    import os as api\n    api.getcwd()\n",
            True,
            id="conflicting-aliases-in-separate-tests",
        ),
        pytest.param(
            "def test_a():\n    from os import system\n\ndef test_b():\n    system('x')\n",
            False,
            id="alias-does-not-leak-across-functions",
        ),
        pytest.param(
            "from asyncio.subprocess import create_subprocess_exec\n\n"
            "async def test():\n    await create_subprocess_exec('x')\n",
            True,
            id="asyncio-subprocess-direct-import",
        ),
        pytest.param(
            "import asyncio\n\nasync def test():\n"
            "    await asyncio.subprocess.create_subprocess_shell('x')\n",
            True,
            id="asyncio-subprocess-attribute",
        ),
    ],
)
def test_tm014_spawn_detection_respects_scopes_and_asyncio(source: str, spawns: bool) -> None:
    assert spawns_processes(source) is spawns


def _allowance_repo(root: Path, lists: dict[str, list[str]]) -> Path:
    baseline = root / "tests" / "quality" / "test_taxonomy_baseline.json"
    baseline.parent.mkdir(parents=True)
    payload = {"doc": "x", "schema_version": 1, **lists}
    baseline.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for command in (
        ["init", "-q"],
        ["add", "-A"],
        [
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "base",
        ],
    ):
        subprocess.run(["git", "-C", str(root), *command], check=True, capture_output=True)
    return baseline


def _run_allowances(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_taxonomy_allowances.py"),
            "--root",
            str(root),
            "--base-ref",
            "HEAD",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_tm015_appending_an_allowance_fails_against_base(tmp_path: Path) -> None:
    lists = {
        "misplaced_modules": [],
        "promise_exempt_modules": ["tests/unit/test_old.py"],
        "unclassified_modules": ["tests/unit/test_old.py"],
    }
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_old.py").write_text("", encoding="utf-8")
    baseline = _allowance_repo(tmp_path, lists)
    grown = {**lists, "unclassified_modules": ["tests/unit/test_new.py", "tests/unit/test_old.py"]}
    baseline.write_text(json.dumps({"doc": "x", "schema_version": 1, **grown}), encoding="utf-8")

    result = _run_allowances(tmp_path)

    assert result.returncode == 1
    assert (
        "[x] tests/unit/test_new.py: added to unclassified_modules in "
        "tests/quality/test_taxonomy_baseline.json. Fix:"
    ) in result.stderr


def test_tm015_validated_move_rekey_passes(tmp_path: Path) -> None:
    lists = {
        "misplaced_modules": [],
        "promise_exempt_modules": ["tests/unit/test_moved.py"],
        "unclassified_modules": ["tests/unit/test_moved.py"],
    }
    baseline = _allowance_repo(tmp_path, lists)
    (tmp_path / "tests" / "integration").mkdir(parents=True)
    (tmp_path / "tests" / "integration" / "test_moved.py").write_text("", encoding="utf-8")
    moved = {
        key: [entry.replace("unit", "integration") for entry in value]
        for key, value in lists.items()
    }
    baseline.write_text(json.dumps({"doc": "x", "schema_version": 1, **moved}), encoding="utf-8")

    result = _run_allowances(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "[+] taxonomy allowances did not grow" in result.stdout


_SUGGESTION_CATALOG = {
    "P-OWN-lockfile-write": "who writes apm.lock.yaml",
    "P-OWN-marketplace-fetch": "how a marketplace catalog is fetched",
    "P-DOC-cli-surface": "the docs describe the shipped CLI",
    "req-lk-002": "the lockfile records the resolved source",
}


def test_tm016_suggestion_ranks_mentioned_requirements_then_name_tokens() -> None:
    source = '"""Covers req-lk-002."""\n\ndef test_value():\n    assert True\n'
    assert candidate_promises("tests/unit/test_lockfile_write.py", source, _SUGGESTION_CATALOG) == [
        "req-lk-002",
        "P-OWN-lockfile-write",
    ]
    assert candidate_promises("tests/unit/test_unrelated.py", "", _SUGGESTION_CATALOG) == []


@pytest.mark.parametrize(
    ("module", "source", "behavioral"),
    [
        ("tests/unit/test_marketplace_fetch.py", "", "unit"),
        ("tests/lifecycle/test_marketplace_fetch.py", "", "e2e"),
        ("tests/integration/test_marketplace_fetch.py", "", "component"),
        ("tests/integration/test_marketplace_fetch.py", "def test(apm_binary_path): pass\n", "e2e"),
    ],
)
def test_tm016_suggested_line_infers_the_behavioral_marker(
    module: str, source: str, behavioral: str
) -> None:
    line, ids = suggested_pytestmark(module, source, _SUGGESTION_CATALOG)
    assert ids == ["P-OWN-marketplace-fetch"]
    assert line == (
        f'pytestmark = [pytest.mark.{behavioral}, pytest.mark.promise("P-OWN-marketplace-fetch")]'
    )
