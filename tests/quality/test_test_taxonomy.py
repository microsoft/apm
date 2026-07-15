from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import tomllib

from tests.quality.repository_python_inventory import (
    PythonModuleFacts,
    tracked_python_inventory,
)

RATCHET_TEST_SCOPE = "repository"

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
MANIFEST = REPO_ROOT / "tests" / "quality" / "critical_suite.toml"
DOCS = REPO_ROOT / "docs" / "src" / "content" / "docs" / "contributing" / "integration-testing.md"
APM_INSTRUCTIONS = REPO_ROOT / ".apm" / "instructions" / "tests.instructions.md"
GITHUB_INSTRUCTIONS = REPO_ROOT / ".github" / "instructions" / "tests.instructions.md"
EXPECTED_MARKERS = {
    "unit": "pure logic with no filesystem and no CLI",
    "component": ("in-process behavior that touches a filesystem or one command boundary"),
    "e2e": "a real installed CLI crossing at least one command boundary",
}
BEHAVIORAL_MARKERS = frozenset(EXPECTED_MARKERS)
INVENTORY_PLUGIN = "tests.quality.taxonomy_inventory_plugin"


def _declared_markers() -> dict[str, str]:
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    entries = data["tool"]["pytest"]["ini_options"]["markers"]
    declared: dict[str, str] = {}
    for entry in entries:
        name, separator, description = entry.partition(":")
        if separator:
            declared[name.strip()] = description.strip()
    return declared


def _manifest_modules(manifest: Path = MANIFEST) -> list[dict[str, str]]:
    with manifest.open("rb") as handle:
        data = tomllib.load(handle)
    modules = data.get("modules")
    assert data == {"schema_version": 1, "modules": modules}
    assert isinstance(modules, list)

    normalized: list[dict[str, str]] = []
    for entry in modules:
        assert isinstance(entry, dict)
        assert set(entry) == {"path", "marker"}
        path = entry["path"]
        marker = entry["marker"]
        assert isinstance(path, str)
        assert isinstance(marker, str)
        normalized.append({"path": path, "marker": marker})
    return normalized


def _documented_marker_definitions(path: Path) -> dict[str, str]:
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


def _collect_inventory(root: Path, output: Path) -> dict[str, list[str]]:
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
            "--collect-only",
            "-q",
            "tests",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _assert_behavioral_marker_scope(
    manifest: Path,
    inventory: dict[str, list[str]],
) -> None:
    modules = _manifest_modules(manifest)
    manifest_paths = {entry["path"] for entry in modules}
    manifest_nodes = {nodeid for nodeid in inventory if nodeid.split("::", 1)[0] in manifest_paths}
    globally_marked_nodes = {
        nodeid for nodeid, markers in inventory.items() if BEHAVIORAL_MARKERS.intersection(markers)
    }
    extras = sorted(globally_marked_nodes - manifest_nodes)
    missing = sorted(manifest_nodes - globally_marked_nodes)
    assert extras == [], f"behavioral markers outside critical manifest: {extras}"
    assert missing == [], f"manifest nodes missing behavioral markers: {missing}"


@pytest.fixture(scope="module")
def marker_inventory(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, list[str]]:
    output = tmp_path_factory.mktemp("taxonomy") / "inventory.json"
    return _collect_inventory(REPO_ROOT, output)


def test_tm001_behavioral_marker_definitions_match_spec() -> None:
    declared = _declared_markers()
    actual = {marker: declared.get(marker) for marker in EXPECTED_MARKERS}
    assert actual == EXPECTED_MARKERS


def test_tm002_manifest_is_finite_unique_and_existing() -> None:
    modules = _manifest_modules()
    assert len(modules) == 10
    paths = [entry["path"] for entry in modules]
    assert len(paths) == len(set(paths))
    assert {entry["marker"] for entry in modules} == BEHAVIORAL_MARKERS
    missing = [path for path in paths if not (REPO_ROOT / path).is_file()]
    assert missing == []


def _assert_manifest_is_only_module_list(
    python_inventory: dict[str, PythonModuleFacts],
) -> None:
    manifest_paths = {entry["path"] for entry in _manifest_modules()}
    duplicated_literals = {
        path: sorted(facts.string_literals.intersection(manifest_paths))
        for path, facts in python_inventory.items()
        if facts.string_literals.intersection(manifest_paths)
    }

    assert duplicated_literals == {}, (
        f"critical module paths must be declared only in critical_suite.toml: {duplicated_literals}"
    )


def test_tm002_manifest_is_the_only_critical_module_list(
    repository_python_inventory: dict[str, PythonModuleFacts],
) -> None:
    _assert_manifest_is_only_module_list(repository_python_inventory)


def test_tm002_manifest_literal_outside_quality_fails(tmp_path: Path) -> None:
    manifest_path = next(iter(entry["path"] for entry in _manifest_modules()))
    duplicate = tmp_path / "scripts" / "duplicate_authority.py"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text(
        f"DUPLICATE_MODULE = {manifest_path!r}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "scripts"], check=True)

    with pytest.raises(
        AssertionError,
        match="critical module paths must be declared only",
    ):
        _assert_manifest_is_only_module_list(tracked_python_inventory(tmp_path))


def test_tm003_each_manifest_module_has_exactly_its_declared_marker(
    marker_inventory: dict[str, list[str]],
) -> None:
    modules = _manifest_modules()
    for entry in modules:
        module_nodes = {
            nodeid: markers
            for nodeid, markers in marker_inventory.items()
            if nodeid.split("::", 1)[0] == entry["path"]
        }
        assert module_nodes
        for markers in module_nodes.values():
            assert BEHAVIORAL_MARKERS.intersection(markers) == {entry["marker"]}


def test_tm004_behavioral_markers_are_not_used_outside_manifest(
    marker_inventory: dict[str, list[str]],
) -> None:
    _assert_behavioral_marker_scope(MANIFEST, marker_inventory)


def test_tm004_rejects_an_unmanifested_behavioral_marker(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    tests = root / "tests"
    quality = tests / "quality"
    quality.mkdir(parents=True)
    (tests / "test_manifested.py").write_text(
        "def test_manifested():\n    assert 1 == 1\n",
        encoding="utf-8",
    )
    (tests / "test_unmanifested.py").write_text(
        "def test_unmanifested():\n    assert 1 == 1\n",
        encoding="utf-8",
    )
    manifest = quality / "critical_suite.toml"
    manifest.write_text(
        'schema_version = 1\n[[modules]]\npath = "tests/test_manifested.py"\nmarker = "unit"\n',
        encoding="utf-8",
    )

    with pytest.raises(
        AssertionError,
        match="behavioral markers outside critical manifest",
    ):
        _assert_behavioral_marker_scope(
            manifest,
            {
                "tests/test_manifested.py::test_manifested": ["unit"],
                "tests/test_unmanifested.py::test_unmanifested": ["e2e"],
            },
        )


def test_tm005_behavioral_marker_prose_mirrors_canonical_definitions() -> None:
    assert APM_INSTRUCTIONS.read_bytes() == GITHUB_INSTRUCTIONS.read_bytes()
    for path in (APM_INSTRUCTIONS, GITHUB_INSTRUCTIONS, DOCS):
        assert _documented_marker_definitions(path) == EXPECTED_MARKERS
        text = path.read_text(encoding="utf-8")
        assert "uv run --frozen python scripts/check_test_assertions.py" in text
        assert "uv run --frozen python scripts/check_exact_test_duplicates.py" in text
        assert "--allow-provisional" not in text
