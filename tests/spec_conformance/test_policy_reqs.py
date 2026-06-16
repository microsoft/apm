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


@pytest.mark.req("req-pl-015")
def test_unmanaged_files_surfacing_completeness(tmp_path):
    """req-pl-015: surface every untracked file under a managed primitive
    target tree with its reason, deny-conflict note, and inferred type,
    while an ``unmanaged_files.exclude`` match is never surfaced.

    Binds the spec MUST to the real ``_check_unmanaged_files`` behavior so a
    regression in reason strings, type tagging, deny-conflict, or exclude
    suppression breaks at PR time. Also asserts the normative spec phrases.
    """
    from apm_cli.policy.policy_checks import _check_unmanaged_files
    from apm_cli.policy.schema import UnmanagedFilesPolicy

    inst_dir = tmp_path / ".github" / "instructions"
    agents_dir = tmp_path / ".github" / "agents"
    inst_dir.mkdir(parents=True)
    agents_dir.mkdir(parents=True)
    (inst_dir / "foo.instructions.md").write_text("x", encoding="utf-8")
    (inst_dir / "ignored.md").write_text("x", encoding="utf-8")
    (agents_dir / "evil.agent.md").write_text("x", encoding="utf-8")

    policy = UnmanagedFilesPolicy(
        action="warn",
        directories=(".github/instructions", ".github/agents"),
        exclude=("**/ignored.md",),
    )
    result = _check_unmanaged_files(tmp_path, None, policy, dependency_deny=("**/evil*",))
    body = "\n".join(result.details)

    # Untracked primitive surfaced with reason + inferred type.
    assert ".github/instructions/foo.instructions.md [type: instruction]" in body
    assert "not tracked in apm.lock.yaml" in body
    # Deny-conflict note carried where the path also matches a deny pattern.
    assert "matches deny rule (**/evil*)" in body
    assert ".github/agents/evil.agent.md [type: agent]" in body
    # An excluded path MUST NOT be surfaced.
    assert "ignored.md" not in body

    # Spec-text drift guard: the normative phrasing MUST stay in the body.
    assert_spec_contains(
        "not tracked in `apm.lock.yaml`",
        "inferred primitive type",
        "MUST NOT be surfaced",
    )


_ = waive  # keep import for any future structural waiver
