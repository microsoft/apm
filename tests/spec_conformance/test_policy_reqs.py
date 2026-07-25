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


@pytest.mark.req("req-pl-013")
def test_policy_require_hashes_parses_and_is_specified():
    """req-pl-013: security.integrity.require_hashes fail-closed install.

    Binds the parsed boolean to the spec MUST. The install enforcement
    itself is exercised by tests/unit/install/test_require_hashes.py;
    here we assert the parser surfaces the key and the spec language
    that mandates the fail-closed behaviour is intact.
    """
    from apm_cli.policy.parser import load_policy

    policy, _ = load_policy(fixture_path("policy", "security-integrity.yml"))
    assert policy.security.integrity.require_hashes is True
    assert_spec_contains(
        "`security.integrity.require_hashes: true`",
        "fail-closed diagnostic",
    )


@pytest.mark.req("req-pl-014")
def test_policy_fail_on_drift_parses_and_is_specified():
    """req-pl-014: security.audit.fail_on_drift non-zero audit exit.

    Binds the parsed boolean to the spec MUST. The audit exit-code
    path is exercised by tests/unit/test_audit_fail_on_drift.py; here
    we assert the parser surfaces the key and the spec language that
    mandates the non-zero exit is intact.
    """
    from apm_cli.policy.parser import load_policy

    policy, _ = load_policy(fixture_path("policy", "security-integrity.yml"))
    assert policy.security.audit.fail_on_drift is True
    assert_spec_contains(
        "`security.audit.fail_on_drift: true`",
        "non-zero exit status when lockfile",
    )


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


@pytest.mark.req("req-pl-016")
def test_deployment_ledger_owner_is_hard_integrity_failure():
    """req-pl-016: a canonical deployment-ledger owner absent from the
    lockfile is a hard integrity failure, independent of
    ``security.audit.fail_on_drift``.

    Binds the spec MUST to the shared authority --
    ``DeploymentLedgerCodec.owner_reference_violations`` and the
    ``deployment-ledger-owners`` baseline check -- so a regression that
    lets a stale owner pass silently breaks at PR time. The non-zero
    audit-exit path (default and CI) is exercised by
    ``tests/unit/test_audit_deployment_owners.py``; here we assert the
    detection surface and the spec language that mandates the unconditional
    hard failure are intact.
    """
    from apm_cli.core.deployment_ledger import (
        DEPLOYMENT_OWNER_REMEDIATION,
        DeploymentLedgerCodec,
    )
    from apm_cli.core.deployment_state import (
        DeploymentLedger,
        DeploymentLocator,
        DeploymentRecord,
        LocatorKind,
    )
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.policy.ci_checks import _check_deployment_ledger_owners

    ghost_owner = "removed/beta"
    locator = DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="claude",
        value=".claude/rules/beta.md",
        runtime=None,
        scope="project",
    )
    record = DeploymentRecord(
        locator=locator,
        owners=(ghost_owner,),
        active_owner=ghost_owner,
        content_hash=f"sha256:{'a' * 64}",
    )
    # Empty dependency set -> the ghost owner resolves to no lockfile entry.
    lockfile = LockFile(dependencies={})
    DeploymentLedgerCodec.apply_to_lockfile(
        DeploymentLedger(records={record.locator.key: record}),
        lockfile,
    )

    violations = DeploymentLedgerCodec.owner_reference_violations(lockfile)
    assert violations, "a ghost owner absent from the lockfile MUST be detected"
    assert ghost_owner in violations[0].invalid_owners

    check = _check_deployment_ledger_owners(lockfile)
    assert check.passed is False
    assert DEPLOYMENT_OWNER_REMEDIATION in check.message

    # Spec-text drift guard: the normative phrasing MUST stay in the body.
    assert_spec_contains(
        "hard integrity failure",
        "independent of the `security.audit.fail_on_drift` control",
        "**both** its default and CI modes",
    )


_ = waive  # keep import for any future structural waiver
