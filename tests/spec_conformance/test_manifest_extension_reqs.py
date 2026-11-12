"""req-ext-003: preserve top-level x-* keys and warn on other unknown keys.

Behavioral coverage also lives in tests/unit/test_manifest_extension_keys.py.
This module is the spec-conformance bind (column-0 req marker).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache
from tests.spec_conformance._helpers import assert_spec_contains, load_schema

_WARN = "apm_cli.utils.console._rich_warning"


@pytest.mark.req("req-ext-003")
def test_parser_keeps_x_extensions_and_warns_without_failing(tmp_path: Path) -> None:
    """req-ext-003: x-* values are kept as declared; other unknown keys warn."""
    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        "name: sample\nversion: 1.0.0\nx-zeta: 7\nx-alpha:\n  nested: keep\nmystery: 1\n",
        encoding="utf-8",
    )
    clear_apm_yml_cache()

    with patch(_WARN) as warned:
        package = APMPackage.from_apm_yml(manifest, create_config=False)

    assert package.name == "sample"
    assert list(package.extensions) == ["x-zeta", "x-alpha"]
    assert package.extensions["x-zeta"] == 7
    assert package.extensions["x-alpha"] == {"nested": "keep"}
    warned.assert_called_once()
    message = warned.call_args[0][0]
    assert "mystery" in message
    assert "x-zeta" not in message
    assert "x-alpha" not in message


@pytest.mark.req("req-ext-003")
def test_lock_entry_records_extensions_and_omits_when_empty() -> None:
    """req-ext-003: extensions is recorded, ordered, and omitted when empty."""
    recorded = LockedDependency(
        repo_url="owner/sample",
        extensions={"x-zeta": 7, "x-alpha": {"nested": "keep"}},
    )
    rendered = LockFile(dependencies={"owner/sample": recorded}).to_yaml()
    assert rendered.index("extensions:") < rendered.index("x-zeta:")
    assert rendered.index("x-zeta:") < rendered.index("x-alpha:")
    restored = LockFile.from_yaml(rendered).get_package_dependencies()[0]
    assert list(restored.extensions) == ["x-zeta", "x-alpha"]
    assert restored.extensions["x-alpha"] == {"nested": "keep"}

    empty = LockedDependency(repo_url="owner/sample")
    assert "extensions" not in empty.to_dict()
    empty_yaml = LockFile(dependencies={"owner/sample": empty}).to_yaml()
    assert "extensions:" not in empty_yaml

    entry_props = load_schema("lockfile-v0.1.46.schema.json")["$defs"]["entry"]["properties"]
    assert entry_props["extensions"]["type"] == "object"


@pytest.mark.req("req-ext-003")
def test_req_ext_003_keeps_rewrite_preservation() -> None:
    """req-ext-003 must not contradict the writer preserve-unknown-keys rule."""
    assert_spec_contains(
        "name matches `x-[a-z][a-z0-9-]*`",
        "optional `extensions`",
        "non-failing warning",
        "does not relax",
        "[req-mf-006](#req-mf-006)",
    )


@pytest.mark.req("req-ext-003")
@pytest.mark.parametrize("key", ["x-", "x-Foo", "x-a_b", "unknown", "x-valid\n"])
def test_extension_schema_checks_names_not_values(key: str) -> None:
    """The distinct schema validates vendor names without interpreting values."""
    validator = Draft202012Validator(load_schema("lockfile-v0.1.46.schema.json"))
    lock = {
        "lockfile_version": "2",
        "dependencies": [{"repo_url": "owner/sample", "extensions": {"x-valid": [None, {"k": 1}]}}],
    }
    assert list(validator.iter_errors(lock)) == []
    lock["dependencies"][0]["extensions"] = {key: "opaque"}
    assert list(validator.iter_errors(lock))


@pytest.mark.req("req-ext-003")
def test_extension_schema_preserves_previous_contract() -> None:
    """The .46 resource changes only its identity and optional extensions field."""
    previous = load_schema("lockfile-v0.1.41.schema.json")
    proposed = load_schema("lockfile-v0.1.46.schema.json")
    assert proposed["$id"] != previous["$id"]
    assert proposed["title"] != previous["title"]
    assert "extensions" not in previous["$defs"]["entry"]["properties"]
    extensions = proposed["$defs"]["entry"]["properties"].pop("extensions")
    assert extensions["type"] == "object"
    proposed["$id"] = previous["$id"]
    proposed["title"] = previous["title"]
    assert proposed == previous
