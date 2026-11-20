"""Unit tests for SecurityGate — centralized scan→classify→decide→report."""

import os  # noqa: F401
import textwrap  # noqa: F401
from unittest.mock import MagicMock

import pytest

from apm_cli.security.gate import (
    BLOCK_POLICY,
    REPORT_POLICY,
    WARN_POLICY,
    ScanPolicy,
    ScanVerdict,
    SecurityGate,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# U+E0100 is a VS17 variation selector — always "critical"
CRITICAL_CHAR = "\U000e0100"
# U+200B is a zero-width space — "warning"


# ---------------------------------------------------------------------------
# scan_files
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# scan_text
# ---------------------------------------------------------------------------


class TestScanText:
    def test_clean_text(self):
        v = SecurityGate.scan_text("clean content", "test.md")
        assert not v.has_findings

    def test_critical_text(self):
        v = SecurityGate.scan_text(f"evil {CRITICAL_CHAR}", "test.md", policy=BLOCK_POLICY)
        assert v.has_critical
        assert v.should_block

    def test_warn_policy_text(self):
        v = SecurityGate.scan_text(f"evil {CRITICAL_CHAR}", "test.md", policy=WARN_POLICY)
        assert v.has_critical
        assert not v.should_block


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


class TestReport:
    def test_no_findings_no_report(self):
        diag = MagicMock()
        v = ScanVerdict()
        SecurityGate.report(v, diag)
        diag.security.assert_not_called()

    def test_blocked_critical_reports(self):
        diag = MagicMock()
        v = ScanVerdict(
            findings_by_file={"x.md": [MagicMock(severity="critical")]},
            has_critical=True,
            should_block=True,
            critical_count=1,
            warning_count=0,
        )
        SecurityGate.report(v, diag, package="pkg")
        diag.security.assert_called_once()
        call_args = diag.security.call_args
        assert "Blocked" in call_args.kwargs.get("message", call_args[1].get("message", ""))

    def test_force_critical_reports_deployed(self):
        diag = MagicMock()
        v = ScanVerdict(
            findings_by_file={"x.md": [MagicMock(severity="critical")]},
            has_critical=True,
            should_block=False,
            critical_count=1,
            warning_count=0,
        )
        SecurityGate.report(v, diag, package="pkg", force=True)
        diag.security.assert_called_once()
        call_args = diag.security.call_args
        assert "force" in call_args.kwargs.get("message", call_args[1].get("message", "")).lower()
        # Should include actionable follow-up
        detail = call_args.kwargs.get("detail", call_args[1].get("detail", ""))
        assert "apm audit --strip" in detail

    def test_force_report_accepts_preflight_action(self):
        diag = MagicMock()
        verdict = ScanVerdict(
            findings_by_file={"decoded.yml": [MagicMock(severity="critical")]},
            has_critical=True,
            should_block=False,
            critical_count=1,
            warning_count=0,
        )

        SecurityGate.report(
            verdict,
            diag,
            package="pkg",
            force=True,
            force_action="Allowed by preflight",
            force_detail="Edit the escaped value in decoded.yml",
        )

        message = diag.security.call_args.kwargs["message"]
        assert message == "Allowed by preflight with --force despite critical hidden characters"
        assert diag.security.call_args.kwargs["detail"] == "Edit the escaped value in decoded.yml"

    def test_warning_only_reports(self):
        diag = MagicMock()
        v = ScanVerdict(
            findings_by_file={"x.md": [MagicMock(severity="warning")]},
            has_critical=False,
            should_block=False,
            critical_count=0,
            warning_count=2,
        )
        SecurityGate.report(v, diag, package="pkg")
        diag.security.assert_called_once()
        call_args = diag.security.call_args  # noqa: F841

    def test_warn_policy_critical_reports(self):
        """WARN_POLICY with critical findings must still record a diagnostic."""
        diag = MagicMock()
        v = ScanVerdict(
            findings_by_file={"x.md": [MagicMock(severity="critical")]},
            has_critical=True,
            should_block=False,
            critical_count=1,
            warning_count=0,
        )
        # force=False, should_block=False — the WARN_POLICY path
        SecurityGate.report(v, diag, package="pkg", force=False)
        diag.security.assert_called_once()
        call_args = diag.security.call_args
        assert call_args.kwargs.get("severity") == "critical"


# ---------------------------------------------------------------------------
# ScanVerdict properties
# ---------------------------------------------------------------------------


class TestScanVerdict:
    def test_all_findings_flattens(self):
        finding1 = MagicMock(severity="critical")
        finding2 = MagicMock(severity="warning")
        v = ScanVerdict(findings_by_file={"a": [finding1], "b": [finding2]})
        assert len(v.all_findings) == 2

    def test_has_findings_empty(self):
        assert not ScanVerdict().has_findings

    def test_has_findings_populated(self):
        v = ScanVerdict(findings_by_file={"a": [MagicMock()]})
        assert v.has_findings


# ---------------------------------------------------------------------------
# Policy presets
# ---------------------------------------------------------------------------


class TestPolicies:
    def test_block_policy(self):
        assert BLOCK_POLICY.on_critical == "block"
        assert BLOCK_POLICY.force_overrides is True

    def test_warn_policy(self):
        assert WARN_POLICY.on_critical == "warn"
        assert WARN_POLICY.force_overrides is False

    def test_report_policy(self):
        assert REPORT_POLICY.on_critical == "ignore"

    def test_custom_policy(self):
        p = ScanPolicy(on_critical="block", force_overrides=False)
        assert not p.force_overrides

    def test_effective_block_blocks_without_force(self):
        assert BLOCK_POLICY.effective_block(force=False) is True

    def test_effective_block_force_overrides(self):
        assert BLOCK_POLICY.effective_block(force=True) is False

    def test_effective_block_warn_never_blocks(self):
        assert WARN_POLICY.effective_block(force=False) is False

    def test_effective_block_no_force_override_ignores_force(self):
        p = ScanPolicy(on_critical="block", force_overrides=False)
        assert p.effective_block(force=True) is True


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_scan_verdict_is_frozen(self):
        v = ScanVerdict(has_critical=True, should_block=True)
        with pytest.raises(AttributeError):
            v.should_block = False  # type: ignore[misc]

    def test_scan_policy_is_frozen(self):
        with pytest.raises(AttributeError):
            BLOCK_POLICY.on_critical = "warn"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Robustness — unreadable files
# ---------------------------------------------------------------------------
