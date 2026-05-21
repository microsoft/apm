"""Integration tests for apm audit command coverage."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.commands.audit import (
    _audit_outcome_cause,
    _AuditConfig,
    _has_actionable_findings,
    _scan_single_file,
)
from apm_cli.core.command_logger import CommandLogger
from apm_cli.security.content_scanner import ScanFinding


class TestAuditConfig:
    """Tests for _AuditConfig dataclass."""

    def test_create_audit_config(self, tmp_path: Path):
        """Create _AuditConfig with required fields."""
        logger = CommandLogger("test")

        config = _AuditConfig(
            project_root=tmp_path,
            logger=logger,
            verbose=False,
            output_format="text",
            output_path=None,
        )

        assert config.project_root == tmp_path
        assert config.logger == logger
        assert config.verbose is False
        assert config.output_format == "text"
        assert config.output_path is None

    def test_audit_config_with_output_path(self, tmp_path: Path):
        """Create _AuditConfig with output path."""
        logger = CommandLogger("test")
        output_file = tmp_path / "audit-results.json"

        config = _AuditConfig(
            project_root=tmp_path,
            logger=logger,
            verbose=True,
            output_format="json",
            output_path=str(output_file),
        )

        assert config.output_path == str(output_file)
        assert config.verbose is True

    def test_audit_config_is_frozen(self, tmp_path: Path):
        """_AuditConfig is immutable (frozen dataclass)."""
        logger = CommandLogger("test")

        config = _AuditConfig(
            project_root=tmp_path,
            logger=logger,
            verbose=False,
            output_format="text",
            output_path=None,
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            config.verbose = True


class TestAuditOutcomeCause:
    """Tests for _audit_outcome_cause helper."""

    def test_cause_no_git_remote(self):
        """Returns message for no_git_remote outcome."""
        result = _audit_outcome_cause("no_git_remote", None, None)

        assert "Could not determine org from git remote" in result

    def test_cause_absent(self):
        """Returns message for absent outcome."""
        result = _audit_outcome_cause("absent", "https://example.com/policy.yml", None)

        assert "No org policy found" in result
        assert "https://example.com/policy.yml" in result

    def test_cause_empty(self):
        """Returns message for empty outcome."""
        result = _audit_outcome_cause("empty", "https://example.com/policy.yml", None)

        assert "Org policy at" in result
        assert "is present but empty" in result

    def test_cause_with_error_text(self):
        """Returns message for error outcomes with error text."""
        result = _audit_outcome_cause("malformed", None, "Invalid YAML")

        assert "Policy fetch failed" in result
        assert "Invalid YAML" in result

    def test_cause_with_unknown_source(self):
        """Uses 'unknown' when source is None."""
        result = _audit_outcome_cause("absent", None, None)

        assert "No org policy found" in result
        assert "unknown" in result


class TestScanSingleFile:
    """Tests for _scan_single_file helper."""

    def test_scan_existing_file(self, tmp_path: Path):
        """Scan an existing file."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello world")

        logger = CommandLogger("test")

        with patch("apm_cli.commands.audit.ContentScanner.scan_file") as mock_scan:
            mock_finding = MagicMock(spec=ScanFinding)
            mock_scan.return_value = [mock_finding]

            findings, count = _scan_single_file(test_file, logger)

            assert count == 1
            assert len(findings) == 1

    def test_scan_file_no_findings(self, tmp_path: Path):
        """File with no findings returns empty findings."""
        test_file = tmp_path / "clean.txt"
        test_file.write_text("Clean content")

        logger = CommandLogger("test")

        with patch("apm_cli.commands.audit.ContentScanner.scan_file") as mock_scan:
            mock_scan.return_value = []

            findings, count = _scan_single_file(test_file, logger)

            assert count == 1
            assert findings == {}

    def test_scan_missing_file_exits(self, tmp_path: Path):
        """Scanning missing file calls sys.exit(1)."""
        missing_file = tmp_path / "nonexistent.txt"
        logger = CommandLogger("test")

        with pytest.raises(SystemExit):
            _scan_single_file(missing_file, logger)

    def test_scan_directory_path_exits(self, tmp_path: Path):
        """Scanning directory instead of file calls sys.exit(1)."""
        dir_path = tmp_path / "somedir"
        dir_path.mkdir()

        logger = CommandLogger("test")

        with pytest.raises(SystemExit):
            _scan_single_file(dir_path, logger)

    def test_scan_returns_absolute_path_as_key(self, tmp_path: Path):
        """Returned findings dict uses absolute path as key."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("Content")

        logger = CommandLogger("test")

        with patch("apm_cli.commands.audit.ContentScanner.scan_file") as mock_scan:
            mock_finding = MagicMock(spec=ScanFinding)
            mock_scan.return_value = [mock_finding]

            findings, _ = _scan_single_file(test_file, logger)

            # Key should be absolute path
            key = next(iter(findings.keys()))
            assert key == str(test_file.resolve())


class TestHasActionableFindings:
    """Tests for _has_actionable_findings helper."""

    def test_no_findings(self):
        """Empty findings dict returns False."""
        result = _has_actionable_findings({})

        assert result is False

    def test_with_findings(self):
        """Non-empty findings dict returns True."""
        finding = ScanFinding(
            file="/path/to/file.md",
            line=1,
            column=5,
            char="X",
            codepoint="U+200B",
            severity="critical",
            category="tag-character",
            description="Hidden character",
        )
        findings = {
            "/path/to/file.md": [finding],
        }

        result = _has_actionable_findings(findings)

        assert result is True

    def test_multiple_files_with_findings(self):
        """Multiple files with findings returns True."""
        finding1 = ScanFinding(
            file="/path/to/file1.md",
            line=1,
            column=5,
            char="X",
            codepoint="U+200B",
            severity="warning",
            category="tag-character",
            description="Hidden character",
        )
        finding2 = ScanFinding(
            file="/path/to/file2.md",
            line=2,
            column=10,
            char="Y",
            codepoint="U+200C",
            severity="critical",
            category="bidi-override",
            description="Bidi override",
        )
        findings = {
            "/path/to/file1.md": [finding1],
            "/path/to/file2.md": [finding2],
        }

        result = _has_actionable_findings(findings)

        assert result is True

    def test_empty_findings_list(self):
        """File with empty findings list returns False."""
        findings = {
            "/path/to/file.md": [],
        }

        result = _has_actionable_findings(findings)

        assert result is False

    def test_mixed_empty_and_nonempty(self):
        """Mixed empty and non-empty findings returns True."""
        finding = ScanFinding(
            file="/path/to/file2.md",
            line=1,
            column=5,
            char="X",
            codepoint="U+200B",
            severity="warning",
            category="tag-character",
            description="Hidden character",
        )
        findings = {
            "/path/to/file1.md": [],
            "/path/to/file2.md": [finding],
        }

        result = _has_actionable_findings(findings)

        assert result is True


class TestAuditCommand:
    """Tests for audit command structure."""

    def test_audit_command_exists(self):
        """audit command is available."""
        from apm_cli.commands.audit import audit

        assert audit is not None

    def test_audit_command_has_options(self):
        """audit command accepts expected options."""
        from apm_cli.commands.audit import audit

        # Verify Click command has params
        assert hasattr(audit, "params")
        param_names = {p.name for p in audit.params}
        assert "file" in param_names or "verbose" in param_names

    def test_audit_command_callable(self):
        """audit command is callable."""
        from apm_cli.commands.audit import audit

        assert callable(audit)

    def test_audit_exit_codes(self):
        """Audit module documents exit codes."""
        from apm_cli.commands import audit

        # Module docstring should mention exit codes
        assert audit.__doc__ is not None
        assert "0" in audit.__doc__ or "exit" in audit.__doc__.lower()


class TestAuditIntegration:
    """Integration tests for audit functionality."""

    def test_scan_file_with_content_scanner(self, tmp_path: Path):
        """Integrated scan with ContentScanner."""
        test_file = tmp_path / "test.md"
        test_file.write_text("# Test\nNormal content here")

        logger = CommandLogger("test")

        with patch("apm_cli.commands.audit.ContentScanner.scan_file") as mock_scan:
            mock_scan.return_value = []

            _findings, count = _scan_single_file(test_file, logger)

            assert count == 1
            mock_scan.assert_called_once_with(test_file)

    def test_audit_config_creation_for_scan(self, tmp_path: Path):
        """Create audit config and use for scan operation."""
        logger = CommandLogger("test", verbose=True)

        config = _AuditConfig(
            project_root=tmp_path,
            logger=logger,
            verbose=True,
            output_format="json",
            output_path=str(tmp_path / "results.json"),
        )

        assert config.project_root == tmp_path
        assert config.verbose is True
        assert "results.json" in config.output_path
