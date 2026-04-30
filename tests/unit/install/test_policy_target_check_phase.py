"""Unit tests for the policy_target_check install pipeline phase.

Covers:
- Block-mode + disallowed target -> raises PolicyViolationError
- Warn-mode + disallowed target -> emits warn diagnostic, does not raise
- Off-mode / enforcement_active=False -> noop
- --target CLI override that does NOT match policy.allow -> raises
- --target CLI override that DOES match policy.allow -> passes
- Skip when policy_enforcement_active=False (escape-hatched)
- Skip when no policy fetched
- Uses fixtures: apm-policy-target-allow.yml + target-mismatch/

Design reference: plan.md section G, rubber-duck finding I6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional  # noqa: F401, UP035
from unittest.mock import MagicMock, patch

import pytest
import yaml

from apm_cli.install.phases.policy_gate import PolicyViolationError
from apm_cli.install.phases.policy_target_check import TARGET_CHECK_IDS, run
from apm_cli.policy.models import CheckResult, CIAuditResult
from apm_cli.policy.schema import (
    ApmPolicy,
    CompilationPolicy,
    CompilationTargetPolicy,
)

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
    target_override: str | None = None

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
    """Build a _FakeContext with defaults."""
    pkg = _FakePackage(target=manifest_target)
    return _FakeContext(
        logger=logger or MagicMock(),
        deps_to_install=deps or [],
        apm_package=pkg,
        target_override=target_override,
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


def _make_policy_fetch(*, enforcement="block", allow=("vscode",)):
    """Build a _FakePolicyFetch with a custom compilation target allow list."""
    policy = ApmPolicy(
        enforcement=enforcement,
        compilation=CompilationPolicy(
            target=CompilationTargetPolicy(allow=allow),
        ),
    )
    return _FakePolicyFetch(policy=policy)


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


class TestSkipConditions:
    """Phase should noop when preconditions are not met."""

    def test_skip_when_enforcement_not_active(self):
        """Skip when policy_enforcement_active is False (escape-hatched, no policy, etc.)."""
        ctx = _make_ctx(
            enforcement_active=False,
            policy_fetch=_make_policy_fetch(),
            manifest_target="claude",
        )

        run(ctx)  # should not raise

        assert ctx.policy_enforcement_active is False

    def test_skip_when_no_policy_fetched(self):
        """Skip when policy_fetch is None (no discovery ran)."""
        ctx = _make_ctx(
            enforcement_active=True,
            policy_fetch=None,
            manifest_target="claude",
        )

        run(ctx)  # should not raise

    def test_skip_when_policy_fetch_has_no_policy(self):
        """Skip when policy_fetch exists but policy object is None."""
        fetch = _FakePolicyFetch(policy=None)
        ctx = _make_ctx(
            enforcement_active=True,
            policy_fetch=fetch,
            manifest_target="claude",
        )

        run(ctx)  # should not raise

    def test_skip_when_no_effective_target(self):
        """Skip when neither --target nor manifest target is set."""
        ctx = _make_ctx(
            enforcement_active=True,
            policy_fetch=_make_policy_fetch(),
            manifest_target=None,
            target_override=None,
        )

        run(ctx)  # should not raise


# =====================================================================
# Test: block mode
# =====================================================================


class TestBlockMode:
    """enforcement=block + disallowed target -> PolicyViolationError."""

    @patch(_PATCH_CHECKS)
    def test_block_mode_disallowed_target_raises(self, mock_checks):
        """Manifest target=claude, policy allow=[vscode], enforcement=block -> raises."""
        mock_checks.return_value = _target_failing_audit()
        ctx = _make_ctx(
            enforcement_active=True,
            policy_fetch=_make_policy_fetch(enforcement="block"),
            manifest_target="claude",
        )

        with pytest.raises(PolicyViolationError, match="compilation target"):
            run(ctx)

        # Verify the violation was logged
        ctx.logger.policy_violation.assert_called_once()
        call_kwargs = ctx.logger.policy_violation.call_args
        assert call_kwargs[1]["severity"] == "block"
        assert call_kwargs[1]["dep_ref"] == "compilation-target"

    @patch(_PATCH_CHECKS)
    def test_block_mode_allowed_target_passes(self, mock_checks):
        """Manifest target=vscode, policy allow=[vscode], enforcement=block -> passes."""
        mock_checks.return_value = _target_passing_audit()
        ctx = _make_ctx(
            enforcement_active=True,
            policy_fetch=_make_policy_fetch(enforcement="block"),
            manifest_target="vscode",
        )

        run(ctx)  # should not raise

        ctx.logger.policy_violation.assert_not_called()


# =====================================================================
# Test: warn mode
# =====================================================================


class TestWarnMode:
    """enforcement=warn + disallowed target -> warn diagnostic, no raise."""

    @patch(_PATCH_CHECKS)
    def test_warn_mode_disallowed_target_does_not_raise(self, mock_checks):
        """Manifest target=claude, policy allow=[vscode], enforcement=warn -> warn only."""
        mock_checks.return_value = _target_failing_audit()
        ctx = _make_ctx(
            enforcement_active=True,
            policy_fetch=_make_policy_fetch(enforcement="warn"),
            manifest_target="claude",
        )

        run(ctx)  # should NOT raise

        # Verify the violation was logged as a warning
        ctx.logger.policy_violation.assert_called_once()
        call_kwargs = ctx.logger.policy_violation.call_args
        assert call_kwargs[1]["severity"] == "warn"
        assert call_kwargs[1]["dep_ref"] == "compilation-target"


# =====================================================================
# Test: --target CLI override
# =====================================================================


class TestTargetOverride:
    """--target CLI override determines effective target for policy checks."""

    @patch(_PATCH_CHECKS)
    def test_cli_override_disallowed_raises_in_block_mode(self, mock_checks):
        """--target claude overrides manifest; claude not in allow=[vscode] -> raises."""
        mock_checks.return_value = _target_failing_audit()
        ctx = _make_ctx(
            enforcement_active=True,
            policy_fetch=_make_policy_fetch(enforcement="block"),
            target_override="claude",  # CLI --target
            manifest_target="vscode",  # would be allowed, but overridden
        )

        with pytest.raises(PolicyViolationError):
            run(ctx)

        # Verify effective_target passed to checks is the CLI override
        mock_checks.assert_called_once()
        call_kwargs = mock_checks.call_args
        assert call_kwargs[1]["effective_target"] == "claude"

    @patch(_PATCH_CHECKS)
    def test_cli_override_fixes_disallowed_manifest_target(self, mock_checks):
        """Manifest target=claude (disallowed), --target vscode (allowed) -> passes.

        This is the key I6 scenario: the CLI override fixes a manifest
        target that would otherwise be disallowed.
        """
        mock_checks.return_value = _target_passing_audit()
        ctx = _make_ctx(
            enforcement_active=True,
            policy_fetch=_make_policy_fetch(enforcement="block"),
            target_override="vscode",  # CLI override fixes it
            manifest_target="claude",  # would be disallowed
        )

        run(ctx)  # should NOT raise

        # Verify effective_target passed to checks is the CLI override
        mock_checks.assert_called_once()
        call_kwargs = mock_checks.call_args
        assert call_kwargs[1]["effective_target"] == "vscode"

        ctx.logger.policy_violation.assert_not_called()


# =====================================================================
# Test: double-emit filtering
# =====================================================================


class TestNoDoubleEmit:
    """Phase must NOT re-emit dep-policy violations from the gate phase."""

    @patch(_PATCH_CHECKS)
    def test_dep_check_failures_filtered_out(self, mock_checks):
        """Dep checks fail + target check fails -> only target violation emitted."""
        mock_checks.return_value = CIAuditResult(
            checks=[
                # These already ran in gate phase -- must be filtered
                CheckResult(
                    name="dependency-denylist",
                    passed=False,
                    message="Denied dep",
                    details=["evil/pkg"],
                ),
                CheckResult(
                    name="dependency-allowlist",
                    passed=False,
                    message="Not in allow list",
                    details=["unknown/pkg"],
                ),
                # This is the target check -- should be processed
                CheckResult(
                    name="compilation-target",
                    passed=False,
                    message="Target disallowed",
                    details=["target: claude, allowed: ['vscode']"],
                ),
            ]
        )
        ctx = _make_ctx(
            enforcement_active=True,
            policy_fetch=_make_policy_fetch(enforcement="warn"),
            manifest_target="claude",
        )

        run(ctx)  # warn mode -> no raise

        # Only one violation logged (the target one)
        assert ctx.logger.policy_violation.call_count == 1
        call_kwargs = ctx.logger.policy_violation.call_args
        assert call_kwargs[1]["dep_ref"] == "compilation-target"

    def test_target_check_ids_constant(self):
        """Sanity: TARGET_CHECK_IDS contains exactly the expected IDs."""
        assert frozenset({"compilation-target"}) == TARGET_CHECK_IDS


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
        assert call_kwargs[1]["effective_target"] == "vscode"


# =====================================================================
# Test: no logger (defensive)
# =====================================================================


class TestNoLogger:
    """Phase must not crash when ctx.logger is None."""

    @patch(_PATCH_CHECKS)
    def test_warn_mode_no_logger(self, mock_checks):
        """Violation in warn mode with logger=None -> no crash."""
        mock_checks.return_value = _target_failing_audit()
        ctx = _make_ctx(
            enforcement_active=True,
            policy_fetch=_make_policy_fetch(enforcement="warn"),
            manifest_target="claude",
        )
        ctx.logger = None

        run(ctx)  # should not raise or crash

    @patch(_PATCH_CHECKS)
    def test_block_mode_no_logger(self, mock_checks):
        """Violation in block mode with logger=None -> still raises."""
        mock_checks.return_value = _target_failing_audit()
        ctx = _make_ctx(
            enforcement_active=True,
            policy_fetch=_make_policy_fetch(enforcement="block"),
            manifest_target="claude",
        )
        ctx.logger = None

        with pytest.raises(PolicyViolationError):
            run(ctx)
