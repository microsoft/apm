"""Instance-level qualification of the corrected manifest schema candidate."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from jsonschema import Draft202012Validator

from apm_cli.models.apm_package import APMPackage
from apm_cli.models.manifest_contract import ManifestContract, negotiate_manifest_contract
from tests.spec_conformance._helpers import load_json_fixture, load_schema, sha256_hex

pytestmark = pytest.mark.component

SCHEMA_NAME = "manifest-v0.1.48.schema.json"
SCHEMA_URI = f"https://microsoft.github.io/apm/specs/schemas/{SCHEMA_NAME}"
CASES = load_json_fixture("manifest", "dependency-schema-cases.json")


@pytest.fixture
def registry_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the real existing opt-in in an isolated config, not a gate mock."""
    from apm_cli import config

    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "_config_cache", None)
    (tmp_path / "config.json").write_text(
        json.dumps({"experimental": {"registries": True}}), encoding="utf-8"
    )


@pytest.mark.parametrize("section", ["dependencies", "devDependencies"])
@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            case,
            id=case["name"],
            marks=pytest.mark.req(case["requirement"]) if "requirement" in case else (),
        )
        for case in CASES
    ],
)
def test_dependency_instances_match_schema_and_loader(
    tmp_path: Path, registry_opt_in: None, section: str, case: dict[str, Any]
) -> None:
    """Exercise source conflicts and MCP obligations, not a schema's syntax."""
    document = {
        "$schema": SCHEMA_URI,
        "name": "schema-repro",
        "version": "0.0.0",
        "target": "copilot",
        section: {case["kind"]: [copy.deepcopy(case["entry"])]},
    }
    validator = Draft202012Validator(load_schema(SCHEMA_NAME))
    errors = list(validator.iter_errors(document))
    if case["valid"]:
        assert not errors, [error.message for error in errors]
        package = APMPackage.from_mapping(document, package_path=tmp_path, create_config=False)
        parsed = package.dependencies if section == "dependencies" else package.dev_dependencies
        assert parsed is not None
        assert len(parsed[case["kind"]]) == 1
        assert package.manifest_contract == ManifestContract.OPENAPM_V01
    else:
        assert errors, case["name"]
        with pytest.raises(ValueError) as caught:
            APMPackage.from_mapping(document, package_path=tmp_path, create_config=False)
        assert case["diagnostic"] in str(caught.value)


@pytest.mark.parametrize(
    ("name", "digest"),
    [
        (
            "manifest-v0.1.schema.json",
            "7bdefbe443d3315d71add021c777d776c9cfd4942acb19750a799f46fa0d1344",
        ),
        (
            "manifest-v0.1.41.schema.json",
            "2ed2b189bffce737d0bf25737d6a5ed5070728b04efc8e12d90363a942cbaac5",
        ),
    ],
)
def test_both_old_manifest_resources_stay_immutable(
    repo_root: Path, name: str, digest: str
) -> None:
    """A correction cannot rewrite either already-public validator identity."""
    path = repo_root / "docs/public/specs/schemas" / name
    assert sha256_hex(path.read_text(encoding="utf-8").encode("utf-8")) == digest
    identity = urlparse(load_schema(name)["$id"])
    assert (identity.scheme, identity.hostname, identity.path) == (
        "https",
        "microsoft.github.io",
        f"/apm/specs/schemas/{name}",
    )


def test_new_resource_selects_existing_contract_without_changing_working_draft() -> None:
    """The new explicit opt-in belongs to the one negotiation owner."""
    schema = load_schema(SCHEMA_NAME)
    Draft202012Validator.check_schema(schema)
    identity = urlparse(schema["$id"])
    assert (identity.scheme, identity.hostname, identity.path) == (
        "https",
        "microsoft.github.io",
        f"/apm/specs/schemas/{SCHEMA_NAME}",
    )
    assert negotiate_manifest_contract({"$schema": schema["$id"]}) is ManifestContract.OPENAPM_V01
    assert negotiate_manifest_contract({}) is ManifestContract.WORKING_DRAFT
    assert negotiate_manifest_contract({"$schema": None}) is ManifestContract.WORKING_DRAFT


def test_schema_less_registry_shape_remains_working_draft(
    tmp_path: Path, registry_opt_in: None
) -> None:
    """Omission must not silently admit the explicit contract's scalar registries."""
    document = {
        "name": "working-draft",
        "version": "0.0.0",
        "registries": {"corp": "https://registry.example.invalid"},
    }
    with pytest.raises(ValueError, match="must be a mapping"):
        APMPackage.from_mapping(document, package_path=tmp_path, create_config=False)
    document["$schema"] = SCHEMA_URI
    package = APMPackage.from_mapping(document, package_path=tmp_path, create_config=False)
    assert package.registries == {"corp": "https://registry.example.invalid"}


@pytest.mark.parametrize("alias", [".safe", "safe.", "foo..bar", " \tMy.Safe\n"])
def test_corrected_schema_retains_alias_input_canonicalization(alias: str) -> None:
    """The correction preserves the alias-aware schema rather than regressing it."""
    document = {
        "name": "alias-repro",
        "version": "0.0.0",
        "dependencies": {"apm": [{"path": "../sibling", "alias": alias}]},
    }
    Draft202012Validator(load_schema(SCHEMA_NAME)).validate(document)


@pytest.mark.parametrize("alias", [".", "..", "../escape", "", 7])
def test_corrected_schema_retains_invalid_alias_rejection(alias: object) -> None:
    """New source combinations do not bypass the existing alias constraint."""
    document = {
        "name": "alias-repro",
        "version": "0.0.0",
        "dependencies": {"apm": [{"git": "acme/skills", "path": "skills/review", "alias": alias}]},
    }
    assert list(Draft202012Validator(load_schema(SCHEMA_NAME)).iter_errors(document))


def test_correction_preserves_unrelated_schema_contracts() -> None:
    """Targets, extensions, runtime declarations and policy are not broadened."""
    old = load_schema("manifest-v0.1.41.schema.json")
    new = load_schema(SCHEMA_NAME)
    assert new["properties"] == old["properties"]
    assert new["patternProperties"] == old["patternProperties"]
    assert new["not"] == old["not"]
    assert new["required"] == old["required"]
    assert new["additionalProperties"] == old["additionalProperties"]
    assert (
        new["$defs"]["depsBlock"]["properties"]["conflict_resolution"]
        == (old["$defs"]["depsBlock"]["properties"]["conflict_resolution"])
    )
