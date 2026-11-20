"""Dry-run no-policy and noise-cap rendering."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional  # noqa: F401
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.command_logger import InstallLogger
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.install_preflight import (
    run_policy_preflight,
)
from apm_cli.policy.models import CheckResult, CIAuditResult
from apm_cli.policy.schema import ApmPolicy

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-prospective-dry-run-install-plan")]

# -- Fixtures / helpers ---------------------------------------------------


def _make_fetch_result(
    policy: ApmPolicy | None = None,
    outcome: str = "found",
    source: str = "org:test-org/.github",
) -> PolicyFetchResult:
    """Build a PolicyFetchResult for testing."""
    return PolicyFetchResult(
        policy=policy,
        source=source,
        cached=False,
        outcome=outcome,
    )


def _make_dep(repo_url: str, reference: str = "main"):
    """Build a minimal DependencyReference-like object for policy checks.

    The mock provides the canonical identity methods and case-prefix metadata
    consumed by policy checks.
    """
    dep = MagicMock()
    dep.repo_url = repo_url
    dep.reference = reference
    dep.case_insensitive_identity_prefix_segments = 2
    dep.get_unique_key.return_value = repo_url
    dep.get_canonical_dependency_string.return_value = repo_url
    return dep


def _mock_logger() -> MagicMock:
    """Build a MagicMock logger with InstallLogger interface."""
    logger = MagicMock(spec=InstallLogger)
    logger.verbose = False
    logger.dry_run = True
    return logger


# ==========================================================================
# Test 1: denied dep + block mode -> "Would be blocked"; no raise; exit 0
# ==========================================================================


# Reaching here without exception proves exit 0 contract


# ==========================================================================
# Test 2: required-missing dep + block mode -> mentions required-missing
# ==========================================================================


# ==========================================================================
# Test 3: allowed deps -> no policy verdict shown; clean dry-run output
# ==========================================================================


# ==========================================================================
# Test 4: --no-policy -> policy preflight skipped
# ==========================================================================


class TestDryRunNoPolicy:
    """apm install --dry-run --no-policy -> skips policy entirely."""

    def test_no_policy_skips_discovery(self):
        logger = _mock_logger()

        with patch(
            "apm_cli.policy.install_preflight.discover_policy_with_chain",
        ) as mock_discover:
            result_fetch, result_active = run_policy_preflight(
                project_root=Path("/fake"),
                apm_deps=[_make_dep("anything/dep")],
                no_policy=True,
                logger=logger,
                dry_run=True,
            )

        # Discovery never called
        mock_discover.assert_not_called()
        assert result_fetch is None
        assert result_active is False

        # logger.policy_disabled was called
        logger.policy_disabled.assert_called_once()

    def test_env_var_skips_discovery(self):
        """APM_POLICY_DISABLE=1 also skips discovery in dry-run."""
        logger = _mock_logger()

        with (
            patch(
                "apm_cli.policy.install_preflight.discover_policy_with_chain",
            ) as mock_discover,
            patch.dict(os.environ, {"APM_POLICY_DISABLE": "1"}),
        ):
            result_fetch, result_active = run_policy_preflight(
                project_root=Path("/fake"),
                apm_deps=[_make_dep("anything/dep")],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )

        mock_discover.assert_not_called()
        assert result_fetch is None
        assert result_active is False
        logger.policy_disabled.assert_called_once()


# ==========================================================================
# Test 5: install <denied-pkg> --dry-run -> would-be-block + no mutation
# ==========================================================================


# ==========================================================================
# Test 6: install --mcp <denied> --dry-run -> preview block message
# ==========================================================================


# ==========================================================================
# Warn-severity dry-run tests
# ==========================================================================


# ==========================================================================
# Backward compatibility: dry_run=False default
# ==========================================================================


# ==========================================================================
# D2: Dry-run noise cap tests
# ==========================================================================


class TestDryRunNoiseCap:
    """D2: dry-run preview capped at _DRY_RUN_PREVIEW_LIMIT per bucket."""

    def _make_check_result(self, n_details: int, check_name: str = "deny-list"):
        """Build a CheckResult naming *n_details* denied dependencies."""
        from apm_cli.policy.models import PolicySubject

        subjects = tuple(
            PolicySubject(f"pkg-{i}", "denied by policy") for i in range(1, n_details + 1)
        )
        return CheckResult(
            name=check_name,
            passed=False,
            message=f"{n_details} deps denied",
            details=[f"{s.ref}: {s.reason}" for s in subjects],
            subjects=subjects,
        )

    def _make_failing_audit(self, n_details: int):
        """Build a CIAuditResult with a single failing check."""
        check = self._make_check_result(n_details)
        return CIAuditResult(checks=[check])

    def test_six_denied_shows_five_plus_tail(self):
        """6 denied deps -> 5 lines + 1 tail line 'and 1 more would be blocked'."""
        from apm_cli.policy.install_preflight import _DRY_RUN_PREVIEW_LIMIT

        assert _DRY_RUN_PREVIEW_LIMIT == 5

        policy = ApmPolicy(enforcement="block")
        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()
        audit = self._make_failing_audit(6)

        with (
            patch(
                "apm_cli.policy.install_preflight.discover_policy_with_chain",
                return_value=fetch_result,
            ),
            patch(
                "apm_cli.policy.install_preflight.run_dependency_policy_checks",
                return_value=audit,
            ),
        ):
            run_policy_preflight(
                project_root=Path("/fake"),
                apm_deps=[_make_dep("anything/dep")],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )

        warning_msgs = [str(c) for c in logger.warning.call_args_list]

        # 5 "Would be blocked" lines
        blocked_lines = [m for m in warning_msgs if "Would be blocked by policy" in m]
        assert len(blocked_lines) == 5, (
            f"Expected 5 blocked lines, got {len(blocked_lines)}: {blocked_lines}"
        )

        # 1 tail line
        tail_lines = [m for m in warning_msgs if "and 1 more would be blocked" in m]
        assert len(tail_lines) == 1, f"Expected 1 tail line, got {len(tail_lines)}: {tail_lines}"

        # Tail mentions apm audit
        assert any("apm audit" in m for m in tail_lines)

    def test_five_denied_no_tail(self):
        """5 denied deps -> 5 lines + NO tail line."""
        policy = ApmPolicy(enforcement="block")
        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()
        audit = self._make_failing_audit(5)

        with (
            patch(
                "apm_cli.policy.install_preflight.discover_policy_with_chain",
                return_value=fetch_result,
            ),
            patch(
                "apm_cli.policy.install_preflight.run_dependency_policy_checks",
                return_value=audit,
            ),
        ):
            run_policy_preflight(
                project_root=Path("/fake"),
                apm_deps=[_make_dep("anything/dep")],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )

        warning_msgs = [str(c) for c in logger.warning.call_args_list]

        blocked_lines = [m for m in warning_msgs if "Would be blocked by policy" in m]
        assert len(blocked_lines) == 5

        # No tail
        tail_lines = [m for m in warning_msgs if "more would be blocked" in m]
        assert len(tail_lines) == 0, f"Should be no tail for exactly 5, got: {tail_lines}"

    def test_ten_denied_ten_warn_separate_buckets(self):
        """10 deny + 10 warn -> 5 deny + 1 deny-tail + 5 warn + 1 warn-tail.

        Since enforcement is policy-level, we run two preflight calls:
        one with block enforcement, one with warn enforcement.
        """
        # --- Block bucket ---
        block_policy = ApmPolicy(enforcement="block")
        block_fetch = _make_fetch_result(policy=block_policy)
        block_logger = _mock_logger()
        block_audit = self._make_failing_audit(10)

        with (
            patch(
                "apm_cli.policy.install_preflight.discover_policy_with_chain",
                return_value=block_fetch,
            ),
            patch(
                "apm_cli.policy.install_preflight.run_dependency_policy_checks",
                return_value=block_audit,
            ),
        ):
            run_policy_preflight(
                project_root=Path("/fake"),
                apm_deps=[_make_dep("x/y")],
                no_policy=False,
                logger=block_logger,
                dry_run=True,
            )

        block_msgs = [str(c) for c in block_logger.warning.call_args_list]
        assert len([m for m in block_msgs if "Would be blocked by policy" in m]) == 5
        assert len([m for m in block_msgs if "and 5 more would be blocked" in m]) == 1

        # --- Warn bucket ---
        warn_policy = ApmPolicy(enforcement="warn")
        warn_fetch = _make_fetch_result(policy=warn_policy)
        warn_logger = _mock_logger()
        warn_audit = self._make_failing_audit(10)

        with (
            patch(
                "apm_cli.policy.install_preflight.discover_policy_with_chain",
                return_value=warn_fetch,
            ),
            patch(
                "apm_cli.policy.install_preflight.run_dependency_policy_checks",
                return_value=warn_audit,
            ),
        ):
            run_policy_preflight(
                project_root=Path("/fake"),
                apm_deps=[_make_dep("x/y")],
                no_policy=False,
                logger=warn_logger,
                dry_run=True,
            )

        warn_msgs = [str(c) for c in warn_logger.warning.call_args_list]
        assert (
            len([m for m in warn_msgs if "Policy warning" in m and "more policy warnings" not in m])
            == 5
        )
        assert len([m for m in warn_msgs if "and 5 more policy warnings" in m]) == 1

    def test_tail_wording_is_ascii_and_mentions_apm_audit(self):
        """Tail lines are pure ASCII and mention 'apm audit'."""
        policy = ApmPolicy(enforcement="block")
        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()
        audit = self._make_failing_audit(8)

        with (
            patch(
                "apm_cli.policy.install_preflight.discover_policy_with_chain",
                return_value=fetch_result,
            ),
            patch(
                "apm_cli.policy.install_preflight.run_dependency_policy_checks",
                return_value=audit,
            ),
        ):
            run_policy_preflight(
                project_root=Path("/fake"),
                apm_deps=[_make_dep("x/y")],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )

        warning_msgs = [str(c) for c in logger.warning.call_args_list]
        tail_lines = [m for m in warning_msgs if "more would be blocked" in m]
        assert len(tail_lines) == 1

        tail = tail_lines[0]
        # ASCII only
        assert all(ord(ch) < 128 for ch in tail), f"Tail line contains non-ASCII: {tail!r}"
        # Mentions apm audit
        assert "apm audit" in tail
        # Correct overflow count (8 - 5 = 3)
        assert "and 3 more" in tail
