"""Tests for per-scanner governance (security.audit.scanners) in policy.

Covers schema, parser validation/build, and inheritance AND-merge of the
restrict-only ``allow_args`` floor.
"""

from __future__ import annotations

from apm_cli.policy.inheritance import merge_policies
from apm_cli.policy.parser import _build_policy, validate_policy
from apm_cli.policy.schema import (
    ApmPolicy,
    AuditPolicy,
    ScannerGovernance,
    SecurityPolicy,
)


def _build(scanners_block: dict) -> AuditPolicy:
    data = {"security": {"audit": {"scanners": scanners_block}}}
    return _build_policy(data).security.audit


# ---------------------------------------------------------------------------
# parser: build
# ---------------------------------------------------------------------------


def test_build_scanners_allow_args_false():
    audit = _build({"skillspector": {"allow_args": False}})
    assert audit.scanners == (("skillspector", ScannerGovernance(allow_args=False)),)


def test_build_scanners_allow_args_true():
    audit = _build({"skillspector": {"allow_args": True}})
    assert audit.scanners == (("skillspector", ScannerGovernance(allow_args=True)),)


def test_build_scanners_absent_is_none():
    audit = _build_policy({"security": {"audit": {"on_install": "warn"}}}).security.audit
    assert audit.scanners is None


def test_build_empty_block_yields_none_governance_value():
    audit = _build({"skillspector": None})
    assert audit.scanners == (("skillspector", ScannerGovernance(allow_args=None)),)


def test_build_non_bool_allow_args_coerces_to_none():
    """A non-bool allow_args (e.g. unvalidated 'yes') must never become True.

    ``_build_policy`` may run on data that skipped ``validate_policy``; a truthy
    string like ``"yes"`` must be treated as no-opinion (None), not silently
    coerced to an args kill-switch.
    """
    audit = _build({"skillspector": {"allow_args": "yes"}})
    assert audit.scanners == (("skillspector", ScannerGovernance(allow_args=None)),)


# ---------------------------------------------------------------------------
# parser: validation
# ---------------------------------------------------------------------------


def test_validate_scanners_must_be_mapping():
    errors, _ = validate_policy({"security": {"audit": {"scanners": ["skillspector"]}}})
    assert any("scanners must be a YAML mapping" in e for e in errors)


def test_validate_allow_args_must_be_bool():
    errors, _ = validate_policy(
        {"security": {"audit": {"scanners": {"skillspector": {"allow_args": "yes"}}}}}
    )
    assert any("allow_args must be a boolean" in e for e in errors)


def test_validate_unknown_scanner_warns_not_errors():
    errors, warnings = validate_policy(
        {"security": {"audit": {"scanners": {"bogus": {"allow_args": False}}}}}
    )
    assert not any("bogus" in e for e in errors)
    assert any("bogus" in w for w in warnings)


def test_validate_valid_block_is_clean():
    errors, _ = validate_policy(
        {"security": {"audit": {"scanners": {"skillspector": {"allow_args": False}}}}}
    )
    assert errors == []


# ---------------------------------------------------------------------------
# inheritance: AND-merge (restrict-only)
# ---------------------------------------------------------------------------


def _policy_with(scanners: tuple) -> ApmPolicy:
    return ApmPolicy(
        security=SecurityPolicy(audit=AuditPolicy(scanners=scanners)),
    )


def _allow_args_of(policy: ApmPolicy, name: str) -> bool | None:
    scanners = dict(policy.security.audit.scanners or ())
    gov = scanners.get(name)
    return gov.allow_args if gov is not None else None


def test_merge_false_beats_true():
    parent = _policy_with((("skillspector", ScannerGovernance(allow_args=False)),))
    child = _policy_with((("skillspector", ScannerGovernance(allow_args=True)),))
    merged = merge_policies(parent, child)
    assert _allow_args_of(merged, "skillspector") is False


def test_merge_child_false_tightens_parent_true():
    parent = _policy_with((("skillspector", ScannerGovernance(allow_args=True)),))
    child = _policy_with((("skillspector", ScannerGovernance(allow_args=False)),))
    merged = merge_policies(parent, child)
    assert _allow_args_of(merged, "skillspector") is False


def test_merge_none_transparent():
    parent = _policy_with((("skillspector", ScannerGovernance(allow_args=False)),))
    child = ApmPolicy()  # child has no opinion
    merged = merge_policies(parent, child)
    assert _allow_args_of(merged, "skillspector") is False


def test_merge_unions_scanner_names():
    parent = _policy_with((("skillspector", ScannerGovernance(allow_args=False)),))
    child = _policy_with((("sarif", ScannerGovernance(allow_args=True)),))
    merged = merge_policies(parent, child)
    names = {n for n, _ in (merged.security.audit.scanners or ())}
    assert names == {"skillspector", "sarif"}


def test_merge_both_none_is_none():
    merged = merge_policies(ApmPolicy(), ApmPolicy())
    assert merged.security.audit.scanners is None
