"""Integration tests for ``require_pinned_constraint`` flowing through the
four policy-check entry points.

Covers:
- ``run_dependency_policy_checks`` (gate seam: ``policy_gate``,
  ``policy_target_check``, ``run_policy_preflight`` all go through this)
- ``run_policy_checks`` (audit wrapper)

The parametrized ``test_pinned_check_runs_at_all_four_call_sites``
verifies the check name surfaces (passing or failing) on every
call site.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.models.apm_package import DependencyReference
from apm_cli.policy.policy_checks import (
    run_dependency_policy_checks,
    run_policy_checks,
)
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy

CHECK_NAME = "dependency-pinned-constraint"


def _refs(*specs: str) -> list[DependencyReference]:
    return [DependencyReference.parse(s) for s in specs]


def _policy(*, enforcement: str = "block", required: bool = True) -> ApmPolicy:
    return ApmPolicy(
        enforcement=enforcement,
        dependencies=DependencyPolicy(require_pinned_constraint=required),
    )


# ---------------------------------------------------------------------------
# Enforcement variants
# ---------------------------------------------------------------------------


def test_policy_block_with_pinned_required_aborts_install_on_unbounded():
    deps = _refs("acme/skills", "other/lib#>=1.0.0", "third/lib#^1.2.0")
    result = run_dependency_policy_checks(deps, policy=_policy(enforcement="block"), fail_fast=True)
    assert not result.passed
    failing = [c for c in result.checks if c.name == CHECK_NAME and not c.passed]
    assert len(failing) == 1
    # Both unbounded deps must appear in details.
    details = " ".join(failing[0].details)
    assert "acme/skills" in details
    assert "other/lib" in details
    assert "third/lib" not in details


def test_policy_warn_with_pinned_required_does_not_abort_runner():
    """The runner doesn't decide block vs warn; it just reports.

    Enforcement routing is the caller's responsibility (policy_gate
    converts failed checks into warnings under enforcement=warn). The
    runner still returns the failing check so the caller can render it.
    """
    deps = _refs("acme/skills")
    result = run_dependency_policy_checks(deps, policy=_policy(enforcement="warn"), fail_fast=False)
    # Check ran and reported the violation.
    assert not result.passed
    assert any(c.name == CHECK_NAME and not c.passed for c in result.checks)


def test_policy_off_path_emits_no_diagnostic_even_when_unbounded():
    """When require_pinned_constraint is False the check passes silently.

    (Note: ``enforcement: off`` is handled upstream in policy_gate; at the
    runner level the relevant knob is the field itself.)
    """
    deps = _refs("acme/skills", "other/lib#>=1.0.0")
    result = run_dependency_policy_checks(deps, policy=_policy(required=False), fail_fast=True)
    pin_checks = [c for c in result.checks if c.name == CHECK_NAME]
    assert len(pin_checks) == 1
    assert pin_checks[0].passed
    assert "disabled" in pin_checks[0].message.lower()


def test_policy_block_with_pinned_false_does_nothing():
    """Regression trap: disabled field must never produce violations."""
    deps = _refs("acme/skills", "other/lib")
    result = run_dependency_policy_checks(deps, policy=_policy(enforcement="block", required=False))
    assert not any(c.name == CHECK_NAME and not c.passed for c in result.checks)


def test_policy_block_emits_actionable_hint_per_dep():
    deps = _refs("acme/skills", "other/lib#*", "third/lib#>=2.0.0")
    result = run_dependency_policy_checks(
        deps, policy=_policy(enforcement="block"), fail_fast=False
    )
    failing = next(c for c in result.checks if c.name == CHECK_NAME and not c.passed)
    joined = "\n".join(failing.details)
    assert "no ref" in joined  # NO_REF for acme/skills
    assert "wildcard" in joined  # WILDCARD for other/lib#*
    assert "unbounded upper" in joined  # OPEN_UPPER for third/lib#>=2.0.0
    # ASCII-only invariant.
    for line in failing.details:
        line.encode("ascii", errors="strict")


# ---------------------------------------------------------------------------
# All-four-call-sites smoke test (parametrized)
# ---------------------------------------------------------------------------


def _run_via_dep_seam(deps, policy):
    return run_dependency_policy_checks(deps, policy=policy, fail_fast=False)


def _run_via_policy_gate_seam(deps, policy):
    """Mirror the call pattern policy_gate uses (see install/phases/policy_gate.py)."""
    return run_dependency_policy_checks(
        deps,
        lockfile=None,
        policy=policy,
        mcp_deps=None,
        effective_target=None,
        fetch_outcome="cached",
        fail_fast=(policy.enforcement == "block"),
    )


def _run_via_target_check_seam(deps, policy):
    """Mirror the call pattern policy_target_check uses."""
    return run_dependency_policy_checks(
        deps,
        lockfile=None,
        policy=policy,
        effective_target="vscode",
        fetch_outcome="cached",
        fail_fast=False,
    )


def _run_via_preflight_seam(deps, policy):
    """Mirror the call pattern install_preflight.run_policy_preflight uses."""
    return run_dependency_policy_checks(
        deps,
        lockfile=None,
        policy=policy,
        mcp_deps=[],
        effective_target=None,
        fetch_outcome="cached",
        fail_fast=(policy.enforcement == "block"),
    )


@pytest.mark.parametrize(
    "runner",
    [
        _run_via_dep_seam,
        _run_via_policy_gate_seam,
        _run_via_target_check_seam,
        _run_via_preflight_seam,
    ],
    ids=[
        "run_dependency_policy_checks-direct",
        "policy_gate-call-pattern",
        "policy_target_check-call-pattern",
        "run_policy_preflight-call-pattern",
    ],
)
def test_policy_pinned_check_runs_at_all_four_call_sites(runner):
    deps = _refs("acme/skills", "other/lib#^1.0.0")
    result = runner(deps, _policy(enforcement="block"))
    # Check is present (failing) at every call site.
    names = [c.name for c in result.checks]
    assert CHECK_NAME in names
    failing = [c for c in result.checks if c.name == CHECK_NAME and not c.passed]
    assert len(failing) == 1
    assert any("acme/skills" in d for d in failing[0].details)


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


def test_pinned_check_skips_transitive_when_direct_dep_keys_provided():
    """Regression trap (#1494 Copilot review): callers that distinguish
    direct vs transitive deps pass ``direct_dep_keys``; the pinned-
    constraint check must restrict its evaluation to those keys.

    Scenario: direct dep is pinned; transitive dep has an unbounded
    constraint declared in its own manifest. The consumer cannot
    rewrite the transitive's constraint, so the check must pass.
    """
    direct = DependencyReference.parse("acme/skills#^1.0.0")
    transitive_unbounded = DependencyReference.parse("third/lib#*")
    deps = [direct, transitive_unbounded]
    direct_keys = {direct.get_unique_key()}

    result = run_dependency_policy_checks(
        deps,
        policy=_policy(enforcement="block"),
        fail_fast=False,
        direct_dep_keys=direct_keys,
    )
    pin = next(c for c in result.checks if c.name == CHECK_NAME)
    assert pin.passed, (
        f"transitive unbounded dep should be skipped when direct_dep_keys "
        f"is provided; got details={pin.details}"
    )


def test_pinned_check_flags_direct_unbounded_even_with_pinned_transitive():
    """Counterpart: when the direct dep is the offender it must still
    surface; passing ``direct_dep_keys`` must not silence direct deps.
    """
    direct_unbounded = DependencyReference.parse("acme/skills")  # NO_REF
    transitive_pinned = DependencyReference.parse("third/lib#^1.0.0")
    deps = [direct_unbounded, transitive_pinned]
    direct_keys = {direct_unbounded.get_unique_key()}

    result = run_dependency_policy_checks(
        deps,
        policy=_policy(enforcement="block"),
        fail_fast=False,
        direct_dep_keys=direct_keys,
    )
    pin = next(c for c in result.checks if c.name == CHECK_NAME and not c.passed)
    assert any("acme/skills" in d for d in pin.details)
    assert not any("third/lib" in d for d in pin.details)


def test_pinned_check_legacy_no_filter_still_evaluates_all_deps():
    """Backwards-compat: when ``direct_dep_keys`` is ``None`` (legacy
    dep-only seam, audit wrapper) every dep is evaluated -- preserves
    behavior for callers that have no direct-vs-transitive context.
    """
    deps = _refs("acme/skills", "other/lib#*")
    result = run_dependency_policy_checks(
        deps, policy=_policy(enforcement="block"), fail_fast=False
    )
    pin = next(c for c in result.checks if c.name == CHECK_NAME and not c.passed)
    joined = " ".join(pin.details)
    assert "acme/skills" in joined
    assert "other/lib" in joined
