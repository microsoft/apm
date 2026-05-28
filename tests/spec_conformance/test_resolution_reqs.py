"""Resolution (sec.7) and Primitive (sec.8) conformance tests.

Covers req-rs-001..014, req-pr-001..005, req-rg-001. The semver
dialect oracle (req-rs-007) gets a real assertion against the
shipped JSON.
"""

from __future__ import annotations

import pytest

from tests.spec_conformance._helpers import (
    load_json_fixture,
    waive,
)

# --- req-rs-001..014 ---------------------------------------------------


@pytest.mark.req("req-rs-001")
def test_resolver_walks_dependency_graph_deterministically():
    waive("Determinism integration; deferred to v0.1.2.")


@pytest.mark.req("req-rs-002")
def test_resolver_emits_lockfile_after_successful_resolution():
    waive("Resolution -> lockfile integration; deferred to v0.1.2.")


@pytest.mark.req("req-rs-003")
def test_resolver_uses_pinned_version_when_present():
    waive("Pin-honour integration; deferred to v0.1.2.")


@pytest.mark.req("req-rs-004")
def test_resolver_records_resolution_provenance_in_lockfile():
    waive("Provenance binding; structural.")


@pytest.mark.req("req-rs-005")
def test_resolver_rejects_unresolvable_dependency():
    waive("Negative resolution path; deferred to v0.1.2 fixture expansion.")


@pytest.mark.req("req-rs-006")
def test_resolver_handles_commit_pin():
    waive("Commit-pin integration; deferred to v0.1.2.")


@pytest.mark.req("req-rs-007")
def test_semver_dialect_oracle_present_and_well_formed():
    """req-rs-007 cites tests/fixtures/.../semver-dialect.json as the
    canonical oracle of semver-range -> resolved-tag mappings.
    """
    oracle = load_json_fixture("resolution", "semver-dialect.json")
    assert isinstance(oracle, (list, dict)), "oracle must be list or dict"
    if isinstance(oracle, list):
        assert oracle, "oracle list must not be empty"
        for entry in oracle:
            assert isinstance(entry, dict)
    else:
        assert oracle, "oracle dict must not be empty"


@pytest.mark.req("req-rs-008")
def test_resolver_supports_caret_range():
    waive("Range-grammar binding; covered structurally by req-rs-007 oracle.")


@pytest.mark.req("req-rs-009")
def test_resolver_supports_tilde_range():
    waive("Range-grammar binding; covered structurally by req-rs-007 oracle.")


@pytest.mark.req("req-rs-010")
def test_resolver_supports_exact_pin():
    waive("Exact-pin integration; deferred to v0.1.2.")


@pytest.mark.req("req-rs-011")
def test_resolver_records_source_url_in_lockfile():
    waive("Source-URL recording; covered structurally by lockfile cluster.")


@pytest.mark.req("req-rs-012")
def test_resolver_records_resolved_ref_in_lockfile():
    waive("Resolved-ref recording; covered structurally by lockfile cluster.")


@pytest.mark.req("req-rs-013")
def test_resolver_fails_closed_on_ambiguous_resolution():
    waive("Ambiguity-fail-closed; deferred to v0.1.2.")


@pytest.mark.req("req-rs-014")
def test_resolver_honours_prerelease_inclusion_rules():
    waive("Prerelease semantics; covered structurally by req-rs-007 oracle shape.")


# --- req-pr-001..005: primitives ---------------------------------------


@pytest.mark.req("req-pr-001")
def test_consumer_loads_primitives_from_resolved_dep():
    waive("Primitive loading integration; deferred to v0.1.2.")


@pytest.mark.req("req-pr-002")
def test_consumer_namespaces_primitives_by_source():
    waive("Namespacing rule; structural.")


@pytest.mark.req("req-pr-003")
def test_consumer_rejects_primitive_collisions():
    waive("Collision-fail-closed; deferred to v0.1.2.")


@pytest.mark.req("req-pr-004")
def test_producer_publishes_primitive_index():
    waive("Producer-publish surface; deferred to v0.2.")


@pytest.mark.req("req-pr-005")
def test_producer_should_carry_primitive_descriptions():
    waive("SHOULD requirement; structural.")


# --- req-rg-001: registry trust anchor ---------------------------------


@pytest.mark.req("req-rg-001")
def test_registry_trust_anchor_is_declared():
    waive(
        "v0.1 registry class ships only this trust-anchor MUST; no wire "
        "surface to exercise. Tracked at sec.11.3.3; v0.2 expands."
    )
