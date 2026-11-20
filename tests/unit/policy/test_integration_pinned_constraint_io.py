"""Integration tests for ``require_pinned_constraint`` flowing through the (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.policy.policy_checks import (
    run_policy_checks,
)
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-policy-mcp-transport-projection")]

CHECK_NAME = "dependency-pinned-constraint"


def _policy(*, enforcement: str = "block", required: bool = True) -> ApmPolicy:
    return ApmPolicy(
        enforcement=enforcement,
        dependencies=DependencyPolicy(require_pinned_constraint=required),
    )


# ---------------------------------------------------------------------------
# Enforcement variants
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# All-four-call-sites smoke test (parametrized)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Audit wrapper (run_policy_checks)
# ---------------------------------------------------------------------------


def test_run_policy_checks_audit_surfaces_pinned_violation(tmp_path: Path):
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text(
        "name: sample\n"
        "version: 0.0.1\n"
        "dependencies:\n"
        "  apm:\n"
        "    - acme/skills\n"
        "    - other/lib#^1.2.0\n",
        encoding="utf-8",
    )
    policy = _policy(enforcement="block")
    result = run_policy_checks(tmp_path, policy, fail_fast=False)
    pin = next(c for c in result.checks if c.name == CHECK_NAME)
    assert not pin.passed
    assert any("acme/skills" in d for d in pin.details)


# ---------------------------------------------------------------------------
# direct_dep_keys filter: pinned check must NOT flag transitives
# ---------------------------------------------------------------------------
