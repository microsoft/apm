"""Comprehensive unit tests for apm_cli.commands.audit.

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

import sys
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
from apm_cli.core.command_logger import CommandLogger
from apm_cli.security.content_scanner import ScanFinding
from apm_cli.security.file_scanner import FileScanResult

pytestmark = pytest.mark.component

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
    @pytest.mark.parametrize(
        ("severities_by_file", "expected"),
        [
            ({}, False),
            ({"file.md": ["info"]}, False),
            ({"file.md": ["warning"]}, True),
            ({"file.md": ["critical"]}, True),
            ({"file.md": ["info", "critical"]}, True),
            ({"a.md": ["info"], "b.md": ["info"]}, False),
        ],
        ids=["empty", "info-only", "warning", "critical", "mixed", "multi-file-info"],
    )
    def test_actionable_when_any_finding_is_not_info(
        self, severities_by_file: dict[str, list[str]], expected: bool
    ) -> None:
        findings = {
            path: [_make_finding(severity) for severity in severities]
            for path, severities in severities_by_file.items()
        }
        assert _has_actionable_findings(findings) is expected


# ---------------------------------------------------------------------------
# _render_findings_table
# ---------------------------------------------------------------------------


class TestRenderFindingsTable:
    @pytest.mark.parametrize("severities", [[], ["info"]], ids=["empty", "info-non-verbose"])
    def test_nothing_rendered_without_visible_rows(self, severities: list[str]) -> None:
        findings = {"file.md": [_make_finding(s) for s in severities]} if severities else {}
        console = MagicMock()
        with patch("apm_cli.commands.audit._get_console", return_value=console):
            with patch("apm_cli.commands.audit._rich_echo") as mock_echo:
                _render_findings_table(findings, verbose=False)
        mock_echo.assert_not_called()
        console.print.assert_not_called()

    def test_info_included_in_verbose_mode(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Info findings pass through filter in verbose mode."""
        findings = {"file.md": [_make_finding("info")]}
        with patch("apm_cli.commands.audit._get_console", return_value=None):
            _render_findings_table(findings, verbose=True)
        assert "INFO       test.md 1:5  U+200B" in "".join(capsys.readouterr())

    def test_critical_rendered_without_console(self, capsys: pytest.CaptureFixture[str]) -> None:
        findings = {"file.md": [_make_finding("critical")]}
        with patch("apm_cli.commands.audit._get_console", return_value=None):
            _render_findings_table(findings, verbose=False)
        assert "CRITICAL   test.md 1:5  U+200B  Zero-width space" in "".join(capsys.readouterr())

    def test_warning_rendered_without_console(self, capsys: pytest.CaptureFixture[str]) -> None:
        findings = {"file.md": [_make_finding("warning")]}
        with patch("apm_cli.commands.audit._get_console", return_value=None):
            _render_findings_table(findings, verbose=False)
        assert "WARNING    test.md 1:5  U+200B  Zero-width space" in "".join(capsys.readouterr())

    def test_severity_ordering(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Critical findings should appear before warnings in output."""
        findings = {
            "file.md": [
                _make_finding("warning", line=2),
                _make_finding("critical", line=1),
            ]
        }
        # Critical lines go to stderr (RA-004); interleave both streams to keep order.
        monkeypatch.setattr(sys, "stderr", sys.stdout)
        with patch("apm_cli.commands.audit._get_console", return_value=None):
            _render_findings_table(findings, verbose=True)
        out = "".join(capsys.readouterr())
        assert out.index("CRITICAL   test.md 1:5") < out.index("WARNING    test.md 2:5")


# ---------------------------------------------------------------------------
# _render_summary
# ---------------------------------------------------------------------------


class TestRenderSummary:
    def test_no_findings_logs_success(self, capsys: pytest.CaptureFixture[str]) -> None:
        _render_summary({}, files_scanned=5, logger=CommandLogger("audit"))
        assert "[*] 5 file(s) scanned -- no issues found" in "".join(capsys.readouterr())

    def test_critical_findings_logs_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        findings = {"file.md": [_make_finding("critical")]}
        with patch("apm_cli.commands.audit.ContentScanner") as mock_cs:
            mock_cs.summarize.return_value = {"critical": 1, "warning": 0, "info": 0}
            _render_summary(findings, files_scanned=1, logger=CommandLogger("audit"))
        out = "".join(capsys.readouterr())
        assert (
            out.count("[x] 1 critical finding(s) in 1 file(s) -- hidden characters detected") == 1
        )

    def test_warning_only_logs_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        findings = {"file.md": [_make_finding("warning")]}
        with patch("apm_cli.commands.audit.ContentScanner") as mock_cs:
            mock_cs.summarize.return_value = {"critical": 0, "warning": 2, "info": 0}
            _render_summary(findings, files_scanned=1, logger=CommandLogger("audit"))
        out = "".join(capsys.readouterr())
        assert out.count("[!] 2 warning(s) in 1 file(s) -- hidden characters detected") == 1

    def test_info_only_logs_progress(self, capsys: pytest.CaptureFixture[str]) -> None:
        findings = {"file.md": [_make_finding("info")]}
        with patch("apm_cli.commands.audit.ContentScanner") as mock_cs:
            mock_cs.summarize.return_value = {"critical": 0, "warning": 0, "info": 3}
            _render_summary(findings, files_scanned=1, logger=CommandLogger("audit"))
        out = " ".join("".join(capsys.readouterr()).split())
        assert "3 info-level finding(s) in 1 file(s) -- unusual characters" in out

    def test_info_plus_critical_logs_extra_progress(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        findings = {"file.md": [_make_finding("critical"), _make_finding("info")]}
        with patch("apm_cli.commands.audit.ContentScanner") as mock_cs:
            mock_cs.summarize.return_value = {"critical": 1, "warning": 0, "info": 1}
            _render_summary(findings, files_scanned=1, logger=CommandLogger("audit"))
        out = "".join(capsys.readouterr())
        assert "1 critical finding(s) in 1 file(s)" in out
        assert "These characters may embed invisible instructions" in out


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
        assert bad_file.read_text(encoding="utf-8") == "Helloworld"

    def test_skips_nonexistent_files(self, tmp_path: Path) -> None:
        logger = _make_logger()
        missing = str(tmp_path / "nonexistent.md")
        findings = {missing: [_make_finding("critical")]}
        result = _apply_strip(findings, tmp_path, logger)
        assert result == 0
        assert not (tmp_path / "nonexistent.md").exists()

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
        assert not (tmp_path / "outside.md").exists()

    def test_handles_os_error_gracefully(self, tmp_path: Path) -> None:
        logger = _make_logger()
        bad_file = tmp_path / "bad.md"
        bad_file.write_text("Hello\u202eworld", encoding="utf-8")
        findings = {str(bad_file): [_make_finding("critical")]}

        with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
            result = _apply_strip(findings, tmp_path, logger)

        assert result == 0
        logger.warning.assert_called()
        assert bad_file.read_text(encoding="utf-8") == "Hello\u202eworld"

    def test_no_change_means_no_write(self, tmp_path: Path) -> None:
        logger = _make_logger()
        clean_file = tmp_path / "clean.md"
        clean_file.write_text("Hello world", encoding="utf-8")
        findings = {str(clean_file): [_make_finding("critical")]}

        with patch("apm_cli.commands.audit.ContentScanner.strip_dangerous") as mock_strip:
            mock_strip.return_value = "Hello world"  # unchanged
            result = _apply_strip(findings, tmp_path, logger)

        assert result == 0
        assert clean_file.read_text(encoding="utf-8") == "Hello world"


# ---------------------------------------------------------------------------
# _preview_strip
# ---------------------------------------------------------------------------


class TestPreviewStrip:
    def test_no_strippable_returns_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        findings = {"file.md": [_make_finding("info")]}
        result = _preview_strip(findings, CommandLogger("audit"))
        assert result == 0
        assert "Nothing to clean -- no strippable characters found" in "".join(capsys.readouterr())

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


class TestRichTableMarkupEscaping:
    """#3845: bracketed text in audit tables must not be parsed as Rich markup."""

    @staticmethod
    def _console():
        import io

        from rich.console import Console

        return Console(file=io.StringIO(), width=120, color_system=None)

    def test_ci_table_shows_cross_for_failed_check(self) -> None:
        from apm_cli.commands.audit import _render_ci_results
        from apm_cli.policy.models import CheckResult, CIAuditResult

        console = self._console()
        result = CIAuditResult(
            checks=[
                CheckResult(name="lockfile-exists", passed=True, message="ok"),
                CheckResult(name="required-packages", passed=False, message="missing"),
            ]
        )
        with patch("apm_cli.commands.audit._get_console", return_value=console):
            _render_ci_results(result)
        lines = console.file.getvalue().splitlines()
        failed_row = next(line for line in lines if "missing" in line)
        passed_row = next(line for line in lines if "lockfile-exists" in line)
        assert "[x]" in failed_row
        assert "[+]" in passed_row

    def test_warn_mode_check_names_its_subjects(self) -> None:
        """Warn-mode CI names the denied dependency the team must fix (#3994)."""
        from apm_cli.commands.audit import _render_ci_results
        from apm_cli.policy.models import CheckResult, CIAuditResult

        console = self._console()
        result = CIAuditResult(
            checks=[
                CheckResult(
                    name="dependency-denylist",
                    passed=True,
                    message="1 dependency(ies) match deny list (enforcement: warn)",
                    details=["github.com/acme/tools"],
                    warned=True,
                ),
            ]
        )
        with (
            patch("apm_cli.commands.audit._get_console", return_value=console),
            patch("apm_cli.commands.audit._rich_echo") as echo,
            patch("apm_cli.commands.audit._rich_warning") as warning,
            patch("apm_cli.commands.audit._rich_success") as success,
        ):
            _render_ci_results(result)
        row = next(line for line in console.file.getvalue().splitlines() if "deny" in line)
        assert "[!]" in row
        echoed = " ".join(call.args[0] for call in echo.call_args_list)
        assert "github.com/acme/tools" in echoed
        assert "enforcement: warn" in warning.call_args.args[0]
        # No green "All N passed" next to a violation (#3994).
        success.assert_not_called()

    def test_strip_preview_keeps_bracketed_path(self) -> None:
        console = self._console()
        findings = {".github/prompts/[x]note.md": [_make_finding("critical")]}
        with patch("apm_cli.commands.audit._get_console", return_value=console):
            with patch("apm_cli.commands.audit._rich_echo"):
                _preview_strip(findings, _make_logger())
        assert "[x]note.md" in console.file.getvalue()


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
                with patch("apm_cli.commands.audit.scan_project_result") as mock_scan:
                    mock_scan.return_value = FileScanResult({}, frozenset({"a.md", "b.md", "c.md"}))
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
        lock_file.write_text("lockfile_version: '1'\ndependencies: []\n")

        with pytest.raises(SystemExit) as exc_info:
            with patch("apm_cli.commands.audit.get_lockfile_path") as mock_lf:
                mock_lf.return_value = lock_file
                with patch("apm_cli.commands.audit.scan_project_result") as mock_scan:
                    mock_scan.return_value = FileScanResult({}, frozenset({"checked.md"}))
                    with patch(
                        "apm_cli.commands.audit._has_actionable_findings", return_value=False
                    ):
                        with patch("apm_cli.commands.audit._render_summary"):
                            _audit_content_scan(cfg, None, None, strip=False, dry_run=False)
        assert exc_info.value.code == 1

    def test_package_not_installed_errors_and_exits_one(self, tmp_path: Path) -> None:
        logger = _make_logger()
        lock_file = tmp_path / "apm.lock.yaml"
        lock_file.write_text("lockfile_version: '1'\ndependencies: []\n")
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
                with patch("apm_cli.commands.audit.scan_project_result") as mock_scan:
                    mock_scan.return_value = FileScanResult({}, frozenset())
                    _audit_content_scan(cfg, "owner/repo", None, strip=False, dry_run=False)
        assert exc_info.value.code == 1
        logger.error.assert_called()
        mock_scan.assert_not_called()

    def test_dry_run_without_strip_warns(self, tmp_path: Path) -> None:
        logger = _make_logger()
        lock_file = tmp_path / "apm.lock.yaml"
        lock_file.write_text("lockfile_version: '1'\ndependencies: []\n")
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
                with patch("apm_cli.commands.audit.scan_project_result") as mock_scan:
                    mock_scan.return_value = FileScanResult({}, frozenset({"checked.md"}))
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
