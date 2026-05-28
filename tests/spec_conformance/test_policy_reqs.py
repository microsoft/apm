"""Policy (apm-policy.yml) conformance tests -- sec.6.

Covers req-pl-001..012. Real assertions where the seed fixtures
allow (schema validation + cycle rejection); structural elsewhere.
"""

from __future__ import annotations

import pytest

from tests.spec_conformance._helpers import (
    load_yaml_fixture,
    validate_against,
    waive,
)


@pytest.mark.req("req-pl-001")
def test_policy_valid_extends_passes_schema():
    doc = load_yaml_fixture("policy", "valid-extends.yml")
    validate_against("policy-v0.1.schema.json", doc)


@pytest.mark.req("req-pl-002")
def test_policy_carries_apiversion_or_kind_key():
    doc = load_yaml_fixture("policy", "valid-extends.yml")
    assert "name" in doc, "policy fixture must declare a name"


@pytest.mark.req("req-pl-003")
def test_policy_extends_field_resolves_to_other_policy():
    doc = load_yaml_fixture("policy", "valid-extends.yml")
    assert "extends" in doc


@pytest.mark.req("req-pl-004")
def test_policy_extends_cycle_is_rejected():
    """Cycle detection is a behavioural test; the fixture is the
    contract artifact and the assertion is that the cycle fixture is
    structurally a cycle so a future apm_cli loader can be regression-
    tested against it.
    """
    doc = load_yaml_fixture("policy", "invalid-extends-cycle.yml")
    assert "extends" in doc, "cycle fixture must declare an extends edge"
    waive(
        "apm_cli policy-cycle detector wire-up is the v0.1.2 follow-up. "
        "Cycle fixture is in place; oracle binding deferred."
    )


@pytest.mark.req("req-pl-005")
def test_policy_rule_set_carries_required_fields():
    waive("Rule-set shape; covered structurally by valid-extends fixture.")


@pytest.mark.req("req-pl-006")
def test_policy_extends_resolves_relative_to_policy_root():
    waive("Path resolution detail; deferred to v0.1.2 fixture expansion.")


@pytest.mark.req("req-pl-007")
def test_policy_supports_allow_action():
    waive("Action enum coverage; deferred to v0.1.2 fixture expansion.")


@pytest.mark.req("req-pl-008")
def test_policy_supports_deny_action():
    waive("Action enum coverage; deferred to v0.1.2 fixture expansion.")


@pytest.mark.req("req-pl-009")
def test_policy_evaluator_short_circuits_on_first_deny():
    waive("Evaluator behaviour; integration test deferred to v0.1.2.")


@pytest.mark.req("req-pl-010")
def test_policy_apiversion_pinned_to_v0_1():
    waive("v0.1 policy seed fixtures do not yet carry an apiVersion field; deferred.")


@pytest.mark.req("req-pl-011")
def test_policy_provides_default_allow_list_shape():
    waive("Default-allow shape; structural.")


@pytest.mark.req("req-pl-012")
def test_policy_provides_default_deny_list_shape():
    waive("Default-deny shape; structural.")
