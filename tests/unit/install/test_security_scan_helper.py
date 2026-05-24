"""Unit tests for apm_cli.install.helpers.security_scan."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan
from apm_cli.utils.diagnostics import DiagnosticCollector

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_verdict(
    *,
    has_findings: bool = False,
    should_block: bool = False,
) -> MagicMock:
    verdict = MagicMock()
    verdict.has_findings = has_findings
    verdict.should_block = should_block
    return verdict


# SecurityGate is imported lazily inside _pre_deploy_security_scan from
# apm_cli.security.gate, so we patch at the source module.
_GATE_SCAN = "apm_cli.security.gate.SecurityGate.scan_files"
_GATE_REPORT = "apm_cli.security.gate.SecurityGate.report"


# ---------------------------------------------------------------------------
# _pre_deploy_security_scan
# ---------------------------------------------------------------------------


class TestPreDeploySecurityScan:
    """Tests for _pre_deploy_security_scan."""

    def test_returns_true_when_no_findings(self, tmp_path: Path) -> None:
        verdict = _make_verdict(has_findings=False)
        diag = DiagnosticCollector()

        with patch(_GATE_SCAN, return_value=verdict):
            result = _pre_deploy_security_scan(tmp_path, diag)

        assert result is True

    def test_returns_false_when_findings_and_should_block(self, tmp_path: Path) -> None:
        verdict = _make_verdict(has_findings=True, should_block=True)
        diag = DiagnosticCollector()

        with (
            patch(_GATE_SCAN, return_value=verdict),
            patch(_GATE_REPORT),
        ):
            result = _pre_deploy_security_scan(tmp_path, diag, package_name="my-pkg")

        assert result is False

    def test_returns_true_when_findings_but_not_blocking(self, tmp_path: Path) -> None:
        verdict = _make_verdict(has_findings=True, should_block=False)
        diag = DiagnosticCollector()

        with (
            patch(_GATE_SCAN, return_value=verdict),
            patch(_GATE_REPORT),
        ):
            result = _pre_deploy_security_scan(tmp_path, diag)

        assert result is True

    def test_calls_security_gate_report_when_findings(self, tmp_path: Path) -> None:
        verdict = _make_verdict(has_findings=True, should_block=False)
        diag = DiagnosticCollector()

        with (
            patch(_GATE_SCAN, return_value=verdict),
            patch(_GATE_REPORT) as mock_report,
        ):
            _pre_deploy_security_scan(tmp_path, diag, package_name="pkg", force=True)

        mock_report.assert_called_once_with(verdict, diag, package="pkg", force=True)

    def test_logger_error_called_when_blocking(self, tmp_path: Path) -> None:
        verdict = _make_verdict(has_findings=True, should_block=True)
        diag = DiagnosticCollector()
        logger = MagicMock()

        with (
            patch(_GATE_SCAN, return_value=verdict),
            patch(_GATE_REPORT),
        ):
            _pre_deploy_security_scan(tmp_path, diag, package_name="my-pkg", logger=logger)

        logger.error.assert_called_once()
        error_call = logger.error.call_args[0][0]
        assert "my-pkg" in error_call or "Blocked" in error_call

    def test_logger_tree_items_when_blocking(self, tmp_path: Path) -> None:
        verdict = _make_verdict(has_findings=True, should_block=True)
        diag = DiagnosticCollector()
        logger = MagicMock()

        with (
            patch(_GATE_SCAN, return_value=verdict),
            patch(_GATE_REPORT),
        ):
            _pre_deploy_security_scan(tmp_path, diag, logger=logger)

        assert logger.tree_item.call_count >= 2

    def test_no_logger_calls_when_logger_is_none(self, tmp_path: Path) -> None:
        """When logger=None, no AttributeError is raised."""
        verdict = _make_verdict(has_findings=True, should_block=True)
        diag = DiagnosticCollector()

        with (
            patch(_GATE_SCAN, return_value=verdict),
            patch(_GATE_REPORT),
        ):
            result = _pre_deploy_security_scan(tmp_path, diag, logger=None)

        assert result is False

    def test_force_forwarded_to_scan_and_report(self, tmp_path: Path) -> None:
        verdict = _make_verdict(has_findings=True, should_block=False)
        diag = DiagnosticCollector()

        with (
            patch(_GATE_SCAN, return_value=verdict) as mock_scan,
            patch(_GATE_REPORT) as mock_report,
        ):
            _pre_deploy_security_scan(tmp_path, diag, force=True)

        # scan_files called with force=True
        call_kwargs = mock_scan.call_args[1]
        assert call_kwargs.get("force") is True
        # report called with force=True
        report_kwargs = mock_report.call_args[1]
        assert report_kwargs.get("force") is True

    def test_block_policy_used_for_scan(self, tmp_path: Path) -> None:
        verdict = _make_verdict(has_findings=False)
        diag = DiagnosticCollector()

        with patch(_GATE_SCAN, return_value=verdict) as mock_scan:
            _pre_deploy_security_scan(tmp_path, diag)

        call_kwargs = mock_scan.call_args[1]
        assert "policy" in call_kwargs
