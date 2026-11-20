"""Policy target check phase driven by policy fixture files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional  # noqa: F401, UP035
from unittest.mock import MagicMock, patch

import pytest
import yaml

from apm_cli.core.target_detection import EffectiveTargetDecision
from apm_cli.install.phases.policy_gate import PolicyViolationError
from apm_cli.install.phases.policy_target_check import run
from apm_cli.policy.models import CheckResult, CIAuditResult
from apm_cli.policy.schema import (
    ApmPolicy,
    CompilationPolicy,
    CompilationTargetPolicy,
)

pytestmark = pytest.mark.component

# Path to fixtures
FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "policy"
TARGET_POLICY_FIXTURE = FIXTURES_DIR / "apm-policy-target-allow.yml"
TARGET_MISMATCH_DIR = FIXTURES_DIR / "projects" / "target-mismatch"


# -- Minimal synthetic context ----------------------------------------


@dataclass
class _FakePackage:
    """Minimal stand-in for APMPackage."""

    target: str | None = None


@dataclass
class _FakePolicyFetch:
    """Minimal stand-in for PolicyFetchResult."""

    policy: Any = None
    outcome: str = "found"
    source: str = "org:contoso/.github"
    cached: bool = False
    cache_age_seconds: int | None = None
    fetch_error: str | None = None


@dataclass
class _FakeContext:
    """Minimal stand-in for InstallContext with fields policy_target_check reads."""

    project_root: Path = field(default_factory=lambda: Path("/tmp/fake-project"))
    apm_dir: Path = field(default_factory=lambda: Path("/tmp/fake-project/.apm"))
    verbose: bool = False
    logger: Any = None
    deps_to_install: list[Any] = field(default_factory=list)
    existing_lockfile: Any = None

    # From caller / CLI
    apm_package: Any = None
    target_override: str | list[str] | None = None
    target_decision: EffectiveTargetDecision | None = None

    # From policy_gate
    policy_fetch: Any = None
    policy_enforcement_active: bool = False


def _make_ctx(
    *,
    logger=None,
    enforcement_active=True,
    policy_fetch=None,
    target_override=None,
    manifest_target=None,
    deps=None,
):
    """Build a _FakeContext whose target decision mirrors the pipeline."""
    pkg = _FakePackage(target=manifest_target)
    decision = (
        EffectiveTargetDecision(target_override, "--target flag")
        if target_override is not None
        else EffectiveTargetDecision(manifest_target, "apm.yml")
    )
    return _FakeContext(
        logger=logger or MagicMock(),
        deps_to_install=deps or [],
        apm_package=pkg,
        target_override=decision.value,
        target_decision=decision,
        policy_fetch=policy_fetch,
        policy_enforcement_active=enforcement_active,
    )


def _load_target_policy_from_fixture() -> ApmPolicy:
    """Load the apm-policy-target-allow.yml fixture into an ApmPolicy."""
    raw = yaml.safe_load(TARGET_POLICY_FIXTURE.read_text())
    # Build the policy with compilation.target.allow from fixture
    allow = raw.get("compilation", {}).get("target", {}).get("allow")
    enforcement = raw.get("enforcement", "warn")
    allow_tuple = tuple(allow) if allow else None
    return ApmPolicy(
        name=raw.get("name", ""),
        version=raw.get("version", ""),
        enforcement=enforcement,
        compilation=CompilationPolicy(
            target=CompilationTargetPolicy(allow=allow_tuple),
        ),
    )


def _target_failing_audit(*, target_value="claude", allowed=("vscode",)):
    """CIAuditResult with a failing compilation-target check."""
    return CIAuditResult(
        checks=[
            # Dep checks that already ran in gate phase (should be filtered out)
            CheckResult(name="dependency-allowlist", passed=True, message="OK"),
            # The target check that should be processed
            CheckResult(
                name="compilation-target",
                passed=False,
                message=f"Target(s) ['{target_value}'] not in allowed list {sorted(allowed)}",
                details=[f"target: {target_value}, allowed: {sorted(allowed)}"],
            ),
        ]
    )


def _target_passing_audit():
    """CIAuditResult where the compilation-target check passes."""
    return CIAuditResult(
        checks=[
            CheckResult(name="dependency-allowlist", passed=True, message="OK"),
            CheckResult(
                name="compilation-target",
                passed=True,
                message="Compilation target compliant",
            ),
        ]
    )


# Patch target for run_dependency_policy_checks
_PATCH_CHECKS = "apm_cli.policy.policy_checks.run_dependency_policy_checks"


# =====================================================================
# Test: skip conditions (noop paths)
# =====================================================================


# =====================================================================
# Test: block mode
# =====================================================================


# =====================================================================
# Test: warn mode
# =====================================================================


# =====================================================================
# Test: --target CLI override
# =====================================================================


# =====================================================================
# Test: double-emit filtering
# =====================================================================


# =====================================================================
# Test: fixture-based integration
# =====================================================================


class TestWithFixtures:
    """Tests using the real policy fixture files."""

    def test_fixture_loads_correctly(self):
        """Verify apm-policy-target-allow.yml fixture parses to allow=[vscode]."""
        assert TARGET_POLICY_FIXTURE.exists(), f"Fixture not found: {TARGET_POLICY_FIXTURE}"
        policy = _load_target_policy_from_fixture()
        assert policy.enforcement == "block"
        assert policy.compilation.target.allow == ("vscode",)

    def test_target_mismatch_fixture_exists(self):
        """Verify the target-mismatch project fixture exists with target=claude."""
        apm_yml = TARGET_MISMATCH_DIR / "apm.yml"
        assert apm_yml.exists(), f"Fixture not found: {apm_yml}"
        raw = yaml.safe_load(apm_yml.read_text())
        assert raw["target"] == "claude"

    @patch(_PATCH_CHECKS)
    def test_fixture_block_mode_target_mismatch(self, mock_checks):
        """Real fixture: policy allow=[vscode], project target=claude, block -> raises."""
        policy = _load_target_policy_from_fixture()
        fetch = _FakePolicyFetch(policy=policy, outcome="found")

        mock_checks.return_value = _target_failing_audit(target_value="claude", allowed=("vscode",))

        # Read manifest target from fixture
        apm_yml = TARGET_MISMATCH_DIR / "apm.yml"
        raw = yaml.safe_load(apm_yml.read_text())
        manifest_target = raw["target"]

        ctx = _make_ctx(
            enforcement_active=True,
            policy_fetch=fetch,
            manifest_target=manifest_target,
        )

        with pytest.raises(PolicyViolationError, match="compilation target"):
            run(ctx)

    @patch(_PATCH_CHECKS)
    def test_fixture_cli_override_fixes_mismatch(self, mock_checks):
        """Real fixture: --target vscode overrides manifest claude -> passes."""
        policy = _load_target_policy_from_fixture()
        fetch = _FakePolicyFetch(policy=policy, outcome="found")

        mock_checks.return_value = _target_passing_audit()

        ctx = _make_ctx(
            enforcement_active=True,
            policy_fetch=fetch,
            target_override="vscode",  # override fixes the mismatch
            manifest_target="claude",  # from fixture
        )

        run(ctx)  # should NOT raise

        # Verify the override was passed through
        call_kwargs = mock_checks.call_args
        assert call_kwargs[1]["effective_target"] == ("copilot",)


# =====================================================================
# Test: no logger (defensive)
# =====================================================================
