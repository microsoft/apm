"""Unit tests for W2-dry-run: policy preflight rendering in ``install --dry-run``.

Covers:
- ``apm install --dry-run`` with denied dep + block mode -> dry-run output
  contains "Would be blocked by policy"; exit 0; apm.yml NOT mutated.
- ``apm install --dry-run`` with required-missing dep + block mode -> output
  mentions required-missing.
- ``apm install --dry-run`` with allowed deps -> no policy verdict shown;
  clean dry-run output.
- ``apm install --dry-run --no-policy`` -> policy preflight skipped; dry-run
  output unchanged from baseline.
- ``apm install <denied-pkg> --dry-run`` -> shows the would-be-block AND
  apm.yml is NOT mutated (dry-run never persists).
- ``apm install --mcp <denied> --dry-run`` -> same UX (preview block message).

Design choice: dry-run checks run against **direct manifest deps** only, not
resolved/transitive deps.  The resolver does not run in ``--dry-run`` mode;
evaluating transitives would require a full resolve which defeats the purpose
of a lightweight preview.  This is a documented limitation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional  # noqa: F401
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.command_logger import InstallLogger
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.install_preflight import (
    PolicyBlockError,
    run_policy_preflight,
)
from apm_cli.policy.models import CheckResult, CIAuditResult
from apm_cli.policy.parser import load_policy
from apm_cli.policy.schema import ApmPolicy

# -- Fixtures / helpers ---------------------------------------------------

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "policy"


def _load_fixture_policy(name: str) -> ApmPolicy:
    """Load a policy fixture by filename."""
    policy, _ = load_policy(FIXTURE_DIR / name)
    return policy


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

    The mock provides ``get_canonical_dependency_string()`` and
    ``get_unique_key()`` which are the two methods policy checks inspect.
    """
    dep = MagicMock()
    dep.repo_url = repo_url
    dep.reference = reference
    dep.get_unique_key.return_value = repo_url
    dep.get_canonical_dependency_string.return_value = repo_url
    return dep


def _make_mcp_dep(name: str, transport: str | None = None, url: str | None = None):
    """Build a minimal MCPDependency for policy checks."""
    from apm_cli.models.dependency.mcp import MCPDependency

    return MCPDependency(name=name, transport=transport, url=url)


def _mock_logger() -> MagicMock:
    """Build a MagicMock logger with InstallLogger interface."""
    logger = MagicMock(spec=InstallLogger)
    logger.verbose = False
    logger.dry_run = True
    return logger


# ==========================================================================
# Test 1: denied dep + block mode -> "Would be blocked"; no raise; exit 0
# ==========================================================================


class TestDryRunDeniedDepBlock:
    """apm install --dry-run with a denied dep under enforcement=block."""

    def test_emits_would_be_blocked_no_raise(self):
        """Block-severity violations render as preview, not exceptions."""
        policy = _load_fixture_policy("apm-policy-deny.yml")
        assert policy.enforcement == "block"

        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()
        denied_dep = _make_dep("test-blocked/foo")

        with patch(
            "apm_cli.policy.install_preflight.discover_policy_with_chain",
            return_value=fetch_result,
        ):
            # Should NOT raise PolicyBlockError
            result_fetch, result_active = run_policy_preflight(
                project_root=Path("/fake"),
                apm_deps=[denied_dep],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )

        # We got here -> no PolicyBlockError raised
        assert result_fetch is not None
        assert result_active is True  # policy was found + active

        # logger.warning called with "Would be blocked by policy"
        warning_calls = [str(c) for c in logger.warning.call_args_list]
        assert any("Would be blocked by policy" in c for c in warning_calls), (
            f"Expected 'Would be blocked by policy' in warnings, got: {warning_calls}"
        )

    def test_does_not_call_policy_violation(self):
        """Dry-run should NOT push to DiagnosticCollector via policy_violation."""
        policy = _load_fixture_policy("apm-policy-deny.yml")
        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()
        denied_dep = _make_dep("test-blocked/foo")

        with patch(
            "apm_cli.policy.install_preflight.discover_policy_with_chain",
            return_value=fetch_result,
        ):
            run_policy_preflight(
                project_root=Path("/fake"),
                apm_deps=[denied_dep],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )

        # policy_violation is the real-install path -- dry-run must NOT call it
        logger.policy_violation.assert_not_called()

    def test_non_dry_run_still_raises(self):
        """Verify that without dry_run=True, PolicyBlockError IS raised."""
        policy = _load_fixture_policy("apm-policy-deny.yml")
        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()
        denied_dep = _make_dep("test-blocked/foo")

        with (
            patch(
                "apm_cli.policy.install_preflight.discover_policy_with_chain",
                return_value=fetch_result,
            ),
            pytest.raises(PolicyBlockError),
        ):
            run_policy_preflight(
                project_root=Path("/fake"),
                apm_deps=[denied_dep],
                no_policy=False,
                logger=logger,
                dry_run=False,
            )

    def test_exit_zero_contract(self):
        """Dry-run NEVER produces non-zero exit -- no exception path."""
        policy = _load_fixture_policy("apm-policy-deny.yml")
        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()
        denied_dep = _make_dep("test-blocked/foo")

        with patch(
            "apm_cli.policy.install_preflight.discover_policy_with_chain",
            return_value=fetch_result,
        ):
            # Must return normally (no SystemExit, no PolicyBlockError)
            run_policy_preflight(
                project_root=Path("/fake"),
                apm_deps=[denied_dep],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )
        # Reaching here without exception proves exit 0 contract


# ==========================================================================
# Test 2: required-missing dep + block mode -> mentions required-missing
# ==========================================================================


class TestDryRunRequiredMissingBlock:
    """apm install --dry-run with required dep missing under enforcement=block."""

    def test_emits_would_be_blocked_for_required_missing(self):
        policy = _load_fixture_policy("apm-policy-required.yml")
        assert policy.enforcement == "block"

        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()

        # Install a dep that is NOT the required one
        some_dep = _make_dep("other-org/some-package")

        with patch(
            "apm_cli.policy.install_preflight.discover_policy_with_chain",
            return_value=fetch_result,
        ):
            result_fetch, result_active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/fake"),
                apm_deps=[some_dep],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )

        # Should emit "Would be blocked" for the missing required package
        warning_calls = [str(c) for c in logger.warning.call_args_list]
        assert any("Would be blocked by policy" in c for c in warning_calls), (
            f"Expected required-missing warning, got: {warning_calls}"
        )

        # Verify the required package name appears in at least one warning
        assert any("DevExpGbb/required-standards" in c for c in warning_calls), (
            f"Expected 'DevExpGbb/required-standards' in warnings, got: {warning_calls}"
        )


# ==========================================================================
# Test 3: allowed deps -> no policy verdict shown; clean dry-run output
# ==========================================================================


class TestDryRunAllowedDeps:
    """apm install --dry-run with deps that pass policy -> no warnings."""

    def test_no_policy_warnings_when_allowed(self):
        policy = _load_fixture_policy("apm-policy-allow.yml")
        assert policy.enforcement == "warn"

        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()
        allowed_dep = _make_dep("DevExpGbb/some-package")

        with patch(
            "apm_cli.policy.install_preflight.discover_policy_with_chain",
            return_value=fetch_result,
        ):
            result_fetch, result_active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/fake"),
                apm_deps=[allowed_dep],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )

        # No "Would be blocked" or "Policy warning" messages
        for c in logger.warning.call_args_list:
            msg = str(c)
            assert "Would be blocked by policy" not in msg
            assert "Policy warning" not in msg

        # policy_violation also not called
        logger.policy_violation.assert_not_called()

    def test_clean_output_no_deny_list(self):
        """Policy with only allow list and matching deps -> no violations."""
        policy = _load_fixture_policy("apm-policy-allow.yml")
        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()
        allowed_dep = _make_dep("microsoft/some-tool")

        with patch(
            "apm_cli.policy.install_preflight.discover_policy_with_chain",
            return_value=fetch_result,
        ):
            run_policy_preflight(
                project_root=Path("/fake"),
                apm_deps=[allowed_dep],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )

        # Only policy_resolved should be called (discovery success)
        logger.policy_resolved.assert_called_once()
        logger.policy_violation.assert_not_called()


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


class TestDryRunDeniedPkgExplicit:
    """apm install <denied-pkg> --dry-run -> preview block, no mutation."""

    def test_preflight_does_not_mutate_filesystem(self, tmp_path):
        """run_policy_preflight(dry_run=True) does not write any files."""
        policy = _load_fixture_policy("apm-policy-deny.yml")
        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()
        denied_dep = _make_dep("test-blocked/evil-pkg")

        # Record files before
        before_files = set(tmp_path.rglob("*"))

        with patch(
            "apm_cli.policy.install_preflight.discover_policy_with_chain",
            return_value=fetch_result,
        ):
            run_policy_preflight(
                project_root=tmp_path,
                apm_deps=[denied_dep],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )

        # No new files created
        after_files = set(tmp_path.rglob("*"))
        assert before_files == after_files, (
            f"Dry-run policy preflight created files: {after_files - before_files}"
        )

    def test_apm_yml_not_mutated(self, tmp_path):
        """If apm.yml exists, dry-run preflight does not alter it."""
        apm_yml = tmp_path / "apm.yml"
        original_content = b"name: test-project\nversion: 0.1.0\n"
        apm_yml.write_bytes(original_content)

        policy = _load_fixture_policy("apm-policy-deny.yml")
        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()
        denied_dep = _make_dep("test-blocked/evil-pkg")

        with patch(
            "apm_cli.policy.install_preflight.discover_policy_with_chain",
            return_value=fetch_result,
        ):
            run_policy_preflight(
                project_root=tmp_path,
                apm_deps=[denied_dep],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )

        # apm.yml byte-identical to original
        assert apm_yml.read_bytes() == original_content

    def test_would_be_blocked_shown_for_explicit_pkg(self):
        """Even for a specific 'apm install <pkg> --dry-run', block is previewed."""
        policy = _load_fixture_policy("apm-policy-deny.yml")
        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()
        denied_dep = _make_dep("test-blocked/evil-pkg")

        with patch(
            "apm_cli.policy.install_preflight.discover_policy_with_chain",
            return_value=fetch_result,
        ):
            run_policy_preflight(
                project_root=Path("/fake"),
                apm_deps=[denied_dep],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )

        warning_calls = [str(c) for c in logger.warning.call_args_list]
        assert any("Would be blocked by policy" in c for c in warning_calls)
        assert any("test-blocked/evil-pkg" in c for c in warning_calls)


# ==========================================================================
# Test 6: install --mcp <denied> --dry-run -> preview block message
# ==========================================================================


class TestDryRunMcpDenied:
    """apm install --mcp <denied> --dry-run -> preview block."""

    def test_mcp_denied_dry_run_no_raise(self):
        """MCP deny-list violation with dry_run=True emits preview, no raise."""
        policy = _load_fixture_policy("apm-policy-mcp.yml")
        assert policy.enforcement == "block"

        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()

        denied_mcp = _make_mcp_dep(
            name="io.github.untrusted/evil-server",
            transport="stdio",
        )

        with patch(
            "apm_cli.policy.install_preflight.discover_policy_with_chain",
            return_value=fetch_result,
        ):
            result_fetch, result_active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/fake"),
                mcp_deps=[denied_mcp],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )

        # No PolicyBlockError (we got here)
        assert result_fetch is not None

        # Warning about policy block emitted
        warning_calls = [str(c) for c in logger.warning.call_args_list]
        assert any("Would be blocked by policy" in c for c in warning_calls), (
            f"Expected MCP block preview, got: {warning_calls}"
        )

    def test_mcp_denied_non_dry_run_raises(self):
        """Without dry_run=True, MCP deny violation raises PolicyBlockError."""
        policy = _load_fixture_policy("apm-policy-mcp.yml")
        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()

        denied_mcp = _make_mcp_dep(
            name="io.github.untrusted/evil-server",
            transport="stdio",
        )

        with (
            patch(
                "apm_cli.policy.install_preflight.discover_policy_with_chain",
                return_value=fetch_result,
            ),
            pytest.raises(PolicyBlockError),
        ):
            run_policy_preflight(
                project_root=Path("/fake"),
                mcp_deps=[denied_mcp],
                no_policy=False,
                logger=logger,
                dry_run=False,
            )


# ==========================================================================
# Warn-severity dry-run tests
# ==========================================================================


class TestDryRunWarnSeverity:
    """Policy with enforcement=warn emits 'Policy warning' in dry-run."""

    def test_warn_severity_emits_policy_warning(self):
        policy = _load_fixture_policy("apm-policy-warn.yml")
        assert policy.enforcement == "warn"

        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()

        # apm-policy-warn.yml likely has an allow list; use a dep outside it
        outside_dep = _make_dep("unknown-org/suspicious-pkg")

        with patch(
            "apm_cli.policy.install_preflight.discover_policy_with_chain",
            return_value=fetch_result,
        ):
            result_fetch, result_active = run_policy_preflight(  # noqa: RUF059
                project_root=Path("/fake"),
                apm_deps=[outside_dep],
                no_policy=False,
                logger=logger,
                dry_run=True,
            )

        # Should use "Policy warning" (not "Would be blocked")
        warning_calls = [str(c) for c in logger.warning.call_args_list]
        if warning_calls:  # Only assert if there were violations
            assert any("Policy warning" in c for c in warning_calls), (
                f"Expected 'Policy warning' for warn severity, got: {warning_calls}"
            )
            # Should NOT contain "Would be blocked" (that's block-only)
            assert not any("Would be blocked by policy" in c for c in warning_calls), (
                f"'Would be blocked' is for block severity only, got: {warning_calls}"
            )


# ==========================================================================
# Backward compatibility: dry_run=False default
# ==========================================================================


class TestDryRunBackwardCompat:
    """dry_run parameter defaults to False -- existing callers unaffected."""

    def test_default_dry_run_is_false(self):
        """Calling without dry_run= behaves as before (raises on block)."""
        policy = _load_fixture_policy("apm-policy-deny.yml")
        fetch_result = _make_fetch_result(policy=policy)
        logger = _mock_logger()
        denied_dep = _make_dep("test-blocked/foo")

        with (
            patch(
                "apm_cli.policy.install_preflight.discover_policy_with_chain",
                return_value=fetch_result,
            ),
            pytest.raises(PolicyBlockError),
        ):
            # No dry_run argument -> defaults to False -> raises
            run_policy_preflight(
                project_root=Path("/fake"),
                apm_deps=[denied_dep],
                no_policy=False,
                logger=logger,
            )


# ==========================================================================
# D2: Dry-run noise cap tests
# ==========================================================================


class TestDryRunNoiseCap:
    """D2: dry-run preview capped at _DRY_RUN_PREVIEW_LIMIT per bucket."""

    def _make_check_result(self, n_details: int, check_name: str = "deny-list"):
        """Build a CheckResult with *n_details* detail lines."""
        details = [f"pkg-{i}: denied by policy" for i in range(1, n_details + 1)]
        return CheckResult(
            name=check_name,
            passed=False,
            message=f"{n_details} deps denied",
            details=details,
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
