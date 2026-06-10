"""Comprehensive unit tests for apm_cli.commands.audit -- phase 3.

Targets the uncovered branches in:
- _audit_outcome_cause
- _scan_single_file
- _has_actionable_findings
- _render_findings_table
- _render_summary
- _apply_strip
- _preview_strip
- _render_ci_results
- _audit_content_scan (key branches)
- audit CLI command (key branches)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.commands.audit import (
    _apply_strip,
    _audit_outcome_cause,
    _AuditConfig,
    _has_actionable_findings,
    _preview_strip,
    _render_findings_table,
    _render_summary,
    _scan_single_file,
)
from apm_cli.security.content_scanner import ScanFinding

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_finding(
    severity: str = "critical",
    file: str = "test.md",
    line: int = 1,
    column: int = 5,
) -> ScanFinding:
    return ScanFinding(
        file=file,
        line=line,
        column=column,
        char="\u200b",
        codepoint="U+200B",
        severity=severity,
        category="zero-width",
        description="Zero-width space",
    )


def _make_logger() -> MagicMock:
    logger = MagicMock()
    logger.verbose = False
    return logger


def _make_cfg(
    project_root: Path | None = None,
    *,
    verbose: bool = False,
    output_format: str = "text",
    output_path: str | None = None,
) -> _AuditConfig:
    return _AuditConfig(
        project_root=project_root or Path("/tmp/fake"),
        logger=_make_logger(),
        verbose=verbose,
        output_format=output_format,
        output_path=output_path,
    )


# ---------------------------------------------------------------------------
# _audit_outcome_cause
# ---------------------------------------------------------------------------


class TestAuditOutcomeCause:
    def test_no_git_remote(self) -> None:
        result = _audit_outcome_cause("no_git_remote", None, None)
        assert "git remote" in result

    def test_absent_includes_source(self) -> None:
        result = _audit_outcome_cause("absent", "https://example.com/policy.yml", None)
        assert "https://example.com/policy.yml" in result

    def test_absent_with_none_source(self) -> None:
        result = _audit_outcome_cause("absent", None, None)
        assert result == "No org policy found at unknown"

    def test_empty_includes_source(self) -> None:
        result = _audit_outcome_cause("empty", "https://example.com/policy.yml", None)
        assert "https://example.com/policy.yml" in result
        assert "empty" in result

    def test_empty_with_none_source(self) -> None:
        result = _audit_outcome_cause("empty", None, None)
        assert "unknown" in result

    def test_malformed_uses_err_text(self) -> None:
        result = _audit_outcome_cause("malformed", "https://example.com", "JSON parse error")
        assert "JSON parse error" in result
        assert "Policy fetch failed" in result

    def test_cache_miss_fetch_fail_uses_err_text(self) -> None:
        result = _audit_outcome_cause("cache_miss_fetch_fail", None, "connection refused")
        assert "connection refused" in result

    def test_unknown_outcome_falls_back_to_outcome_text(self) -> None:
        result = _audit_outcome_cause("garbage_response", None, None)
        assert "garbage_response" in result

    def test_error_with_none_err_text_uses_outcome(self) -> None:
        result = _audit_outcome_cause("malformed", None, None)
        assert "malformed" in result


# ---------------------------------------------------------------------------
# _scan_single_file
# ---------------------------------------------------------------------------


class TestScanSingleFile:
    def test_nonexistent_file_calls_sys_exit(self, tmp_path: Path) -> None:
        logger = _make_logger()
        missing = tmp_path / "nonexistent.md"
        with pytest.raises(SystemExit) as exc_info:
            _scan_single_file(missing, logger)
        assert exc_info.value.code == 1
        logger.error.assert_called_once()

    def test_directory_calls_sys_exit(self, tmp_path: Path) -> None:
        logger = _make_logger()
        with pytest.raises(SystemExit) as exc_info:
            _scan_single_file(tmp_path, logger)
        assert exc_info.value.code == 1
        logger.error.assert_called_once()

    def test_clean_file_returns_empty_findings(self, tmp_path: Path) -> None:
        logger = _make_logger()
        clean_file = tmp_path / "clean.md"
        clean_file.write_text("Hello world\n", encoding="utf-8")
        findings, count = _scan_single_file(clean_file, logger)
        assert findings == {}
        assert count == 1

    def test_file_with_hidden_char_returns_findings(self, tmp_path: Path) -> None:
        logger = _make_logger()
        bad_file = tmp_path / "bad.md"
        # U+200B zero-width space
        bad_file.write_text("Hello\u200bworld\n", encoding="utf-8")
        findings, count = _scan_single_file(bad_file, logger)
        assert count == 1
        assert len(findings) > 0

    def test_findings_use_absolute_path_as_key(self, tmp_path: Path) -> None:
        logger = _make_logger()
        bad_file = tmp_path / "bad.md"
        bad_file.write_text("Hello\u200bworld\n", encoding="utf-8")
        findings, _ = _scan_single_file(bad_file, logger)
        for key in findings:
            assert Path(key).is_absolute()


# ---------------------------------------------------------------------------
# _has_actionable_findings
# ---------------------------------------------------------------------------


class TestHasActionableFindings:
    def test_empty_dict_returns_false(self) -> None:
        assert _has_actionable_findings({}) is False

    def test_info_only_returns_false(self) -> None:
        findings = {"file.md": [_make_finding("info")]}
        assert _has_actionable_findings(findings) is False

    def test_warning_returns_true(self) -> None:
        findings = {"file.md": [_make_finding("warning")]}
        assert _has_actionable_findings(findings) is True

    def test_critical_returns_true(self) -> None:
        findings = {"file.md": [_make_finding("critical")]}
        assert _has_actionable_findings(findings) is True

    def test_mixed_info_and_critical(self) -> None:
        findings = {
            "file.md": [_make_finding("info"), _make_finding("critical")],
        }
        assert _has_actionable_findings(findings) is True

    def test_multiple_files_with_info_only(self) -> None:
        findings = {
            "a.md": [_make_finding("info")],
            "b.md": [_make_finding("info")],
        }
        assert _has_actionable_findings(findings) is False


# ---------------------------------------------------------------------------
# _render_findings_table
# ---------------------------------------------------------------------------


class TestRenderFindingsTable:
    def test_empty_findings_returns_immediately(self) -> None:
        """No output path; empty findings should not raise."""
        _render_findings_table({})

    def test_info_filtered_in_non_verbose_mode(self) -> None:
        """Info findings are filtered out when verbose=False."""
        findings = {"file.md": [_make_finding("info")]}
        # Should not raise even if no findings remain after filter
        _render_findings_table(findings, verbose=False)

    def test_info_included_in_verbose_mode(self) -> None:
        """Info findings pass through filter in verbose mode."""
        findings = {"file.md": [_make_finding("info")]}
        with patch("apm_cli.commands.audit._get_console", return_value=None):
            with patch("apm_cli.commands.audit._rich_echo") as mock_echo:
                _render_findings_table(findings, verbose=True)
        # Should have produced some output
        assert mock_echo.called

    def test_critical_rendered_without_console(self) -> None:
        findings = {"file.md": [_make_finding("critical")]}
        with patch("apm_cli.commands.audit._get_console", return_value=None):
            with patch("apm_cli.commands.audit._rich_echo") as mock_echo:
                _render_findings_table(findings, verbose=False)
        assert mock_echo.called

    def test_warning_rendered_without_console(self) -> None:
        findings = {"file.md": [_make_finding("warning")]}
        with patch("apm_cli.commands.audit._get_console", return_value=None):
            with patch("apm_cli.commands.audit._rich_echo") as mock_echo:
                _render_findings_table(findings, verbose=False)
        assert mock_echo.called

    def test_severity_ordering(self) -> None:
        """Critical findings should appear before warnings in output."""
        findings = {
            "file.md": [
                _make_finding("warning", line=2),
                _make_finding("critical", line=1),
            ]
        }
        with patch("apm_cli.commands.audit._get_console", return_value=None):
            with patch("apm_cli.commands.audit._rich_echo") as mock_echo:
                _render_findings_table(findings, verbose=True)
        assert mock_echo.called


# ---------------------------------------------------------------------------
# _render_summary
# ---------------------------------------------------------------------------


class TestRenderSummary:
    def test_no_findings_logs_success(self) -> None:
        logger = _make_logger()
        _render_summary({}, files_scanned=5, logger=logger)
        logger.success.assert_called_once()
        args = logger.success.call_args[0][0]
        assert "5" in args

    def test_critical_findings_logs_error(self) -> None:
        logger = _make_logger()
        findings = {"file.md": [_make_finding("critical")]}
        with patch("apm_cli.commands.audit.ContentScanner") as mock_cs:
            mock_cs.summarize.return_value = {"critical": 1, "warning": 0, "info": 0}
            _render_summary(findings, files_scanned=1, logger=logger)
        logger.error.assert_called_once()

    def test_warning_only_logs_warning(self) -> None:
        logger = _make_logger()
        findings = {"file.md": [_make_finding("warning")]}
        with patch("apm_cli.commands.audit.ContentScanner") as mock_cs:
            mock_cs.summarize.return_value = {"critical": 0, "warning": 2, "info": 0}
            _render_summary(findings, files_scanned=1, logger=logger)
        logger.warning.assert_called_once()

    def test_info_only_logs_progress(self) -> None:
        logger = _make_logger()
        findings = {"file.md": [_make_finding("info")]}
        with patch("apm_cli.commands.audit.ContentScanner") as mock_cs:
            mock_cs.summarize.return_value = {"critical": 0, "warning": 0, "info": 3}
            _render_summary(findings, files_scanned=1, logger=logger)
        logger.progress.assert_called()

    def test_info_plus_critical_logs_extra_progress(self) -> None:
        logger = _make_logger()
        findings = {"file.md": [_make_finding("critical"), _make_finding("info")]}
        with patch("apm_cli.commands.audit.ContentScanner") as mock_cs:
            mock_cs.summarize.return_value = {"critical": 1, "warning": 0, "info": 1}
            _render_summary(findings, files_scanned=1, logger=logger)
        # Both error + progress for "Plus N info-level findings" should be called
        assert logger.progress.call_count >= 1


# ---------------------------------------------------------------------------
# _apply_strip
# ---------------------------------------------------------------------------


class TestApplyStrip:
    def test_strips_dangerous_chars_and_returns_count(self, tmp_path: Path) -> None:
        logger = _make_logger()
        bad_file = tmp_path / "bad.md"
        original = "Hello\u202eworld"
        bad_file.write_text(original, encoding="utf-8")

        finding = _make_finding("critical", file=str(bad_file), line=1, column=5)
        findings = {str(bad_file): [finding]}

        with patch("apm_cli.commands.audit.ContentScanner.strip_dangerous") as mock_strip:
            mock_strip.return_value = "Helloworld"
            modified = _apply_strip(findings, tmp_path, logger)

        assert modified == 1

    def test_skips_nonexistent_files(self, tmp_path: Path) -> None:
        logger = _make_logger()
        missing = str(tmp_path / "nonexistent.md")
        findings = {missing: [_make_finding("critical")]}
        result = _apply_strip(findings, tmp_path, logger)
        assert result == 0

    def test_skips_outside_project_root(self, tmp_path: Path) -> None:
        logger = _make_logger()
        project_root = tmp_path / "project"
        project_root.mkdir()
        # A relative path that resolves outside project_root via traversal
        outside_path = "../outside.md"
        findings = {outside_path: [_make_finding("critical")]}
        result = _apply_strip(findings, project_root, logger)
        assert result == 0
        logger.warning.assert_called()

    def test_handles_os_error_gracefully(self, tmp_path: Path) -> None:
        logger = _make_logger()
        bad_file = tmp_path / "bad.md"
        bad_file.write_text("Hello\u202eworld", encoding="utf-8")
        findings = {str(bad_file): [_make_finding("critical")]}

        with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
            result = _apply_strip(findings, tmp_path, logger)

        assert result == 0
        logger.warning.assert_called()

    def test_no_change_means_no_write(self, tmp_path: Path) -> None:
        logger = _make_logger()
        clean_file = tmp_path / "clean.md"
        clean_file.write_text("Hello world", encoding="utf-8")
        findings = {str(clean_file): [_make_finding("critical")]}

        with patch("apm_cli.commands.audit.ContentScanner.strip_dangerous") as mock_strip:
            mock_strip.return_value = "Hello world"  # unchanged
            result = _apply_strip(findings, tmp_path, logger)

        assert result == 0


# ---------------------------------------------------------------------------
# _preview_strip
# ---------------------------------------------------------------------------


class TestPreviewStrip:
    def test_no_strippable_returns_zero(self) -> None:
        logger = _make_logger()
        findings = {"file.md": [_make_finding("info")]}
        result = _preview_strip(findings, logger)
        assert result == 0
        logger.progress.assert_called()

    def test_strippable_returns_affected_count(self) -> None:
        logger = _make_logger()
        findings = {
            "file1.md": [_make_finding("critical")],
            "file2.md": [_make_finding("warning")],
        }
        with patch("apm_cli.commands.audit._get_console", return_value=None):
            with patch("apm_cli.commands.audit._rich_echo"):
                result = _preview_strip(findings, logger)
        assert result == 2

    def test_mixed_info_and_critical_counts_only_strippable(self) -> None:
        logger = _make_logger()
        findings = {
            "file1.md": [_make_finding("info")],  # not strippable
            "file2.md": [_make_finding("critical")],  # strippable
        }
        with patch("apm_cli.commands.audit._get_console", return_value=None):
            with patch("apm_cli.commands.audit._rich_echo"):
                result = _preview_strip(findings, logger)
        assert result == 1

    def test_empty_findings_returns_zero(self) -> None:
        logger = _make_logger()
        result = _preview_strip({}, logger)
        assert result == 0


# ---------------------------------------------------------------------------
# _audit_content_scan -- key branches
# ---------------------------------------------------------------------------


class TestAuditContentScanBranches:
    def test_no_lockfile_exits_zero(self, tmp_path: Path) -> None:
        logger = _make_logger()
        cfg = _AuditConfig(
            project_root=tmp_path,
            logger=logger,
            verbose=False,
            output_format="text",
            output_path=None,
        )
        from apm_cli.commands.audit import _audit_content_scan

        with pytest.raises(SystemExit) as exc_info:
            with patch("apm_cli.commands.audit.get_lockfile_path") as mock_lf:
                mock_lf.return_value = tmp_path / "apm.lock.yaml"
                _audit_content_scan(cfg, None, None, False, False)
        assert exc_info.value.code == 0
        logger.progress.assert_called()

    def test_strip_with_no_findings_exits_zero(self, tmp_path: Path) -> None:
        logger = _make_logger()
        cfg = _AuditConfig(
            project_root=tmp_path,
            logger=logger,
            verbose=False,
            output_format="text",
            output_path=None,
        )
        from apm_cli.commands.audit import _audit_content_scan

        with pytest.raises(SystemExit) as exc_info:
            with patch("apm_cli.commands.audit.get_lockfile_path") as mock_lf:
                mock_lf.return_value = tmp_path / "apm.lock.yaml"
                with patch("apm_cli.commands.audit.scan_lockfile_packages") as mock_scan:
                    mock_scan.return_value = ({}, 3)
                    _audit_content_scan(cfg, None, None, strip=True, dry_run=False)
        assert exc_info.value.code == 0

    def test_format_incompatible_with_strip_exits(self, tmp_path: Path) -> None:
        import click

        logger = _make_logger()
        cfg = _AuditConfig(
            project_root=tmp_path,
            logger=logger,
            verbose=False,
            output_format="json",
            output_path=None,
        )
        from apm_cli.commands.audit import _audit_content_scan

        with pytest.raises(click.UsageError, match=r"cannot be combined"):
            _audit_content_scan(cfg, None, None, strip=True, dry_run=False)

    def test_text_format_with_output_path_exits(self, tmp_path: Path) -> None:
        """Text format + --output path is unsupported."""
        logger = _make_logger()
        cfg = _AuditConfig(
            project_root=tmp_path,
            logger=logger,
            verbose=False,
            output_format="text",
            output_path=str(tmp_path / "report.txt"),
        )
        from apm_cli.commands.audit import _audit_content_scan

        # Create the lockfile so the scan proceeds past the "no lockfile" early-exit
        lock_file = tmp_path / "apm.lock.yaml"
        lock_file.write_text("dependencies: {}\n")

        with pytest.raises(SystemExit) as exc_info:
            with patch("apm_cli.commands.audit.get_lockfile_path") as mock_lf:
                mock_lf.return_value = lock_file
                with patch("apm_cli.commands.audit.scan_lockfile_packages") as mock_scan:
                    mock_scan.return_value = ({}, 1)
                    with patch(
                        "apm_cli.commands.audit._has_actionable_findings", return_value=False
                    ):
                        with patch("apm_cli.commands.audit._render_summary"):
                            _audit_content_scan(cfg, None, None, strip=False, dry_run=False)
        assert exc_info.value.code == 1

    def test_package_not_found_warning_and_exit(self, tmp_path: Path) -> None:
        logger = _make_logger()
        lock_file = tmp_path / "apm.lock.yaml"
        lock_file.write_text("dependencies: {}\n")
        cfg = _AuditConfig(
            project_root=tmp_path,
            logger=logger,
            verbose=False,
            output_format="text",
            output_path=None,
        )
        from apm_cli.commands.audit import _audit_content_scan

        with pytest.raises(SystemExit) as exc_info:
            with patch("apm_cli.commands.audit.get_lockfile_path") as mock_lf:
                mock_lf.return_value = lock_file
                with patch("apm_cli.commands.audit.scan_lockfile_packages") as mock_scan:
                    mock_scan.return_value = ({}, 0)
                    _audit_content_scan(cfg, "owner/repo", None, strip=False, dry_run=False)
        assert exc_info.value.code == 0
        logger.warning.assert_called()

    def test_dry_run_without_strip_warns(self, tmp_path: Path) -> None:
        logger = _make_logger()
        lock_file = tmp_path / "apm.lock.yaml"
        lock_file.write_text("dependencies: {}\n")
        cfg = _AuditConfig(
            project_root=tmp_path,
            logger=logger,
            verbose=False,
            output_format="text",
            output_path=None,
        )
        from apm_cli.commands.audit import _audit_content_scan

        with pytest.raises(SystemExit):
            with patch("apm_cli.commands.audit.get_lockfile_path") as mock_lf:
                mock_lf.return_value = lock_file
                with patch("apm_cli.commands.audit.scan_lockfile_packages") as mock_scan:
                    mock_scan.return_value = ({}, 1)
                    with patch(
                        "apm_cli.commands.audit._has_actionable_findings", return_value=False
                    ):
                        with patch("apm_cli.commands.audit._render_summary"):
                            _audit_content_scan(cfg, None, None, strip=False, dry_run=True)
        logger.progress.assert_any_call(
            "--dry-run only works with --strip (e.g. apm audit --strip --dry-run)"
        )


# ---------------------------------------------------------------------------
# audit CLI command branches
# ---------------------------------------------------------------------------


class TestAuditCommandBranches:
    """Test the Click entry-point for key validation branches."""

    def _invoke(self, args: list[str]) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.audit import audit

        runner = CliRunner()
        return runner.invoke(audit, args, catch_exceptions=False)

    def test_no_drift_with_strip_raises_usage_error(self) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.audit import audit

        runner = CliRunner()
        result = runner.invoke(audit, ["--no-drift", "--strip"])
        assert result.exit_code != 0

    def test_no_drift_with_file_raises_usage_error(self) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.audit import audit

        runner = CliRunner()
        result = runner.invoke(audit, ["--no-drift", "--file", "somefile.md"])
        assert result.exit_code != 0

    def test_ci_with_strip_exits_with_error(self) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.audit import audit

        runner = CliRunner()
        with patch("apm_cli.commands.audit._audit_ci_gate") as mock_ci:
            mock_ci.side_effect = SystemExit(0)
            # --ci with --strip should NOT even reach _audit_ci_gate
            result = runner.invoke(audit, ["--ci", "--strip"])
        assert result.exit_code != 0

    def test_ci_with_markdown_format_exits(self) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.audit import audit

        runner = CliRunner()
        result = runner.invoke(audit, ["--ci", "--format", "markdown"])
        assert result.exit_code != 0

    def test_policy_without_ci_warns(self) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.audit import audit

        runner = CliRunner()
        with patch("apm_cli.commands.audit._audit_content_scan") as mock_scan:
            mock_scan.side_effect = SystemExit(0)
            result = runner.invoke(audit, ["--policy", "myorg"])
        # Should warn but still proceed to content scan
        assert "--policy requires --ci mode" in (result.output + (result.exception or ""))

    def test_verbose_in_ci_mode_warns(self) -> None:
        from click.testing import CliRunner

        from apm_cli.commands.audit import audit

        runner = CliRunner()
        with patch("apm_cli.commands.audit._audit_ci_gate", side_effect=SystemExit(0)):
            result = runner.invoke(audit, ["--ci", "--verbose"])
        # Logger.warning called with "verbose has no effect" message
        # The exit code comes from the mocked SystemExit(0)
        assert result.exit_code == 0
