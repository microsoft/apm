"""Behavioral and offline-schema evidence for the proposed replacement capability."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.plan import lockfile_satisfies_replacements
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.replacement import build_replacement_index, parse_replace_block
from apm_cli.models.manifest_contract import SOURCE_REPLACEMENTS_V1_SCHEMA_URI
from apm_cli.policy.policy_checks import _check_dependency_allowlist, _check_dependency_denylist
from apm_cli.policy.schema import DependencyPolicy
from tests.spec_conformance._helpers import load_schema

pytestmark = pytest.mark.component


def _validator(kind: str) -> Draft202012Validator:
    names = [
        "manifest-v0.1.41.schema.json",
        "manifest-v0.1.48.schema.json",
        "lockfile-v0.1.46.schema.json",
        "manifest-source-replacements-v1.schema.json",
        "lockfile-source-replacements-v1.schema.json",
    ]
    schemas = {name: load_schema(name) for name in names}
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )
    schema = schemas[f"{kind}-source-replacements-v1.schema.json"]
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=registry)


def _manifest(source: dict) -> dict:
    return {
        "$schema": SOURCE_REPLACEMENTS_V1_SCHEMA_URI,
        "name": "consumer",
        "version": "1.0.0",
        "replace": [{"for": {"git": "https://github.com/owner/original"}, "with": source}],
    }


@pytest.mark.req("req-rpl-001")
def test_replacement_schema_preserves_git_subpath_dependencies(tmp_path: Path) -> None:
    manifest = _manifest({"path": "../source"})
    manifest["replace"][0]["for"]["path"] = "packages/base"
    manifest["dependencies"] = {
        "apm": [{"git": "https://github.com/owner/original", "path": "packages/base"}]
    }
    errors = list(_validator("manifest").iter_errors(manifest))
    assert not errors, [error.message for error in errors]
    package = APMPackage.from_mapping(manifest, package_path=tmp_path, create_config=False)
    dependency = package.get_apm_dependencies()[0]
    rule = build_replacement_index(package.replace_rules).lookup(dependency)
    assert dependency.virtual_path == "packages/base"
    assert rule is not None and rule.selector.subpath == "packages/base"


@pytest.mark.req("req-rpl-001")
@pytest.mark.parametrize(
    "source", [{"path": "../source"}, {"git": "https://gitlab.com/team/fork", "ref": "fix"}]
)
def test_explicit_contract_binds_each_original_request_once(tmp_path: Path, source: dict) -> None:
    manifest = _manifest(source)
    _validator("manifest").validate(manifest)
    package = APMPackage.from_mapping(manifest, package_path=tmp_path)
    index = build_replacement_index(package.replace_rules)
    edges = [
        DependencyReference.parse(f"https://github.com/owner/original#{ref}")
        for ref in ("left", "right")
    ]
    selected = [
        index.bind(
            edge,
            consumer_root=tmp_path,
            modules_dir=tmp_path / "apm_modules",
            parent=ref,
            is_dev=False,
        )
        for edge, ref in zip(edges, ("parent/left", "parent/right"), strict=True)
    ]
    binding = selected[0].replacement
    assert binding is not None and selected[1].replacement is binding
    assert {request.ref for request in binding.requests} == {"left", "right"}
    assert index.lookup(selected[0]) is None
    assert edges[0].replacement is None
    assert selected[0].get_install_path(tmp_path / "apm_modules") == edges[0].get_install_path(
        tmp_path / "apm_modules"
    )
    manifest.pop("$schema")
    with pytest.raises(ValueError, match=r"replace"):
        APMPackage.from_mapping(manifest, package_path=tmp_path)


@pytest.mark.req("req-rpl-001")
@pytest.mark.parametrize(
    "source",
    [
        {"git": "owner/fork", "ref": "fix"},
        {"git": "http://gitlab.com/team/fork", "ref": "fix"},
        {"git": "https://gitlab.com/team/fork"},
        {"path": "../source", "ref": "fix"},
    ],
)
def test_invalid_selected_source_fails_schema_and_consumer(tmp_path: Path, source: dict) -> None:
    manifest = _manifest(source)
    with pytest.raises(ValidationError):
        _validator("manifest").validate(manifest)
    with pytest.raises(ValueError):
        APMPackage.from_mapping(manifest, package_path=tmp_path)


@pytest.mark.req("req-rpl-002", "req-rpl-003")
@pytest.mark.parametrize(
    "source", [{"path": "../source"}, {"git": "https://gitlab.com/team/fork", "ref": "a" * 40}]
)
def test_selected_source_policy_and_original_ownership_survive_lock_replay(
    tmp_path: Path, source: dict
) -> None:
    rules = parse_replace_block(_manifest(source)["replace"])
    original = DependencyReference.parse("https://github.com/owner/original#upstream")
    selected = build_replacement_index(rules).bind(
        original,
        consumer_root=tmp_path,
        modules_dir=tmp_path / "apm_modules",
        parent=None,
        is_dev=False,
    )
    locked = LockedDependency.from_dependency_ref(
        selected, None if selected.is_local else "a" * 40, 1, None
    )
    lock = LockFile(replacements=_manifest(source)["replace"])
    lock.add_dependency(locked)
    data = yaml.safe_load(lock.to_yaml())
    _validator("lockfile").validate(data)
    assert data["lockfile_version"] == "3"
    restored = LockFile.from_yaml(lock.to_yaml()).get_dependency("owner/original")
    assert restored is not None
    replay = restored.to_dependency_ref()
    assert replay.get_unique_key() == original.get_unique_key()
    assert (
        replay.source_reference.get_canonical_dependency_string()
        == selected.get_canonical_dependency_string()
    )
    assert replay.reference == (None if selected.is_local else "a" * 40)
    policy = DependencyPolicy(deny=[selected.get_canonical_dependency_string()])
    assert not _check_dependency_denylist([replay], policy).passed
    assert not _check_dependency_allowlist(
        [replay], DependencyPolicy(allow=[original.get_canonical_dependency_string()])
    ).passed
    corrupt = deepcopy(data)
    corrupt["dependencies"][0]["replacement"]["requests"] = []
    with pytest.raises(ValidationError):
        _validator("lockfile").validate(corrupt)
    with pytest.raises(ValueError):
        LockFile.from_yaml(yaml.safe_dump(corrupt))


@pytest.mark.req("req-rpl-003", "req-rpl-004")
def test_empty_and_dormant_tables_are_not_absent_or_implicitly_accepted() -> None:
    empty = LockFile(replacements=[])
    assert yaml.safe_load(empty.to_yaml())["lockfile_version"] == "3"
    assert lockfile_satisfies_replacements(empty, [])[0]
    assert not lockfile_satisfies_replacements(empty, None)[0]
    assert not lockfile_satisfies_replacements(LockFile(), [])[0]
    table = _manifest({"path": "../never-read"})["replace"]
    locked = LockFile(replacements=table)
    assert lockfile_satisfies_replacements(locked, parse_replace_block(table))[0]
    changed = _manifest({"path": "../different-never-read"})["replace"]
    accepted, reasons = lockfile_satisfies_replacements(locked, parse_replace_block(changed))
    assert accepted is False
    assert reasons
