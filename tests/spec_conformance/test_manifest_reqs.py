"""Manifest (apm.yml) + scheme + tag + conformance-class tests.

Covers req-mf-001..021, req-ext-001..002, req-sc-001..010,
req-tg-001..005, req-cf-001..002.

Every requirement is exercised either by (a) schema validation
against shipped fixtures (positive + negative), (b) a verbatim
spec-text grep that detects silent deletion of normative language,
or (c) a real apm_cli loader call where the surface exists.
"""

from __future__ import annotations

import jsonschema
import pytest

from tests.spec_conformance._helpers import (
    assert_spec_contains,
    load_schema,
    load_yaml_fixture,
    validate_against,
    waive,
)

# --- req-mf-001..005: manifest shape, producer side ---------------------


@pytest.mark.req("req-mf-001")
def test_manifest_required_keys_enforced_by_schema():
    schema = load_schema("manifest-v0.1.schema.json")
    assert set(schema["required"]) == {"name", "version"}
    validate_against(
        "manifest-v0.1.schema.json", load_yaml_fixture("manifest", "valid-minimal.yml")
    )


@pytest.mark.req("req-mf-002")
def test_manifest_name_is_non_empty_string():
    schema = load_schema("manifest-v0.1.schema.json")
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["name"]["minLength"] == 1
    doc = load_yaml_fixture("manifest", "valid-minimal.yml")
    assert isinstance(doc["name"], str) and doc["name"]


@pytest.mark.req("req-mf-003")
def test_manifest_missing_name_rejected_by_schema():
    doc = load_yaml_fixture("manifest", "invalid-missing-name.yml")
    with pytest.raises(jsonschema.ValidationError):
        validate_against("manifest-v0.1.schema.json", doc)


@pytest.mark.req("req-mf-004")
def test_manifest_version_is_semver_2_0_0():
    schema = load_schema("manifest-v0.1.schema.json")
    pattern = schema["properties"]["version"]["pattern"]
    assert "0|[1-9]" in pattern, "version pattern must be semver 2.0.0 grammar"
    assert_spec_contains("semver 2.0.0", "version`")


@pytest.mark.req("req-mf-005")
def test_manifest_target_enum_is_pinned():
    """req-mf-005 says producer MUST reject unknown target values."""
    assert_spec_contains(
        "copilot, claude, cursor, codex, gemini, antigravity, opencode, windsurf, agent-skills, all"
    )
    assert_spec_contains("x-[a-z][a-z0-9-]*-[a-z][a-z0-9-]*")


# --- req-mf-006..013, 016..020: consumer parsing -----------------------


@pytest.mark.req("req-mf-006")
def test_consumer_rejects_missing_source_key():
    doc = load_yaml_fixture("manifest", "invalid-no-source-key.yml")
    with pytest.raises(jsonschema.ValidationError):
        validate_against("manifest-v0.1.schema.json", doc)


@pytest.mark.req("req-mf-007")
def test_consumer_apm_source_field_has_supported_shapes():
    schema = load_schema("manifest-v0.1.schema.json")
    entry = schema["$defs"]["depEntry"]
    one_of = entry["oneOf"]
    has_string = any(s.get("type") == "string" for s in one_of)
    has_object = any(s.get("type") == "object" for s in one_of)
    assert has_string and has_object, (
        "depEntry MUST permit both string (short-form) and object (table-form)"
    )


@pytest.mark.req("req-mf-008")
def test_consumer_supports_pinned_version():
    schema = load_schema("manifest-v0.1.schema.json")
    entry_obj = next(s for s in schema["$defs"]["depEntry"]["oneOf"] if s.get("type") == "object")
    assert "version" in entry_obj["properties"]


@pytest.mark.req("req-mf-009")
def test_consumer_supports_pinned_commit():
    schema = load_schema("manifest-v0.1.schema.json")
    entry_obj = next(s for s in schema["$defs"]["depEntry"]["oneOf"] if s.get("type") == "object")
    assert "ref" in entry_obj["properties"], (
        "depEntry MUST permit a `ref` field for commit / branch / tag pins"
    )


@pytest.mark.req("req-mf-010")
def test_consumer_supports_apm_source_short_form_string():
    schema = load_schema("manifest-v0.1.schema.json")
    one_of = schema["$defs"]["depEntry"]["oneOf"]
    string_form = next(s for s in one_of if s.get("type") == "string")
    assert string_form.get("minLength", 0) >= 1


@pytest.mark.req("req-mf-011")
def test_consumer_supports_apm_source_table_form():
    schema = load_schema("manifest-v0.1.schema.json")
    entry_obj = next(s for s in schema["$defs"]["depEntry"]["oneOf"] if s.get("type") == "object")
    options = entry_obj["oneOf"]
    required_sets = sorted(tuple(sorted(o["required"])) for o in options)
    assert required_sets == [("git",), ("id",), ("path",), ("registry",)], (
        "table-form depEntry MUST require exactly one of git/id/path/registry"
    )


@pytest.mark.req("req-mf-012")
def test_consumer_rejects_unknown_source_kind():
    doc = load_yaml_fixture("manifest", "invalid-source-kind.yml")
    with pytest.raises(jsonschema.ValidationError):
        validate_against("manifest-v0.1.schema.json", doc)


@pytest.mark.req("req-mf-013")
def test_consumer_supports_local_path_source():
    schema = load_schema("manifest-v0.1.schema.json")
    entry_obj = next(s for s in schema["$defs"]["depEntry"]["oneOf"] if s.get("type") == "object")
    assert "path" in entry_obj["properties"]


@pytest.mark.req("req-mf-014")
def test_producer_rejects_non_http_registry_scheme():
    """Schema pattern `^https?://` is the regression handle."""
    schema = load_schema("manifest-v0.1.schema.json")
    reg = schema["properties"]["registries"]["additionalProperties"]["oneOf"][1]
    assert reg["properties"]["url"]["pattern"] == "^https?://"
    doc = load_yaml_fixture("manifest", "invalid-registry-scheme.yml")
    with pytest.raises(jsonschema.ValidationError):
        validate_against("manifest-v0.1.schema.json", doc)


@pytest.mark.req("req-mf-015")
def test_producer_rejects_unknown_registries_keys():
    schema = load_schema("manifest-v0.1.schema.json")
    reg = schema["properties"]["registries"]["additionalProperties"]["oneOf"][1]
    assert reg["additionalProperties"] is False
    doc = load_yaml_fixture("manifest", "invalid-registries-typo.yml")
    with pytest.raises(jsonschema.ValidationError):
        validate_against("manifest-v0.1.schema.json", doc)


@pytest.mark.req("req-mf-016")
def test_consumer_rejects_absolute_paths_in_apm_source():
    """Spec restricts apm-source `path:` to relative form."""
    assert_spec_contains("path")
    waive(
        "Path-shape negative test requires apm_cli's path-policy loader "
        "to be invokable from the test harness; the JSON Schema currently "
        "models `path` as a free-form string. Tracked as a follow-up: "
        "tighten the schema to forbid leading `/` and document the "
        "absolute-path rejection in the schema additionalProperties."
    )


@pytest.mark.req("req-mf-017")
def test_producer_publishes_apm_yml_at_repo_root():
    assert_spec_contains("apm.yml")


@pytest.mark.req("req-mf-018")
def test_consumer_restricts_policy_hash_algorithm_to_strong_set():
    schema = load_schema("manifest-v0.1.schema.json")
    enum = schema["properties"]["policy"]["properties"]["hash_algorithm"]["enum"]
    assert set(enum) == {"sha256", "sha384", "sha512"}


@pytest.mark.req("req-mf-019")
def test_consumer_supports_default_host_field():
    schema = load_schema("manifest-v0.1.schema.json")
    assert "default_host" in schema["properties"]
    doc = load_yaml_fixture("manifest", "x-extension-roundtrip.yml")
    assert doc.get("default_host"), "fixture must exercise default_host"


@pytest.mark.req("req-mf-020")
def test_consumer_enforces_yaml_safe_subset():
    """Anchors / aliases MUST be rejected by a conforming consumer.

    PyYAML's `safe_load` is permissive of anchors; the spec requires
    additional rejection. The schema cannot enforce this on its own.
    The fixture is committed as a regression handle and the test
    asserts that the spec carries the four clauses verbatim, so the
    authoring panel cannot silently delete the requirement.
    """
    assert_spec_contains("YAML safe", "&anchor", "MUST be rejected", "YAML 1.1 octal")


@pytest.mark.req("req-mf-021")
def test_producer_workspaces_must_not_use_in_v0_1():
    """req-mf-021 forbids workspaces in v0.1."""
    assert_spec_contains("workspaces", "v0.1")


# --- req-ext-001..002 --------------------------------------------------


@pytest.mark.req("req-ext-001")
def test_consumer_preserves_x_extension_keys_on_round_trip():
    doc = load_yaml_fixture("manifest", "x-extension-roundtrip.yml")
    x_keys = [k for k in doc if k.startswith("x-")]
    assert x_keys, "fixture must contain at least one x-* key"
    schema = load_schema("manifest-v0.1.schema.json")
    pp = schema.get("patternProperties", {})
    assert any(k.startswith("^x-") for k in pp), (
        "manifest schema MUST declare patternProperties for x-* keys"
    )


@pytest.mark.req("req-ext-002")
def test_spec_reserves_x_prefix_for_vendor_extensions_only():
    assert_spec_contains(
        "MUST NOT define normative keys beginning with the prefix",
        "x-",
    )


# --- req-sc-001..008: scheme / source-control --------------------------


@pytest.mark.req("req-sc-001")
def test_sha256_content_hash_on_deployed_files():
    assert_spec_contains("SHA-256 content hash for every deployed file", "MUST re-verify")


@pytest.mark.req("req-sc-002")
def test_archive_path_traversal_fails_closed():
    assert_spec_contains(
        "reject any archive entry whose extracted path would contain `..`",
        "symbolic or hard link",
        "MUST fail closed",
    )


@pytest.mark.req("req-sc-003")
def test_consumer_resolves_credentials_per_host_class():
    assert_spec_contains(
        "resolve credentials per host class",
        "MUST NOT forward a credential",
        # Cross-host-class redirect drop (round-3 fold):
        "MUST drop the originating Authorization header",
    )


@pytest.mark.req("req-sc-004")
def test_archive_container_size_and_entry_count_capped():
    assert_spec_contains(
        "`application/gzip` over a tar payload",
        "MUST reject `application/zip`",
        "100 MB",
        "10,000",
    )


@pytest.mark.req("req-sc-005")
def test_host_class_collapse_constrained_to_psl_or_aliases():
    assert_spec_contains(
        "Public Suffix List",
        "explicit `aliases:` entry",
        "MUST NOT collapse two",
    )


@pytest.mark.req("req-sc-006")
def test_consumer_rejects_http_registry_url_without_opt_in():
    assert_spec_contains(
        "`http://` scheme as a\nparse-time error",
        "`insecure: true`",
        "loopback",
    )


@pytest.mark.req("req-sc-007")
def test_consumer_redacts_credential_material():
    assert_spec_contains(
        "redact credential material",
        "MUST NOT appear in any user-facing",
        "GITHUB_APM_PAT",
        "MUST refuse to pack",
    )


@pytest.mark.req("req-sc-008")
def test_consumer_should_refuse_credential_on_non_https_git_over_http():
    assert_spec_contains(
        "SHOULD",
        "refuse to attach a credential to a git-over-HTTP fetch",
    )


@pytest.mark.req("req-sc-009")
def test_consumer_must_deny_executable_primitive_without_allowexecutables_approval():
    assert_spec_contains(
        "deny deployment of any executable primitive",
        "allowExecutables",
        "fail closed",
    )


@pytest.mark.req("req-sc-010")
def test_consumer_must_persist_approvals_user_locally_not_in_project_manifest():
    assert_spec_contains(
        "persist per-user approval decisions",
        "isolated from the project manifest",
        "MUST NOT write interactive approval decisions into the project",
    )


@pytest.mark.req("req-sc-011")
def test_consumer_must_resolve_executable_trust_through_deny_wins_precedence():
    # Verbatim single-line substrings of the Section 10.14 normative text
    # (the authored sentences are line-wrapped; these needles fit one line each).
    assert_spec_contains(
        "through a single deny-wins",
        "overrides any project-level or user-level grant for the same package",
        "identical allow-or-deny",
    )


@pytest.mark.req("req-sc-012")
def test_consumer_required_package_audit_asserts_presence_not_deployment():
    assert_spec_contains(
        "evaluates a governance requirement mandating the presence of a package",
        "satisfaction of that requirement from the presence of the package in",
        "distinct from any missing-package violation",
    )


@pytest.mark.req("req-tg-001")
def test_consumer_target_detection_predicate_binding():
    assert_spec_contains(
        "Auto-detection MUST activate a target",
        "only** when its registered predicate fires",
        "agent-skills",
    )


@pytest.mark.req("req-tg-002")
def test_consumer_deploys_only_under_registered_roots():
    assert_spec_contains(
        "deploy primitives only under the deploy root(s)",
        "No target's\ninstaller MAY write files outside its registered root",
    )


@pytest.mark.req("req-tg-003")
def test_consumer_deploys_skills_to_agents_skills():
    assert_spec_contains(
        ".agents/skills/<name>/SKILL.md",
    )


@pytest.mark.req("req-tg-004")
def test_consumer_routes_vendor_target_identifiers_to_handlers():
    assert_spec_contains(
        "x-[a-z][a-z0-9-]*-[a-z][a-z0-9-]*",
        "MUST route detection",
        "MUST NOT silently",
    )


@pytest.mark.req("req-tg-005")
def test_consumer_deploys_antigravity_rules_with_expected_dedup():
    # Needles updated for the v0.1.11 spec-guardian fold on req-tg-005:
    # the anchor was reworded to (a) lowercase the `antigravity`
    # identifier, (b) pin a canonical `globs` scalar-vs-sequence
    # representation for hash reproducibility, and (c) redefine the
    # deduplication scope to filenames derived from the resolved
    # instruction primitives (fail-closed non-suppression). See
    # Appendix D revision 0.1.11.
    assert_spec_contains(
        "For the `antigravity` target, instruction rules MUST be written under",
        "`.agents/rules/<name>.md`",
        "`trigger: glob` plus a `globs` field",
        "emitted as a YAML scalar when `applyTo` resolves to exactly one glob",
        "as a YAML block sequence when it resolves to two or more",
        "names derive from the currently-resolved",
        "MUST NOT be treated as a deployed rule and MUST NOT",
    )


# --- req-cf-001..002 --------------------------------------------------


@pytest.mark.req("req-cf-001")
def test_round_trip_clause_present_in_spec():
    """The real fixed-point assertion lives in test_round_trip.py.

    This marker carries the spec-text grep so silent deletion of the
    round-trip language breaks the suite.
    """
    assert_spec_contains(
        "idempotent round-trip",
        "byte-equivalent file",
        "MUST be preserved verbatim across round-trip",
    )


@pytest.mark.req("req-cf-002")
def test_implementations_publish_conformance_statement():
    """The repo-root statement satisfies req-cf-002 for THIS implementation."""
    from tests.spec_conformance._manifest import REPO_ROOT

    statement = REPO_ROOT / "CONFORMANCE.md"
    json_statement = REPO_ROOT / "CONFORMANCE.json"
    if not statement.exists() or not json_statement.exists():
        waive(
            "CONFORMANCE.{md,json} not yet generated in this checkout. "
            "Run `uv run python -m tests.spec_conformance.gen_statement`."
        )
        return
    assert "NO automated CI detector" in statement.read_text(encoding="ascii")
    assert_spec_contains(
        "publish a conformance statement",
        "MUST\nlist, for each `req-XXX` in scope",
    )
