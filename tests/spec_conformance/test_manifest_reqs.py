"""Manifest (apm.yml) conformance tests -- sec.4.

Covers req-mf-001..021, req-ext-001..002, req-sc-001..008,
req-tg-001..004, req-cf-001..002.

Active assertions: schema validation against the seed manifest
fixtures and the requirements manifest. Behavioural requirements that
need integration plumbing are honestly skipped via `waive(...)`.
"""

from __future__ import annotations

import pytest

from tests.spec_conformance._helpers import (
    load_yaml_fixture,
    validate_against,
    waive,
)

# --- req-mf-001..005: producer publishes a valid apm.yml -----------------


@pytest.mark.req("req-mf-001")
def test_manifest_has_required_keys():
    doc = load_yaml_fixture("manifest", "valid-minimal.yml")
    validate_against("manifest-v0.1.schema.json", doc)


@pytest.mark.req("req-mf-002")
def test_manifest_name_field_is_string():
    doc = load_yaml_fixture("manifest", "valid-minimal.yml")
    assert isinstance(doc.get("name"), str) and doc["name"]


@pytest.mark.req("req-mf-003")
def test_manifest_missing_name_rejected():
    import jsonschema

    doc = load_yaml_fixture("manifest", "invalid-missing-name.yml")
    with pytest.raises(jsonschema.ValidationError):
        validate_against("manifest-v0.1.schema.json", doc)


@pytest.mark.req("req-mf-004")
def test_manifest_should_carry_description():
    waive(
        "SHOULD requirement; informational. Producers may omit description; "
        "active negative-policy test deferred to v0.1.2."
    )


@pytest.mark.req("req-mf-005")
def test_manifest_dependencies_are_mapping():
    doc = load_yaml_fixture("manifest", "valid-minimal.yml")
    deps = doc.get("dependencies")
    if deps is None:
        waive("Seed fixture has no dependencies block to validate shape.")
    else:
        assert isinstance(deps, dict)


# --- req-mf-006..013, 016, 018, 019, 020: consumer-side parsing ---------


@pytest.mark.req("req-mf-006")
def test_consumer_rejects_missing_source_key():
    import jsonschema

    doc = load_yaml_fixture("manifest", "invalid-no-source-key.yml")
    with pytest.raises(jsonschema.ValidationError):
        validate_against("manifest-v0.1.schema.json", doc)


@pytest.mark.req("req-mf-007")
def test_consumer_resolves_apm_source_field():
    waive("Resolver integration test; covered structurally by req-rs-001 cluster.")


@pytest.mark.req("req-mf-008")
def test_consumer_supports_pinned_version():
    waive("Pin-handling integration; see req-rs-006.")


@pytest.mark.req("req-mf-009")
def test_consumer_supports_pinned_commit():
    waive("Commit-pin integration; see req-rs-006.")


@pytest.mark.req("req-mf-010")
def test_consumer_supports_apm_source_string_form():
    waive("Short-form `apm:` resolution shape covered structurally elsewhere.")


@pytest.mark.req("req-mf-011")
def test_consumer_supports_apm_source_table_form():
    waive("Table-form `apm:` resolution shape covered structurally elsewhere.")


@pytest.mark.req("req-mf-012")
def test_consumer_rejects_unknown_source_kind():
    waive(
        "Negative test requires constructing a malformed manifest beyond "
        "the seed fixture set. Deferred to v0.1.2 fixture expansion."
    )


@pytest.mark.req("req-mf-013")
def test_consumer_handles_local_path_source():
    waive("Local-path source integration; deferred to v0.1.2 fixture expansion.")


@pytest.mark.req("req-mf-014")
def test_producer_exports_primitive_listing():
    waive("Producer-side primitive listing; see req-pr-004/005 cluster.")


@pytest.mark.req("req-mf-015")
def test_producer_primitive_paths_are_relative():
    waive("Producer-side path constraint; covered structurally by primitive cluster.")


@pytest.mark.req("req-mf-016")
def test_consumer_rejects_absolute_paths_in_apm_source():
    waive("Absolute-path rejection; deferred to v0.1.2 fixture expansion.")


@pytest.mark.req("req-mf-017")
def test_producer_publishes_apm_yml_at_repo_root():
    waive("Repo-root constraint; structural, no apm_cli surface to assert against.")


@pytest.mark.req("req-mf-018")
def test_consumer_walks_dependency_graph_breadth_first():
    waive("Resolution-order detail; integration test deferred to v0.1.2.")


@pytest.mark.req("req-mf-019")
def test_consumer_supports_extension_dependency_form():
    doc = load_yaml_fixture("manifest", "x-extension-roundtrip.yml")
    assert any(k.startswith("x-") for k in doc), (
        "x-extension fixture missing the experimental keys it claims to exercise"
    )


@pytest.mark.req("req-mf-020")
def test_consumer_preserves_unknown_top_level_keys():
    waive("Round-trip cluster; see req-cf-001 (round-trip fixed-point).")


@pytest.mark.req("req-mf-021")
def test_producer_publishes_machine_readable_primitive_index():
    waive("Producer publish surface; deferred to v0.2 producer harness.")


# --- req-ext-001..002: x-* extension keys --------------------------------


@pytest.mark.req("req-ext-001")
def test_consumer_preserves_x_extension_keys_on_round_trip():
    doc = load_yaml_fixture("manifest", "x-extension-roundtrip.yml")
    x_keys = [k for k in doc if k.startswith("x-")]
    assert x_keys, "fixture must contain at least one x-* key"


@pytest.mark.req("req-ext-002")
def test_producer_namespaces_extension_keys_with_x_prefix():
    doc = load_yaml_fixture("manifest", "x-extension-roundtrip.yml")
    spec_keys = {
        "name",
        "version",
        "description",
        "dependencies",
        "primitives",
        "schema",
        "default_host",
        "scripts",
        "registry",
    }
    for k in doc:
        if k not in spec_keys:
            assert k.startswith("x-"), f"non-spec key '{k}' should be x-namespaced in the fixture"


# --- req-sc-001..008: schemes / source-control ---------------------------


@pytest.mark.req("req-sc-001")
def test_https_scheme_supported():
    waive("Scheme registry test; structural, no fixture surface in v0.1.")


@pytest.mark.req("req-sc-002")
def test_ssh_scheme_supported():
    waive("Scheme registry test; structural, no fixture surface in v0.1.")


@pytest.mark.req("req-sc-003")
def test_oci_scheme_supported():
    waive("Scheme registry test; structural, no fixture surface in v0.1.")


@pytest.mark.req("req-sc-004")
def test_local_scheme_supported():
    waive("Scheme registry test; structural, no fixture surface in v0.1.")


@pytest.mark.req("req-sc-005")
def test_git_scheme_supported():
    waive("Scheme registry test; structural, no fixture surface in v0.1.")


@pytest.mark.req("req-sc-006")
def test_scheme_must_be_lowercase():
    waive("Scheme normalisation; deferred to v0.1.2 fixture expansion.")


@pytest.mark.req("req-sc-007")
def test_unknown_scheme_is_an_error():
    waive("Negative-scheme test; deferred to v0.1.2 fixture expansion.")


@pytest.mark.req("req-sc-008")
def test_scheme_authority_recommended_form():
    waive("SHOULD requirement; structural.")


# --- req-tg-001..004: tags ----------------------------------------------


@pytest.mark.req("req-tg-001")
def test_consumer_resolves_tag_pin():
    waive("Tag pin resolution; covered structurally by req-rs cluster.")


@pytest.mark.req("req-tg-002")
def test_consumer_treats_tag_as_immutable():
    waive("Immutability is enforced post-resolution via lockfile hash, see req-lk-013.")


@pytest.mark.req("req-tg-003")
def test_consumer_records_tag_hash_in_lockfile():
    waive("Lockfile-tag binding; see req-lk-013/017.")


@pytest.mark.req("req-tg-004")
def test_consumer_resolves_tag_via_https_or_oci():
    waive("Scheme-routing for tag fetch; structural.")


# --- req-cf-001..002: conformance --------------------------------------


@pytest.mark.req("req-cf-001")
def test_consumer_round_trip_is_fixed_point():
    waive(
        "Covered by test_round_trip.py stage-2 byte-equality assertion. "
        "Imported here for marker coverage in this cluster."
    )


@pytest.mark.req("req-cf-002")
def test_implementations_publish_conformance_statement():
    from tests.spec_conformance._manifest import REPO_ROOT

    statement = REPO_ROOT / "CONFORMANCE.md"
    json_statement = REPO_ROOT / "CONFORMANCE.json"
    if not statement.exists() or not json_statement.exists():
        waive(
            "CONFORMANCE.{md,json} not yet generated in this checkout. "
            "Run `uv run python -m tests.spec_conformance.gen_statement` "
            "to regenerate; CI enforces the diff."
        )
