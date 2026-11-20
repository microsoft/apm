"""Contracts for the strict canonical-owner registry loader."""

from __future__ import annotations

import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from scripts.architecture_linter.registry import (
    RegistryError,
    load_registry,
    load_registry_documents,
    promise_vocabulary,
)

ROOT = Path(__file__).parents[3]
REGISTRY_DIR = ROOT / ".apm/architecture/owners"
OWNER_GATE = ROOT / "packages/autopilot/autopilot-pr-merge-worker/scripts/owner_touch_gate.py"


def _owner(
    *,
    owner_id: str = "fixture-owner",
    decision: str = "Fixture decision",
    selector: str = "src/owner.py",
    guards: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": owner_id,
        "decision": decision,
        "owner": "core/owner.py (FixtureOwner)",
        "selectors": [selector],
        "guards": guards or ["registry-delegation-fixture-owner"],
    }


def _documents(
    owner: dict[str, Any] | None = None,
) -> tuple[bytes, dict[str, bytes], list[str]]:
    index = {"version": 1, "shards": ["core-runtime.json"]}
    shard = {"version": 1, "owners": [owner or _owner()]}
    return (
        json.dumps(index).encode("ascii"),
        {"core-runtime.json": json.dumps(shard).encode("ascii")},
        ["src/owner.py"],
    )


def _load_parts(
    index: bytes,
    shards: dict[str, bytes],
    inventory: list[str],
) -> Any:
    return load_registry_documents(index, shards, inventory)


def test_repository_registry_is_complete_without_a_markdown_mirror() -> None:
    """Every listed shard contributes unique, guarded executable metadata."""
    inventory = [path.relative_to(ROOT).as_posix() for path in ROOT.rglob("*") if path.is_file()]
    registry = load_registry(REGISTRY_DIR, inventory)
    disk_shards = {path.name for path in REGISTRY_DIR.glob("*.json") if path.name != "index.json"}
    owner_ids = {owner.id for owner in registry.owners}
    decisions = {owner.decision for owner in registry.owners}
    selectors = {selector for owner in registry.owners for selector in owner.selectors}

    assert set(registry.shards) == disk_shards
    assert 5 <= len(registry.shards) <= 7
    assert len(owner_ids) == len(registry.owners)
    assert len(decisions) == len(registry.owners)
    assert sum(len(owner.selectors) for owner in registry.owners) == len(selectors)
    assert all(owner.guards for owner in registry.owners)


@pytest.mark.parametrize(
    ("mutate", "diagnostic"),
    [
        (
            lambda index, shard: index.update({"extra": True}),
            "unknown fields",
        ),
        (
            lambda index, shard: index.update({"version": 2}),
            "version must be 1",
        ),
        (
            lambda index, shard: shard.update({"extra": True}),
            "unknown fields",
        ),
        (
            lambda index, shard: shard["owners"][0].update({"extra": True}),
            "unknown fields",
        ),
        (
            lambda index, shard: shard["owners"][0].update({"guards": []}),
            "guards must be a non-empty array",
        ),
        (
            lambda index, shard: shard["owners"][0].update({"id": "Not_Kebab"}),
            "stable kebab-case ID",
        ),
        (
            lambda index, shard: shard["owners"][0].update({"decision": "Non-ASCII \N{SNOWMAN}"}),
            "printable ASCII",
        ),
        (
            lambda index, shard: index.update(
                {"shards": ["core-runtime.json", "core-runtime.json"]}
            ),
            "contains duplicates",
        ),
    ],
)
def test_schema_drift_fails_closed(mutate: Any, diagnostic: str) -> None:
    """Versions, fields, IDs, guards, shard lists, and ASCII are strict."""
    index = {"version": 1, "shards": ["core-runtime.json"]}
    shard = {"version": 1, "owners": [_owner()]}
    mutate(index, shard)

    with pytest.raises(RegistryError, match=diagnostic):
        _load_parts(
            json.dumps(index, ensure_ascii=False).encode("utf-8"),
            {"core-runtime.json": json.dumps(shard, ensure_ascii=False).encode("utf-8")},
            ["src/owner.py"],
        )


@pytest.mark.parametrize(
    ("field", "replacement", "diagnostic"),
    [
        ("id", "fixture-owner", "duplicate canonical owner ID"),
        ("decision", "Fixture decision", "duplicate canonical owner decision"),
        ("selectors", ["src/owner.py"], "duplicate canonical owner selector"),
    ],
)
def test_duplicates_across_shards_fail_closed(
    field: str,
    replacement: Any,
    diagnostic: str,
) -> None:
    """IDs, decisions, and selectors are globally unique, not shard-local."""
    index = {"version": 1, "shards": ["core-runtime.json", "contracts-tooling.json"]}
    second = _owner(
        owner_id="other-owner",
        decision="Other decision",
        selector="src/other.py",
        guards=["registry-delegation-other-owner"],
    )
    second[field] = replacement
    shards = {
        "core-runtime.json": json.dumps({"version": 1, "owners": [_owner()]}).encode("ascii"),
        "contracts-tooling.json": json.dumps({"version": 1, "owners": [second]}).encode("ascii"),
    }

    with pytest.raises(RegistryError, match=diagnostic):
        _load_parts(json.dumps(index).encode("ascii"), shards, ["src/owner.py", "src/other.py"])


def test_guard_id_cannot_be_assigned_to_multiple_owners_across_shards() -> None:
    """A guard is part of exactly one durable owner's enforcement boundary."""
    index = {"version": 1, "shards": ["core-runtime.json", "contracts-tooling.json"]}
    shared_guard = ["registry-delegation-shared"]
    shards = {
        "core-runtime.json": json.dumps(
            {"version": 1, "owners": [_owner(guards=shared_guard)]}
        ).encode("ascii"),
        "contracts-tooling.json": json.dumps(
            {
                "version": 1,
                "owners": [
                    _owner(
                        owner_id="other-owner",
                        decision="Other decision",
                        selector="src/other.py",
                        guards=shared_guard,
                    )
                ],
            }
        ).encode("ascii"),
    }

    with pytest.raises(RegistryError, match="guard ID assigned to multiple owners"):
        _load_parts(json.dumps(index).encode("ascii"), shards, ["src/owner.py", "src/other.py"])


def test_cross_shard_selector_overlap_fails_closed() -> None:
    """Distinct selectors cannot make one tracked file answer to two owners."""
    index = {"version": 1, "shards": ["core-runtime.json", "contracts-tooling.json"]}
    broad_owner = _owner(selector="src/*.py")
    exact_owner = _owner(
        owner_id="other-owner",
        decision="Other decision",
        selector="src/other.py",
        guards=["registry-delegation-other-owner"],
    )
    shards = {
        "core-runtime.json": json.dumps({"version": 1, "owners": [broad_owner]}).encode("ascii"),
        "contracts-tooling.json": json.dumps({"version": 1, "owners": [exact_owner]}).encode(
            "ascii"
        ),
    }

    with pytest.raises(RegistryError, match="matches selectors from multiple owners"):
        _load_parts(json.dumps(index).encode("ascii"), shards, ["src/owner.py", "src/other.py"])


def test_overlapping_selectors_for_same_owner_are_allowed() -> None:
    """One owner may use broad and exact selectors for its own tracked files."""
    owner = _owner()
    owner["selectors"] = ["src/*.py", "src/owner.py"]
    index, _, inventory = _documents(owner)
    shards = {"core-runtime.json": json.dumps({"version": 1, "owners": [owner]}).encode("ascii")}

    registry = _load_parts(index, shards, inventory)

    assert registry.owners[0].selectors == ("src/*.py", "src/owner.py")


@pytest.mark.parametrize(
    "selector",
    [
        "/src/owner.py",
        "./src/owner.py",
        "src/../owner.py",
        "src\\owner.py",
        "src//owner.py",
        "src/",
    ],
)
def test_unsafe_posix_selectors_fail_closed(selector: str) -> None:
    """Selectors cannot escape, alias, or use platform-specific separators."""
    index, shards, inventory = _documents(_owner(selector=selector))

    with pytest.raises(RegistryError, match=r"owner selector|POSIX|repository-relative"):
        _load_parts(index, shards, inventory)


def test_selector_must_match_exact_supplied_inventory() -> None:
    """A stale selector cannot silently make an owner unreachable."""
    index, shards, _ = _documents()

    with pytest.raises(RegistryError, match="matches no supplied file"):
        _load_parts(index, shards, ["src/different.py"])


@pytest.mark.parametrize(
    ("shards", "diagnostic"),
    [
        ({}, "missing registry shards"),
        (
            {
                "core-runtime.json": _documents()[1]["core-runtime.json"],
                "unlisted.json": _documents()[1]["core-runtime.json"],
            },
            "unlisted registry shards",
        ),
    ],
)
def test_missing_and_unlisted_shards_fail_closed(
    shards: dict[str, bytes],
    diagnostic: str,
) -> None:
    """The index is the exact shard allow-list."""
    index, _, inventory = _documents()

    with pytest.raises(RegistryError, match=diagnostic):
        _load_parts(index, shards, inventory)


def test_filesystem_loader_rejects_nested_unlisted_json(tmp_path: Path) -> None:
    """Unlisted JSON cannot hide below a nested registry directory."""
    registry_dir = tmp_path / "owners"
    nested = registry_dir / "nested"
    nested.mkdir(parents=True)
    index, shards, inventory = _documents()
    (registry_dir / "index.json").write_bytes(index)
    (registry_dir / "core-runtime.json").write_bytes(shards["core-runtime.json"])
    (nested / "hidden.json").write_bytes(shards["core-runtime.json"])

    with pytest.raises(RegistryError, match="unlisted registry shards"):
        load_registry(registry_dir, inventory)


def test_filesystem_loader_rejects_partial_registry_without_index(tmp_path: Path) -> None:
    """Any owner artifact prevents an absent index from looking like legacy state."""
    registry_dir = tmp_path / "owners"
    registry_dir.mkdir()
    _, shards, inventory = _documents()
    (registry_dir / "core-runtime.json").write_bytes(shards["core-runtime.json"])

    with pytest.raises(RegistryError, match=r"registry artifacts exist without index\.json"):
        load_registry(registry_dir, inventory)


def test_filesystem_loader_rejects_unlisted_json_case_insensitively(
    tmp_path: Path,
) -> None:
    """A case-variant JSON suffix cannot hide an unexpected registry document."""
    registry_dir = tmp_path / "owners"
    registry_dir.mkdir()
    index, shards, inventory = _documents()
    (registry_dir / "index.json").write_bytes(index)
    (registry_dir / "core-runtime.json").write_bytes(shards["core-runtime.json"])
    (registry_dir / "hidden.JSON").write_bytes(shards["core-runtime.json"])

    with pytest.raises(RegistryError, match="unlisted registry shards"):
        load_registry(registry_dir, inventory)


def test_duplicate_json_fields_fail_closed() -> None:
    """JSON parser behavior cannot silently choose one duplicate field."""
    _, shards, inventory = _documents()
    index = b'{"version":1,"version":1,"shards":["core-runtime.json"]}'

    with pytest.raises(RegistryError, match="duplicate JSON field"):
        _load_parts(index, shards, inventory)


def test_semantic_hash_ignores_document_and_array_order() -> None:
    """Formatting, shard order, owner order, selectors, and guards normalize."""
    first_owner = _owner()
    first_owner["selectors"] = ["src/owner.py", "src/shared.py"]
    first_owner["guards"] = ["registry-delegation-z", "registry-delegation-a"]
    second_owner = _owner(
        owner_id="second-owner",
        decision="Second decision",
        selector="src/second.py",
    )
    first_index = {"version": 1, "shards": ["a.json", "b.json"]}
    first_shards = {
        "a.json": json.dumps({"version": 1, "owners": [first_owner]}).encode("ascii"),
        "b.json": json.dumps({"version": 1, "owners": [second_owner]}).encode("ascii"),
    }
    reversed_owner = deepcopy(first_owner)
    reversed_owner["selectors"].reverse()
    reversed_owner["guards"].reverse()
    second_index = b'{ "shards": [ "b.json", "a.json" ], "version": 1 }\n'
    second_shards = {
        "b.json": json.dumps(
            {"owners": [second_owner], "version": 1},
            indent=4,
        ).encode("ascii"),
        "a.json": json.dumps(
            {"owners": [reversed_owner], "version": 1},
            separators=(",", ":"),
        ).encode("ascii"),
    }
    inventory = ["src/owner.py", "src/shared.py", "src/second.py"]

    first = _load_parts(json.dumps(first_index).encode("ascii"), first_shards, inventory)
    second = _load_parts(second_index, second_shards, inventory)

    assert first.semantic_sha256 == second.semantic_sha256
    assert len(first.semantic_sha256) == 64


def test_reusable_and_standalone_registry_semantic_hashes_match() -> None:
    """Both consumers normalize equivalent registry semantics identically."""
    first_owner = _owner()
    first_owner["selectors"] = ["src/owner.py", "src/shared.py"]
    first_owner["guards"] = ["registry-delegation-z", "registry-delegation-a"]
    second_owner = _owner(
        owner_id="second-owner",
        decision="Second decision",
        selector="src/second.py",
        guards=["registry-delegation-second-owner"],
    )
    index = json.dumps({"version": 1, "shards": ["a.json", "b.json"]}).encode("ascii")
    shards = {
        "a.json": json.dumps({"version": 1, "owners": [first_owner]}).encode("ascii"),
        "b.json": json.dumps({"version": 1, "owners": [second_owner]}).encode("ascii"),
    }
    inventory = ["src/owner.py", "src/shared.py", "src/second.py"]
    reusable = load_registry_documents(index, shards, inventory)
    spec = importlib.util.spec_from_file_location("owner_touch_gate_contract", OWNER_GATE)
    assert spec is not None and spec.loader is not None
    standalone = ModuleType(spec.name)
    sys.modules[spec.name] = standalone
    spec.loader.exec_module(standalone)

    _, standalone_hash = standalone._parse_owner_registry(index, shards, inventory)

    assert standalone_hash == reusable.semantic_sha256


def test_optional_guard_registry_validation_is_bidirectional() -> None:
    """A later runner can reject unknown and unreferenced owner-specific guards."""
    index, shards, inventory = _documents()

    with pytest.raises(RegistryError, match="unknown owner guard IDs"):
        load_registry_documents(
            index,
            shards,
            inventory,
            known_guard_ids={"different-guard"},
        )
    with pytest.raises(RegistryError, match="owner-specific guard IDs are not referenced"):
        load_registry_documents(
            index,
            shards,
            inventory,
            known_guard_ids={"registry-delegation-fixture-owner"},
            owner_specific_guard_ids={"unreferenced-owner-guard"},
        )


_BLAST_SELECTOR = "src/apm_cli/owner.py"
_BLAST_INVENTORY = [_BLAST_SELECTOR, "src/apm_cli/other.py", "tests/integration/test_owner.py"]
_LEDGER = json.dumps({"property_catalog": [{"id": "ownership.preserve_unowned"}]}).encode("ascii")


def _blast_owner(**fields: Any) -> dict[str, Any]:
    owner = _owner(selector=_BLAST_SELECTOR)
    owner.update(
        {
            "families": ["tests/integration/test_owner*.py"],
            "state": ["deployed-files", "lockfile"],
            "promises": ["P-INV-I1", "P-LAW-ownership.preserve_unowned", "P-OWN-fixture-owner"],
            "modules": ["src/apm_cli/owner.py", "src/apm_cli/other.py"],
        }
    )
    owner.update(fields)
    return owner


def _load_blast(
    owner: dict[str, Any],
    *,
    catalog: dict[str, Any] | None = None,
    inventory: list[str] | None = None,
) -> Any:
    index = json.dumps({"version": 1, "shards": ["core-runtime.json"]}).encode("ascii")
    shard = json.dumps({"version": 1, "owners": [owner]}).encode("ascii")
    vocabulary = promise_vocabulary(
        json.dumps(catalog).encode("ascii") if catalog is not None else None, _LEDGER
    )
    return load_registry_documents(
        index,
        {"core-runtime.json": shard},
        inventory or _BLAST_INVENTORY,
        promise_ids=vocabulary,
    )


def test_blast_radius_overlay_loads_without_changing_ownership_semantics() -> None:
    """The overlay is exposed on the record but never shifts the ownership hash."""
    with_overlay = _load_blast(_blast_owner())
    plain = load_registry_documents(
        json.dumps({"version": 1, "shards": ["core-runtime.json"]}).encode("ascii"),
        {
            "core-runtime.json": json.dumps(
                {"version": 1, "owners": [_owner(selector=_BLAST_SELECTOR)]}
            ).encode("ascii")
        },
        _BLAST_INVENTORY,
    )

    owner = with_overlay.owners[0]
    assert owner.families == ("tests/integration/test_owner*.py",)
    assert owner.state == ("deployed-files", "lockfile")
    assert owner.promises[0] == "P-INV-I1"
    assert owner.modules == ("src/apm_cli/owner.py", "src/apm_cli/other.py")
    assert plain.owners[0].modules == ()
    assert with_overlay.semantic_sha256 == plain.semantic_sha256


@pytest.mark.parametrize("dropped", ["families", "state", "promises", "modules"])
def test_blast_radius_fields_are_all_or_none(dropped: str) -> None:
    owner = _blast_owner()
    del owner[dropped]

    with pytest.raises(RegistryError, match=f"all-or-none; missing: {dropped}"):
        _load_blast(owner)


@pytest.mark.parametrize(
    ("fields", "diagnostic"),
    [
        ({"state": ["lockfile", "disk"]}, "state has unknown values: disk"),
        ({"state": ["lockfile", "deployed-files"]}, "state must be sorted"),
        ({"state": []}, "state must be a non-empty array"),
        ({"promises": ["I1"]}, "promises has unknown values: I1"),
        ({"promises": ["P-INV-I10"]}, "promises has unknown values: P-INV-I10"),
        ({"promises": ["P-INV-I0"]}, "promises has unknown values: P-INV-I0"),
        ({"promises": ["P-LAW-ownership"]}, "promises has unknown values: P-LAW-ownership"),
        ({"promises": ["req-cf-001"]}, "promises has unknown values: req-cf-001"),
        ({"promises": ["P-INV-I4", "P-INV-I1"]}, "promises must be sorted"),
        ({"promises": ["P-LAW-cache.not_in_ledger"]}, "uncatalogued promise"),
        ({"promises": ["P-OWN-unregistered-owner"]}, "names an unknown owner"),
    ],
)
def test_blast_radius_vocabulary_is_closed(fields: dict[str, Any], diagnostic: str) -> None:
    with pytest.raises(RegistryError, match=diagnostic):
        _load_blast(_blast_owner(**fields))


def test_promise_catalog_wins_over_the_ledger_fallback() -> None:
    """With the catalog present, only its ids count; ledger laws and owners are not implied."""
    catalog = {"promises": {"P-INV-I1": "x", "P-OWN-fixture-owner": "y"}}

    with pytest.raises(RegistryError, match="uncatalogued promise: P-LAW-ownership"):
        _load_blast(_blast_owner(), catalog=catalog)
    with pytest.raises(RegistryError, match="uncatalogued promise: P-OWN-fixture-owner"):
        _load_blast(_blast_owner(promises=["P-OWN-fixture-owner"]), catalog={"promises": {}})
    loaded = _load_blast(
        _blast_owner(promises=["P-INV-I1", "P-OWN-fixture-owner"]), catalog=catalog
    )
    assert loaded.owners[0].promises == ("P-INV-I1", "P-OWN-fixture-owner")


@pytest.mark.parametrize(
    ("fields", "diagnostic"),
    [
        ({"modules": ["src/other/owner.py"]}, "modules entries must start with src/apm_cli/"),
        ({"modules": ["tests/integration/test_owner.py"]}, "must start with src/apm_cli/"),
        ({"modules": ["src/apm_cli/../owner.py"]}, "unsafe POSIX owner selector"),
        ({"modules": [_BLAST_SELECTOR, "src/apm_cli/gone/*"]}, "matches no supplied file"),
        ({"modules": ["src/apm_cli/other.py"]}, "must cover its own selector file"),
        ({"families": ["src/apm_cli/owner.py"]}, "must start with tests/integration/"),
        ({"families": ["tests/integration/test_gone*.py"]}, "matches no test module"),
    ],
)
def test_blast_radius_globs_are_machine_paths_that_resolve(
    fields: dict[str, Any], diagnostic: str
) -> None:
    with pytest.raises(RegistryError, match=diagnostic):
        _load_blast(_blast_owner(**fields))


def test_owner_touch_gate_tolerates_the_blast_radius_overlay() -> None:
    """The standalone merge gate accepts the overlay and agrees on the ownership hash."""
    index = json.dumps({"version": 1, "shards": ["a.json"]}).encode("ascii")
    shards = {"a.json": json.dumps({"version": 1, "owners": [_blast_owner()]}).encode("ascii")}
    reusable = load_registry_documents(index, shards, _BLAST_INVENTORY)
    spec = importlib.util.spec_from_file_location("owner_touch_gate_overlay", OWNER_GATE)
    assert spec is not None and spec.loader is not None
    standalone = ModuleType(spec.name)
    sys.modules[spec.name] = standalone
    spec.loader.exec_module(standalone)

    _, standalone_hash = standalone._parse_owner_registry(index, shards, _BLAST_INVENTORY)

    assert standalone_hash == reusable.semantic_sha256
