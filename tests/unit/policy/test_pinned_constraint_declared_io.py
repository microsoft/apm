"""Declared-constraint and package-qualified tag coverage for #3224 (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.replacement import project_manifest_dependencies
from apm_cli.policy._constraint_pinning import (
    collect_declared_refs,
)
from apm_cli.policy.policy_checks import _check_pinned_constraints, run_policy_checks
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy

pytestmark = pytest.mark.component

_REPLACE_SCHEMA = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
)


# ---------------------------------------------------------------------------
# Package-qualified literal tags and exact package release tags (name@X.Y.Z),
# #3731. Plain ref classification lives in test_pinned_constraint.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The declared snapshot beats the rewritten reference
# ---------------------------------------------------------------------------


def _policy() -> DependencyPolicy:
    return DependencyPolicy(require_pinned_constraint=True)


# ---------------------------------------------------------------------------
# Several declarations of one package (Copilot review on #3381)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# replace: rules are judged by the selected source (Copilot review on #3652)
# ---------------------------------------------------------------------------


def _manifest(tmp_path: Path, fork_ref: str) -> APMPackage:
    (tmp_path / "apm.yml").write_text(
        f"$schema: {_REPLACE_SCHEMA}\n"
        "name: consumer\n"
        "version: 1.0.0\n"
        "dependencies:\n"
        "  apm:\n"
        "    - acme/widget#^1.0.0\n"
        "replace:\n"
        "  - for: {git: https://github.com/acme/widget}\n"
        f"    with: {{git: https://github.com/acme/fork, ref: {fork_ref}}}\n",
        encoding="utf-8",
    )
    return APMPackage.from_apm_yml(tmp_path / "apm.yml")


@pytest.mark.parametrize(("fork_ref", "passed"), [("main", False), ("v2.0.0", True)])
def test_replacement_is_judged_by_its_selected_ref(tmp_path, fork_ref, passed):
    # A bounded original is provenance, not a constraint on the replacement
    # (manage-dependencies.md), so it must not cover an unbounded fork ref.
    selected = project_manifest_dependencies(_manifest(tmp_path, fork_ref))
    result = _check_pinned_constraints(
        selected,
        _policy(),
        {dep.get_unique_key() for dep in selected},
        declared_refs=collect_declared_refs(selected),
    )
    assert result.passed is passed, result.details


@pytest.mark.parametrize(("fork_ref", "passed"), [("main", False), ("v2.0.0", True)])
def test_audit_judges_a_replacement_by_its_selected_ref(tmp_path, fork_ref, passed):
    # ``apm audit`` must agree with the install gate on a replaced dep.
    _manifest(tmp_path, fork_ref)
    policy = ApmPolicy(dependencies=DependencyPolicy(require_pinned_constraint=True))
    result = run_policy_checks(tmp_path, policy, fail_fast=False)
    (check,) = [c for c in result.checks if c.name == "dependency-pinned-constraint"]
    assert check.passed is passed, check.details
