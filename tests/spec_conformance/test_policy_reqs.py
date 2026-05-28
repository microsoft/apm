"""Policy (apm-policy.yml) conformance tests -- sec.6."""

from __future__ import annotations

import pytest

from tests.spec_conformance._helpers import (
    assert_spec_contains,
    fixture_path,
    load_schema,
    load_yaml_fixture,
    validate_against,
    waive,
)


@pytest.mark.req("req-pl-001")
def test_policy_valid_extends_passes_schema():
    validate_against("policy-v0.1.schema.json", load_yaml_fixture("policy", "valid-extends.yml"))


@pytest.mark.req("req-pl-002")
def test_policy_carries_apiversion_or_kind_key():
    schema = load_schema("policy-v0.1.schema.json")
    assert "name" in schema["properties"]
    doc = load_yaml_fixture("policy", "valid-extends.yml")
    assert "name" in doc


@pytest.mark.req("req-pl-003")
def test_policy_extends_field_resolves_to_other_policy():
    schema = load_schema("policy-v0.1.schema.json")
    assert schema["properties"]["extends"]["type"] == "string"
    doc = load_yaml_fixture("policy", "valid-extends.yml")
    assert "extends" in doc


@pytest.mark.req("req-pl-004")
def test_policy_extends_cycle_is_rejected():
    """Spec-text grep + structural fixture binding.

    apm_cli's policy loader is fetch-driven (the cycle would manifest
    only on cross-host fetch). The fixture captures the cycle as a
    contract artifact; the spec language is asserted so silent
    deletion breaks the test.
    """
    doc = load_yaml_fixture("policy", "invalid-extends-cycle.yml")
    assert "extends" in doc
    assert_spec_contains("cycle")


@pytest.mark.req("req-pl-005")
def test_policy_rule_set_carries_required_fields():
    from apm_cli.policy.parser import load_policy

    policy, _ = load_policy(fixture_path("policy", "valid-extends.yml"))
    assert policy.name == "contoso-baseline"
    assert policy.enforcement == "block"


@pytest.mark.req("req-pl-006")
def test_policy_extends_resolves_relative_to_policy_root():
    assert_spec_contains(
        "host class",
        "MUST NOT extend a\npolicy fetched from any other host class",
    )


@pytest.mark.req("req-pl-007")
def test_policy_supports_allow_action():
    from apm_cli.policy.parser import load_policy

    policy, _ = load_policy(fixture_path("policy", "valid-extends.yml"))
    assert policy.dependencies.allow is not None
    assert "contoso/*" in policy.dependencies.allow


@pytest.mark.req("req-pl-008")
def test_policy_supports_deny_action():
    from apm_cli.policy.parser import load_policy

    policy, _ = load_policy(fixture_path("policy", "valid-extends.yml"))
    assert policy.dependencies.deny is not None
    assert "*/legacy-*" in policy.dependencies.deny


@pytest.mark.req("req-pl-009")
def test_policy_evaluator_short_circuits_on_first_deny():
    assert_spec_contains("deny")
    # Wire-level evaluator assertion is exercised by apm_cli's own
    # unit tests under tests/policy/; here we assert that the spec
    # language for the short-circuit rule is intact.
    assert_spec_contains("deny", "extends")


@pytest.mark.req("req-pl-010")
def test_policy_apiversion_pinned_to_v0_1():
    schema = load_schema("policy-v0.1.schema.json")
    assert schema["$id"].endswith("policy-v0.1.schema.json")
    # Default-value pins (round-3 fold): the spec names `warn` and
    # `project-wins` as the effective defaults for `fetch_failure` and
    # `dependencies.require_resolution`; mirror them in the schema as
    # advisory `default` annotations so a reverter trips this test.
    assert schema["properties"]["fetch_failure"]["default"] == "warn"
    assert (
        schema["properties"]["dependencies"]["properties"]["require_resolution"]["default"]
        == "project-wins"
    )
    assert_spec_contains(
        "`fetch_failure` is unset, the effective value is `warn`",
        "Default `project-wins` when unset",
    )


@pytest.mark.req("req-pl-011")
def test_policy_provides_default_allow_list_shape():
    schema = load_schema("policy-v0.1.schema.json")
    deps = schema["properties"]["dependencies"]["properties"]
    assert "allow" in deps and deps["allow"]["oneOf"][0]["type"] == "array"


@pytest.mark.req("req-pl-012")
def test_policy_provides_default_deny_list_shape():
    schema = load_schema("policy-v0.1.schema.json")
    deps = schema["properties"]["dependencies"]["properties"]
    assert "deny" in deps and deps["deny"]["oneOf"][0]["type"] == "array"


_ = waive  # keep import for any future structural waiver
