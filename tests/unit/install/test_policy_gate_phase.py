"""Unit tests for the policy_gate install pipeline phase.

Covers all 9 discovery outcomes end-to-end, enforcement modes
(block / warn / off), escape hatches (--no-policy, APM_POLICY_DISABLE=1),
and chain_refs threading to the cache writer.

Tests use synthetic InstallContext objects and patch discovery + policy
checks to isolate the phase logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from unittest.mock import MagicMock, call, patch

import pytest

from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy
from apm_cli.policy.models import CIAuditResult, CheckResult
from apm_cli.install.phases.policy_gate import PolicyViolationError, run

# Patch targets:
# _discover_with_chain is a module-level function in policy_gate
_PATCH_DISCOVER = "apm_cli.install.phases.policy_gate._discover_with_chain"
# run_dependency_policy_checks is imported inside run() from policy_checks
_PATCH_CHECKS = "apm_cli.policy.policy_checks.run_dependency_policy_checks"


# -- Minimal synthetic InstallContext ---------------------------------

@dataclass
class _FakeContext:
    """Minimal stand-in for InstallContext with only the fields policy_gate reads."""

    project_root: Path = field(default_factory=lambda: Path("/tmp/fake-project"))
    apm_dir: Path = field(default_factory=lambda: Path("/tmp/fake-project/.apm"))
    verbose: bool = False
    logger: Any = None
    deps_to_install: List[Any] = field(default_factory=list)
    existing_lockfile: Any = None

    # policy_gate fields
    policy_fetch: Any = None
    policy_enforcement_active: bool = False
    no_policy: bool = False


def _make_ctx(*, logger=None, no_policy=False, deps=None):
    """Build a _FakeContext with defaults."""
    return _FakeContext(
        logger=logger or MagicMock(),
        no_policy=no_policy,
        deps_to_install=deps or [],
    )


def _make_fetch_result(outcome, *, enforcement="warn", policy=None,
                       source="org:contoso/.github", cached=False,
                       cache_age_seconds=None, fetch_error=None,
                       error=None):
    """Build a PolicyFetchResult for the given outcome."""
    if policy is None and outcome in ("found", "cached_stale", "empty"):
        policy = ApmPolicy(enforcement=enforcement)
    return PolicyFetchResult(
        policy=policy,
        source=source,
        cached=cached,
        error=error,
        cache_age_seconds=cache_age_seconds,
        cache_stale=outcome == "cached_stale",
        fetch_error=fetch_error,
        outcome=outcome,
    )


def _passing_audit():
    """CIAuditResult with all checks passed."""
    return CIAuditResult(checks=[
        CheckResult(name="dependency-allowlist", passed=True, message="OK"),
    ])


def _failing_audit(*, name="dependency-denylist", message="Denied", details=None):
    """CIAuditResult with one failing check."""
    return CIAuditResult(checks=[
        CheckResult(
            name=name,
            passed=False,
            message=message,
            details=details or ["test-blocked/evil"],
        ),
    ])


# =====================================================================
# Test: escape hatches (--no-policy, APM_POLICY_DISABLE=1)
# =====================================================================


class TestEscapeHatches:
    """Phase noop with loud warning when policy is disabled."""

    def test_no_policy_flag_skips_phase(self):
        ctx = _make_ctx(no_policy=True)

        run(ctx)

        assert ctx.policy_fetch is None
        assert ctx.policy_enforcement_active is False
        ctx.logger.policy_disabled.assert_called_once_with("--no-policy")

    def test_env_var_disable_skips_phase(self, monkeypatch):
        monkeypatch.setenv("APM_POLICY_DISABLE", "1")
        ctx = _make_ctx()

        run(ctx)

        assert ctx.policy_fetch is None
        assert ctx.policy_enforcement_active is False
        ctx.logger.policy_disabled.assert_called_once_with("APM_POLICY_DISABLE=1")

    def test_env_var_not_set_does_not_skip(self, monkeypatch):
        """APM_POLICY_DISABLE absent or != '1' does not trigger escape."""
        monkeypatch.delenv("APM_POLICY_DISABLE", raising=False)
        fetch = _make_fetch_result("absent")

        with patch(
            "apm_cli.install.phases.policy_gate._discover_with_chain",
            return_value=fetch,
        ):
            ctx = _make_ctx()
            run(ctx)

        assert ctx.policy_fetch is not None
        assert ctx.policy_fetch.outcome == "absent"


# =====================================================================
# Test: all 9 discovery outcomes
# =====================================================================


class TestOutcomeFound:
    """outcome=found -> enforce per policy.enforcement."""

    @patch(_PATCH_CHECKS)
    @patch(_PATCH_DISCOVER)
    def test_found_warn_passing(self, mock_discover, mock_checks):
        mock_discover.return_value = _make_fetch_result("found", enforcement="warn")
        mock_checks.return_value = _passing_audit()
        ctx = _make_ctx()

        run(ctx)

        assert ctx.policy_enforcement_active is True
        assert ctx.policy_fetch.outcome == "found"
        mock_checks.assert_called_once()
        ctx.logger.policy_resolved.assert_called_once()

    @patch(_PATCH_CHECKS)
    @patch(_PATCH_DISCOVER)
    def test_found_block_passing(self, mock_discover, mock_checks):
        mock_discover.return_value = _make_fetch_result("found", enforcement="block")
        mock_checks.return_value = _passing_audit()
        ctx = _make_ctx()

        run(ctx)

        assert ctx.policy_enforcement_active is True

    @patch(_PATCH_CHECKS)
    @patch(_PATCH_DISCOVER)
    def test_found_off_skips_checks(self, mock_discover, mock_checks):
        mock_discover.return_value = _make_fetch_result("found", enforcement="off")
        ctx = _make_ctx()

        run(ctx)

        assert ctx.policy_enforcement_active is False
        mock_checks.assert_not_called()


class TestOutcomeAbsent:
    """outcome=absent -> info line, no enforcement."""

    @patch(_PATCH_DISCOVER)
    def test_absent_no_enforcement(self, mock_discover):
        mock_discover.return_value = _make_fetch_result("absent")
        ctx = _make_ctx()

        run(ctx)

        assert ctx.policy_enforcement_active is False
        assert ctx.policy_fetch.outcome == "absent"


class TestOutcomeNoGitRemote:
    """outcome=no_git_remote -> warning, no enforcement."""

    @patch(_PATCH_DISCOVER)
    def test_no_git_remote(self, mock_discover):
        mock_discover.return_value = _make_fetch_result(
            "no_git_remote", source=""
        )
        ctx = _make_ctx()

        run(ctx)

        assert ctx.policy_enforcement_active is False
        assert ctx.policy_fetch.outcome == "no_git_remote"


class TestOutcomeEmpty:
    """outcome=empty -> warning, no enforcement."""

    @patch(_PATCH_DISCOVER)
    def test_empty_policy(self, mock_discover):
        mock_discover.return_value = _make_fetch_result("empty")
        ctx = _make_ctx()

        run(ctx)

        assert ctx.policy_enforcement_active is False
        assert ctx.policy_fetch.outcome == "empty"


class TestOutcomeMalformed:
    """outcome=malformed -> sys.exit(1) (fail-closed)."""

    @patch(_PATCH_DISCOVER)
    def test_malformed_exits(self, mock_discover):
        mock_discover.return_value = _make_fetch_result(
            "malformed", policy=None, error="bad yaml"
        )

        ctx = _make_ctx()
        with pytest.raises(SystemExit) as exc_info:
            run(ctx)
        assert exc_info.value.code == 1


class TestOutcomeCacheMissFetchFail:
    """outcome=cache_miss_fetch_fail -> loud warn, no enforcement."""

    @patch(_PATCH_DISCOVER)
    def test_cache_miss_fetch_fail(self, mock_discover):
        mock_discover.return_value = _make_fetch_result(
            "cache_miss_fetch_fail",
            policy=None,
            fetch_error="Connection error",
        )
        ctx = _make_ctx()

        run(ctx)

        assert ctx.policy_enforcement_active is False
        assert ctx.policy_fetch.outcome == "cache_miss_fetch_fail"


class TestOutcomeGarbageResponse:
    """outcome=garbage_response -> loud warn, no enforcement."""

    @patch(_PATCH_DISCOVER)
    def test_garbage_response(self, mock_discover):
        mock_discover.return_value = _make_fetch_result(
            "garbage_response",
            policy=None,
            fetch_error="Not valid YAML",
        )
        ctx = _make_ctx()

        run(ctx)

        assert ctx.policy_enforcement_active is False


class TestOutcomeCachedStale:
    """outcome=cached_stale -> warn + enforcement still applies."""

    @patch(_PATCH_CHECKS)
    @patch(_PATCH_DISCOVER)
    def test_cached_stale_still_enforces(self, mock_discover, mock_checks):
        mock_discover.return_value = _make_fetch_result(
            "cached_stale",
            enforcement="block",
            cached=True,
            cache_age_seconds=7200,
            fetch_error="Timeout",
        )
        mock_checks.return_value = _passing_audit()
        ctx = _make_ctx()

        run(ctx)

        assert ctx.policy_enforcement_active is True
        mock_checks.assert_called_once()

    @patch(_PATCH_CHECKS)
    @patch(_PATCH_DISCOVER)
    def test_cached_stale_block_violation_raises(self, mock_discover, mock_checks):
        mock_discover.return_value = _make_fetch_result(
            "cached_stale", enforcement="block", cached=True
        )
        mock_checks.return_value = _failing_audit()
        ctx = _make_ctx()

        with pytest.raises(PolicyViolationError):
            run(ctx)


class TestOutcomeDisabled:
    """outcome=disabled -> noop (defensive path)."""

    @patch(_PATCH_DISCOVER)
    def test_disabled_outcome(self, mock_discover):
        mock_discover.return_value = _make_fetch_result("disabled", policy=None)
        ctx = _make_ctx()

        run(ctx)

        assert ctx.policy_enforcement_active is False


# =====================================================================
# Test: enforcement modes (block / warn / off)
# =====================================================================


class TestEnforcementBlock:
    """enforcement=block + denied dep -> phase raises PolicyViolationError."""

    @patch(_PATCH_CHECKS)
    @patch(_PATCH_DISCOVER)
    def test_block_denied_raises(self, mock_discover, mock_checks):
        mock_discover.return_value = _make_fetch_result("found", enforcement="block")
        mock_checks.return_value = _failing_audit()
        ctx = _make_ctx()

        with pytest.raises(PolicyViolationError, match="blocked by org policy"):
            run(ctx)

        # Violation routed through logger with severity="block"
        ctx.logger.policy_violation.assert_called_once()
        call_kwargs = ctx.logger.policy_violation.call_args
        assert call_kwargs[1]["severity"] == "block" or call_kwargs.kwargs.get("severity") == "block"


class TestEnforcementWarn:
    """enforcement=warn + denied dep -> warn diagnostic, does NOT raise."""

    @patch(_PATCH_CHECKS)
    @patch(_PATCH_DISCOVER)
    def test_warn_denied_does_not_raise(self, mock_discover, mock_checks):
        mock_discover.return_value = _make_fetch_result("found", enforcement="warn")
        mock_checks.return_value = _failing_audit()
        ctx = _make_ctx()

        # Should NOT raise
        run(ctx)

        assert ctx.policy_enforcement_active is True
        ctx.logger.policy_violation.assert_called_once()
        args, kwargs = ctx.logger.policy_violation.call_args
        assert kwargs.get("severity") == "warn"


class TestEnforcementOff:
    """enforcement=off + denied dep -> passes silently (verbose-only)."""

    @patch(_PATCH_CHECKS)
    @patch(_PATCH_DISCOVER)
    def test_off_skips_checks_entirely(self, mock_discover, mock_checks):
        mock_discover.return_value = _make_fetch_result("found", enforcement="off")
        ctx = _make_ctx()

        run(ctx)

        assert ctx.policy_enforcement_active is False
        mock_checks.assert_not_called()
        ctx.logger.policy_violation.assert_not_called()


# =====================================================================
# Test: chain_refs threading to cache writer
# =====================================================================


class TestChainRefs:
    """chain_refs are passed correctly to cache writer."""

    @patch("apm_cli.policy.discovery._write_cache")
    @patch("apm_cli.policy.discovery.discover_policy")
    def test_chain_refs_passed_on_extends(self, mock_discover, mock_write_cache):
        """When leaf policy has extends, _resolve_and_cache_chain should
        resolve the chain and call _write_cache with real chain_refs."""
        leaf_policy = ApmPolicy(
            name="leaf",
            enforcement="warn",
            extends="parent-org/.github",
        )
        leaf_fetch = PolicyFetchResult(
            policy=leaf_policy,
            source="org:contoso/.github",
            outcome="found",
            cached=False,
        )

        parent_policy = ApmPolicy(
            name="parent",
            enforcement="block",
            dependencies=DependencyPolicy(deny=("evil/*",)),
        )
        parent_fetch = PolicyFetchResult(
            policy=parent_policy,
            source="org:parent-org/.github",
            outcome="found",
        )

        # First call returns the leaf; second call (for parent) returns parent
        mock_discover.side_effect = [leaf_fetch, parent_fetch]

        ctx = _make_ctx()
        from apm_cli.install.phases.policy_gate import _discover_with_chain
        result = _discover_with_chain(ctx)

        # _write_cache should have been called with chain_refs covering both
        assert mock_write_cache.called
        call_kwargs = mock_write_cache.call_args
        chain_refs = call_kwargs.kwargs.get("chain_refs") or call_kwargs[1].get("chain_refs")
        assert chain_refs is not None
        assert len(chain_refs) == 2, f"Expected 2 chain_refs, got {chain_refs}"
        # Parent should be first, leaf second
        assert "parent-org/.github" in chain_refs[0]
        assert "contoso/.github" in chain_refs[1]

    @patch("apm_cli.policy.discovery._write_cache")
    @patch("apm_cli.policy.discovery.discover_policy")
    def test_no_extends_no_chain_resolution(self, mock_discover, mock_write_cache):
        """Without extends, no chain resolution or re-caching happens."""
        policy = ApmPolicy(name="simple", enforcement="warn")
        fetch = PolicyFetchResult(
            policy=policy,
            source="org:contoso/.github",
            outcome="found",
            cached=False,
        )
        mock_discover.return_value = fetch

        ctx = _make_ctx()
        from apm_cli.install.phases.policy_gate import _discover_with_chain
        result = _discover_with_chain(ctx)

        # _write_cache should NOT be called by _discover_with_chain
        # (it's already cached by discover_policy itself)
        mock_write_cache.assert_not_called()

    @patch("apm_cli.policy.discovery._write_cache")
    @patch("apm_cli.policy.discovery.discover_policy")
    def test_cached_result_skips_chain_resolution(self, mock_discover, mock_write_cache):
        """When result is from cache, skip re-resolution."""
        policy = ApmPolicy(name="cached", enforcement="warn", extends="org")
        fetch = PolicyFetchResult(
            policy=policy,
            source="org:contoso/.github",
            outcome="found",
            cached=True,  # served from cache
        )
        mock_discover.return_value = fetch

        ctx = _make_ctx()
        from apm_cli.install.phases.policy_gate import _discover_with_chain
        result = _discover_with_chain(ctx)

        mock_write_cache.assert_not_called()


# =====================================================================
# Test: severity literal is "warn" not "warning" (C1 amendment)
# =====================================================================


class TestSeverityLiteral:
    """C1 amendment: severity MUST be 'warn' (not 'warning')."""

    @patch(_PATCH_CHECKS)
    @patch(_PATCH_DISCOVER)
    def test_warn_severity_is_literal_warn(self, mock_discover, mock_checks):
        mock_discover.return_value = _make_fetch_result("found", enforcement="warn")
        mock_checks.return_value = _failing_audit()
        ctx = _make_ctx()

        run(ctx)

        _, kwargs = ctx.logger.policy_violation.call_args
        assert kwargs["severity"] == "warn", (
            f"Expected severity='warn', got '{kwargs['severity']}'"
        )

    @patch(_PATCH_CHECKS)
    @patch(_PATCH_DISCOVER)
    def test_block_severity_is_literal_block(self, mock_discover, mock_checks):
        mock_discover.return_value = _make_fetch_result("found", enforcement="block")
        mock_checks.return_value = _failing_audit()
        ctx = _make_ctx()

        with pytest.raises(PolicyViolationError):
            run(ctx)

        _, kwargs = ctx.logger.policy_violation.call_args
        assert kwargs["severity"] == "block"


# =====================================================================
# Test: multiple violations (block + warn mix)
# =====================================================================


class TestMultipleViolations:

    @patch(_PATCH_CHECKS)
    @patch(_PATCH_DISCOVER)
    def test_block_mode_multiple_violations_raises(self, mock_discover, mock_checks):
        mock_discover.return_value = _make_fetch_result("found", enforcement="block")
        mock_checks.return_value = CIAuditResult(checks=[
            CheckResult(name="dep-allow", passed=False, message="Not allowed",
                        details=["acme/evil"]),
            CheckResult(name="dep-deny", passed=False, message="Denied",
                        details=["acme/banned"]),
            CheckResult(name="dep-require", passed=True, message="OK"),
        ])
        ctx = _make_ctx()

        with pytest.raises(PolicyViolationError):
            run(ctx)

        # Both failing checks should be routed to logger
        assert ctx.logger.policy_violation.call_count == 2

    @patch(_PATCH_CHECKS)
    @patch(_PATCH_DISCOVER)
    def test_warn_mode_multiple_violations_continues(self, mock_discover, mock_checks):
        mock_discover.return_value = _make_fetch_result("found", enforcement="warn")
        mock_checks.return_value = CIAuditResult(checks=[
            CheckResult(name="dep-allow", passed=False, message="Not allowed",
                        details=["acme/evil"]),
            CheckResult(name="dep-deny", passed=False, message="Denied",
                        details=["acme/banned"]),
        ])
        ctx = _make_ctx()

        # Should NOT raise
        run(ctx)

        assert ctx.logger.policy_violation.call_count == 2
        assert ctx.policy_enforcement_active is True


# =====================================================================
# Test: run_dependency_policy_checks receives correct arguments
# =====================================================================


class TestCheckInvocation:

    @patch(_PATCH_CHECKS)
    @patch(_PATCH_DISCOVER)
    def test_check_receives_deps_and_policy(self, mock_discover, mock_checks):
        policy = ApmPolicy(
            enforcement="warn",
            dependencies=DependencyPolicy(deny=("evil/*",)),
        )
        mock_discover.return_value = PolicyFetchResult(
            policy=policy,
            source="org:contoso/.github",
            outcome="found",
        )
        mock_checks.return_value = _passing_audit()

        fake_deps = [MagicMock(), MagicMock()]
        ctx = _make_ctx(deps=fake_deps)

        run(ctx)

        mock_checks.assert_called_once()
        call_args = mock_checks.call_args
        # Positional: deps_to_install
        assert call_args[0][0] is fake_deps
        # Keyword: policy is the effective merged policy
        assert call_args[1]["policy"] is policy
        assert call_args[1]["effective_target"] is None  # pre-targets
        assert call_args[1]["fetch_outcome"] == "found"


# =====================================================================
# Test: no logger graceful handling
# =====================================================================


class TestNoLogger:
    """Phase should not crash when logger is None."""

    @patch(_PATCH_DISCOVER)
    def test_absent_without_logger(self, mock_discover):
        mock_discover.return_value = _make_fetch_result("absent")
        ctx = _make_ctx()
        ctx.logger = None

        run(ctx)  # Should not raise

        assert ctx.policy_enforcement_active is False
