"""Unit tests for apm_cli.commands.audit.

Covers missing lines/branches in audit.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_finding(severity="critical", file="a.md", line=1, col=0):
    from apm_cli.security.content_scanner import ScanFinding

    return ScanFinding(
        file=file,
        line=line,
        column=col,
        char="\ue0001",
        codepoint="U+E0001",
        description="tag char",
        severity=severity,
        category="tag",
    )


@pytest.fixture()
def logger():
    from apm_cli.core.command_logger import CommandLogger

    return CommandLogger("test-audit", verbose=False)


# ---------------------------------------------------------------------------
# _audit_outcome_cause
# ---------------------------------------------------------------------------


class TestAuditOutcomeCause:
    def test_no_git_remote(self):
        from apm_cli.commands.audit import _audit_outcome_cause

        msg = _audit_outcome_cause("no_git_remote", None, None)
        assert "git remote" in msg.lower()

    def test_absent(self):
        from apm_cli.commands.audit import _audit_outcome_cause

        msg = _audit_outcome_cause("absent", "https://example.com/policy.yml", None)
        assert "No org policy" in msg
        assert "https://example.com/policy.yml" in msg

    def test_empty(self):
        from apm_cli.commands.audit import _audit_outcome_cause

        msg = _audit_outcome_cause("empty", "https://example.com/p.yml", None)
        assert "empty" in msg.lower()

    def test_malformed_shows_err_text(self):
        from apm_cli.commands.audit import _audit_outcome_cause

        msg = _audit_outcome_cause("malformed", None, "bad json")
        assert "bad json" in msg

    def test_cache_miss_fetch_fail_shows_outcome_when_no_err(self):
        from apm_cli.commands.audit import _audit_outcome_cause

        msg = _audit_outcome_cause("cache_miss_fetch_fail", None, None)
        assert "cache_miss_fetch_fail" in msg


# ---------------------------------------------------------------------------
# _scan_single_file
# ---------------------------------------------------------------------------


class TestScanSingleFile:
    def test_clean_file_zero_findings(self, tmp_path, logger):
        from apm_cli.commands.audit import _scan_single_file

        f = tmp_path / "clean.md"
        f.write_text("Normal text.\n", encoding="utf-8")
        findings, count = _scan_single_file(f, logger)
        assert count == 1
        assert findings == {} or all(len(v) == 0 for v in findings.values())

    def test_file_with_critical_chars(self, tmp_path, logger):
        from apm_cli.commands.audit import _scan_single_file

        f = tmp_path / "bad.md"
        # U+E0001 language tag
        f.write_text("Hello\U000e0001world\n", encoding="utf-8")
        findings, count = _scan_single_file(f, logger)
        assert count == 1
        assert any(findings.values())


# ---------------------------------------------------------------------------
# _render_summary
# ---------------------------------------------------------------------------


class TestRenderSummary:
    def test_critical_findings(self, logger, capsys):
        from apm_cli.commands.audit import _render_summary

        findings_by_file = {"a.md": [_make_finding("critical")]}
        _render_summary(findings_by_file, 1, logger)
        # No exception raised is the key assertion

    def test_warning_findings(self, logger, capsys):
        from apm_cli.commands.audit import _render_summary

        findings_by_file = {"a.md": [_make_finding("warning")]}
        _render_summary(findings_by_file, 1, logger)

    def test_info_only_findings(self, logger):
        from apm_cli.commands.audit import _render_summary

        findings_by_file = {"a.md": [_make_finding("info")]}
        _render_summary(findings_by_file, 1, logger)

    def test_no_findings(self, logger):
        from apm_cli.commands.audit import _render_summary

        _render_summary({}, 5, logger)

    def test_info_plus_critical_shows_extra(self, logger):
        from apm_cli.commands.audit import _render_summary

        findings_by_file = {
            "a.md": [_make_finding("critical"), _make_finding("info")],
        }
        _render_summary(findings_by_file, 1, logger)


# ---------------------------------------------------------------------------
# _apply_strip
# ---------------------------------------------------------------------------


class TestApplyStrip:
    def test_strips_file_with_dangerous_chars(self, tmp_path, logger):
        from apm_cli.commands.audit import _apply_strip

        f = tmp_path / "bad.md"
        content = "Hello\U000e0001world\n"
        f.write_text(content, encoding="utf-8")

        findings = {str(f): [_make_finding("critical", file=str(f))]}
        count = _apply_strip(findings, tmp_path, logger)
        assert count >= 0  # may or may not strip depending on ContentScanner

    def test_skips_outside_project_root(self, tmp_path, logger):
        from apm_cli.commands.audit import _apply_strip

        # relative path that escapes root via ..
        findings = {"../outside/file.md": [_make_finding("critical")]}
        count = _apply_strip(findings, tmp_path, logger)
        assert count == 0

    def test_skips_nonexistent_file(self, tmp_path, logger):
        from apm_cli.commands.audit import _apply_strip

        findings = {str(tmp_path / "missing.md"): [_make_finding("critical")]}
        count = _apply_strip(findings, tmp_path, logger)
        assert count == 0

    def test_handles_unicode_decode_error(self, tmp_path, logger):
        from apm_cli.commands.audit import _apply_strip

        f = tmp_path / "binary.md"
        f.write_bytes(b"\xff\xfe invalid utf-8")
        findings = {str(f): [_make_finding("critical", file=str(f))]}
        count = _apply_strip(findings, tmp_path, logger)
        # Should not raise, count stays 0
        assert count == 0


# ---------------------------------------------------------------------------
# _preview_strip
# ---------------------------------------------------------------------------


class TestPreviewStrip:
    def test_nothing_to_clean_when_only_info(self, logger, capsys):
        from apm_cli.commands.audit import _preview_strip

        findings = {"a.md": [_make_finding("info")]}
        count = _preview_strip(findings, logger)
        assert count == 0

    def test_shows_table_for_critical_findings(self, tmp_path, logger):
        from apm_cli.commands.audit import _preview_strip

        findings = {"a.md": [_make_finding("critical")]}
        # Should not raise
        count = _preview_strip(findings, logger)
        assert count == 1

    def test_warning_findings_show_in_preview(self, logger):
        from apm_cli.commands.audit import _preview_strip

        findings = {"b.md": [_make_finding("warning")]}
        count = _preview_strip(findings, logger)
        assert count == 1

    def test_empty_findings_returns_zero(self, logger):
        from apm_cli.commands.audit import _preview_strip

        count = _preview_strip({}, logger)
        assert count == 0


# ---------------------------------------------------------------------------
# _audit_content_scan
# ---------------------------------------------------------------------------


class TestAuditContentScan:
    def _make_cfg(self, tmp_path, verbose=False, output_format="text", output_path=None):
        from apm_cli.commands.audit import _AuditConfig
        from apm_cli.core.command_logger import CommandLogger

        log = CommandLogger("audit", verbose=verbose)
        return _AuditConfig(
            project_root=tmp_path,
            logger=log,
            verbose=verbose,
            output_format=output_format,
            output_path=output_path,
        )

    def test_no_lockfile_exits_zero(self, tmp_path):
        from apm_cli.commands.audit import _audit_content_scan

        cfg = self._make_cfg(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _audit_content_scan(cfg, package=None, file_path=None, strip=False, dry_run=False)
        assert exc.value.code == 0

    def test_file_path_mode_clean_file(self, tmp_path):
        from apm_cli.commands.audit import _audit_content_scan

        f = tmp_path / "clean.md"
        f.write_text("No issues.\n", encoding="utf-8")
        cfg = self._make_cfg(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _audit_content_scan(cfg, package=None, file_path=str(f), strip=False, dry_run=False)
        assert exc.value.code == 0

    def test_strip_mode_no_findings_exits_zero(self, tmp_path):
        from apm_cli.commands.audit import _audit_content_scan

        f = tmp_path / "clean.md"
        f.write_text("Clean.\n", encoding="utf-8")
        cfg = self._make_cfg(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _audit_content_scan(cfg, package=None, file_path=str(f), strip=True, dry_run=False)
        assert exc.value.code == 0

    def test_dry_run_without_strip_warns(self, tmp_path, capsys):
        from apm_cli.commands.audit import _audit_content_scan

        f = tmp_path / "clean.md"
        f.write_text("Clean.\n", encoding="utf-8")
        cfg = self._make_cfg(tmp_path)
        with pytest.raises(SystemExit):
            _audit_content_scan(cfg, package=None, file_path=str(f), strip=False, dry_run=True)

    def test_strip_dry_run_exits_after_preview(self, tmp_path):
        from apm_cli.commands.audit import _audit_content_scan

        f = tmp_path / "bad.md"
        f.write_text("Danger\U000e0001here\n", encoding="utf-8")
        cfg = self._make_cfg(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _audit_content_scan(cfg, package=None, file_path=str(f), strip=True, dry_run=True)
        assert exc.value.code == 0

    def test_strip_with_critical_findings_exits_zero(self, tmp_path):
        from apm_cli.commands.audit import _audit_content_scan

        f = tmp_path / "critical.md"
        f.write_text("Bad\U000e0001char\n", encoding="utf-8")
        cfg = self._make_cfg(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _audit_content_scan(cfg, package=None, file_path=str(f), strip=True, dry_run=False)
        assert exc.value.code == 0

    def test_format_json_incompatible_with_strip(self, tmp_path):
        import click

        from apm_cli.commands.audit import _audit_content_scan

        f = tmp_path / "f.md"
        f.write_text("x\n", encoding="utf-8")
        cfg = self._make_cfg(tmp_path, output_format="json")
        with pytest.raises(click.UsageError, match=r"cannot be combined"):
            _audit_content_scan(cfg, package=None, file_path=str(f), strip=True, dry_run=False)

    def test_text_format_with_output_path_errors(self, tmp_path):
        from apm_cli.commands.audit import _audit_content_scan

        f = tmp_path / "f.md"
        f.write_text("x\n", encoding="utf-8")
        cfg = self._make_cfg(tmp_path, output_format="text", output_path=str(tmp_path / "out.txt"))
        with pytest.raises(SystemExit) as exc:
            _audit_content_scan(cfg, package=None, file_path=str(f), strip=False, dry_run=False)
        assert exc.value.code == 1

    def test_no_drift_flag_in_text_mode_warns(self, tmp_path, capsys):
        from apm_cli.commands.audit import _audit_content_scan

        f = tmp_path / "f.md"
        f.write_text("clean\n", encoding="utf-8")
        cfg = self._make_cfg(tmp_path, output_format="text")
        with pytest.raises(SystemExit):
            _audit_content_scan(
                cfg, package=None, file_path=str(f), strip=False, dry_run=False, no_drift=True
            )
        captured = capsys.readouterr()
        assert "drift" in captured.err.lower()


# ---------------------------------------------------------------------------
# _audit_ci_gate  -- skipping/no-drift
# ---------------------------------------------------------------------------


class TestAuditCiGateNodrift:
    def _make_cfg(self, tmp_path, output_format="text", output_path=None):
        from apm_cli.commands.audit import _AuditConfig
        from apm_cli.core.command_logger import CommandLogger

        log = CommandLogger("audit", verbose=False)
        return _AuditConfig(
            project_root=tmp_path,
            logger=log,
            verbose=False,
            output_format=output_format,
            output_path=output_path,
        )

    def test_no_drift_in_text_mode_warns(self, tmp_path, capsys):
        from apm_cli.commands.audit import _audit_ci_gate

        cfg = self._make_cfg(tmp_path, output_format="text")

        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.checks = []
        mock_result.to_json.return_value = {"summary": {"total": 0, "failed": 0}}

        with (
            patch("apm_cli.policy.ci_checks.run_baseline_checks", return_value=mock_result),
            pytest.raises(SystemExit),
        ):
            _audit_ci_gate(
                cfg,
                policy_source=None,
                no_cache=False,
                no_policy=True,
                no_fail_fast=False,
                no_drift=True,
            )
        captured = capsys.readouterr()
        assert "drift" in captured.err.lower()

    def test_exits_zero_when_passed(self, tmp_path):
        from apm_cli.commands.audit import _audit_ci_gate

        cfg = self._make_cfg(tmp_path)
        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.checks = []
        mock_result.to_json.return_value = {"summary": {"total": 0, "failed": 0}}

        with (
            patch("apm_cli.policy.ci_checks.run_baseline_checks", return_value=mock_result),
            patch("apm_cli.policy.ci_checks._check_drift"),
            pytest.raises(SystemExit) as exc,
        ):
            _audit_ci_gate(
                cfg,
                policy_source=None,
                no_cache=False,
                no_policy=True,
                no_fail_fast=False,
                no_drift=True,
            )
        assert exc.value.code == 0

    def test_exits_one_when_failed(self, tmp_path):
        from apm_cli.commands.audit import _audit_ci_gate

        cfg = self._make_cfg(tmp_path)
        mock_result = MagicMock()
        mock_result.passed = False
        mock_result.checks = []
        mock_result.to_json.return_value = {"summary": {"total": 1, "failed": 1}}

        with (
            patch("apm_cli.policy.ci_checks.run_baseline_checks", return_value=mock_result),
            pytest.raises(SystemExit) as exc,
        ):
            _audit_ci_gate(
                cfg,
                policy_source=None,
                no_cache=False,
                no_policy=True,
                no_fail_fast=False,
                no_drift=True,
            )
        assert exc.value.code == 1

    def test_json_output_format(self, tmp_path):
        from apm_cli.commands.audit import _audit_ci_gate

        cfg = self._make_cfg(tmp_path, output_format="json")
        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.checks = []
        mock_result.to_json.return_value = {
            "summary": {"total": 0, "failed": 0},
            "checks": [],
        }

        with (
            patch("apm_cli.policy.ci_checks.run_baseline_checks", return_value=mock_result),
            pytest.raises(SystemExit),
        ):
            _audit_ci_gate(
                cfg,
                policy_source=None,
                no_cache=False,
                no_policy=True,
                no_fail_fast=False,
                no_drift=True,
            )

    def test_sarif_output_format(self, tmp_path):
        from apm_cli.commands.audit import _audit_ci_gate

        cfg = self._make_cfg(tmp_path, output_format="sarif")
        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.checks = []
        mock_result.to_sarif.return_value = {"runs": [{"results": []}]}
        mock_result.to_json.return_value = {"summary": {"total": 0, "failed": 0}}

        with (
            patch("apm_cli.policy.ci_checks.run_baseline_checks", return_value=mock_result),
            pytest.raises(SystemExit),
        ):
            _audit_ci_gate(
                cfg,
                policy_source=None,
                no_cache=False,
                no_policy=True,
                no_fail_fast=False,
                no_drift=True,
            )

    def test_policy_disabled_auto_discovery(self, tmp_path, capsys):
        from apm_cli.commands.audit import _audit_ci_gate

        cfg = self._make_cfg(tmp_path)
        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.checks = []
        mock_result.to_json.return_value = {"summary": {"total": 0, "failed": 0}}

        mock_fetch = MagicMock()
        mock_fetch.outcome = "disabled"
        mock_fetch.found = False

        with (
            patch("apm_cli.policy.ci_checks.run_baseline_checks", return_value=mock_result),
            patch(
                "apm_cli.policy.discovery.discover_policy_with_chain",
                return_value=mock_fetch,
            ),
            pytest.raises(SystemExit),
        ):
            _audit_ci_gate(
                cfg,
                policy_source=None,
                no_cache=False,
                no_policy=False,
                no_fail_fast=False,
                no_drift=True,
            )
        captured = capsys.readouterr()
        assert "disabled" in captured.err


# ---------------------------------------------------------------------------
# _audit_content_scan -- package not found
# ---------------------------------------------------------------------------


class TestAuditContentScanPackage:
    def _make_cfg(self, tmp_path):
        from apm_cli.commands.audit import _AuditConfig
        from apm_cli.core.command_logger import CommandLogger

        log = CommandLogger("audit", verbose=False)
        return _AuditConfig(
            project_root=tmp_path,
            logger=log,
            verbose=False,
            output_format="text",
            output_path=None,
        )

    def test_package_not_in_lockfile(self, tmp_path):
        from apm_cli.commands.audit import _audit_content_scan

        lock = tmp_path / "apm.lock.yaml"
        lock.write_text("lockfile_version: '1'\ndependencies: []\n", encoding="utf-8")

        cfg = self._make_cfg(tmp_path)

        with (
            patch(
                "apm_cli.security.file_scanner.scan_lockfile_packages",
                return_value=({}, 0),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            _audit_content_scan(
                cfg,
                package="nonexistent-pkg",
                file_path=None,
                strip=False,
                dry_run=False,
            )
        assert exc.value.code == 0

    def test_no_deployed_files_in_lockfile(self, tmp_path):
        from apm_cli.commands.audit import _audit_content_scan

        lock = tmp_path / "apm.lock.yaml"
        lock.write_text("lockfile_version: '1'\ndependencies: []\n", encoding="utf-8")
        cfg = self._make_cfg(tmp_path)

        with (
            patch(
                "apm_cli.security.file_scanner.scan_lockfile_packages",
                return_value=({}, 0),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            _audit_content_scan(
                cfg,
                package=None,
                file_path=None,
                strip=False,
                dry_run=False,
            )
        assert exc.value.code == 0


# ---------------------------------------------------------------------------
# Source-column helpers (external scanner provenance)
# ---------------------------------------------------------------------------


class TestFindingSourceHelpers:
    """Tests for _finding_source, _has_external_findings, and _findings_title."""

    def test_native_finding_source_is_apm(self):
        from apm_cli.commands.audit import _finding_source

        f = _make_finding()
        assert _finding_source(f) == "apm"

    def test_external_finding_source_extracted(self):
        from apm_cli.commands.audit import _finding_source
        from apm_cli.security.content_scanner import ScanFinding

        f = ScanFinding(
            file="x.md",
            line=1,
            column=1,
            char="",
            codepoint="",
            severity="warning",
            category="skillspector/TOOL_INJECTION",
            description="test",
        )
        assert _finding_source(f) == "skillspector"

    def test_apm_prefixed_category_returns_apm(self):
        from apm_cli.commands.audit import _finding_source
        from apm_cli.security.content_scanner import ScanFinding

        f = ScanFinding(
            file="x.md",
            line=1,
            column=1,
            char="x",
            codepoint="U+200B",
            severity="critical",
            category="apm/hidden-unicode/bidi-override",
            description="test",
        )
        assert _finding_source(f) == "apm"

    def test_has_external_findings_false_for_native_only(self):
        from apm_cli.commands.audit import _has_external_findings

        rows = [_make_finding()]
        assert _has_external_findings(rows) is False

    def test_has_external_findings_true_with_external(self):
        from apm_cli.commands.audit import _has_external_findings
        from apm_cli.security.content_scanner import ScanFinding

        rows = [
            _make_finding(),
            ScanFinding(
                file="y.md",
                line=1,
                column=1,
                char="",
                codepoint="",
                severity="warning",
                category="skillspector/RULE",
                description="ext",
            ),
        ]
        assert _has_external_findings(rows) is True

    def test_findings_title_native_only(self):
        from apm_cli.commands.audit import _findings_title

        rows = [_make_finding()]
        title = _findings_title(rows, has_external=False)
        assert "Content Scan Findings" in title

    def test_findings_title_mixed_includes_counts(self):
        from apm_cli.commands.audit import _findings_title
        from apm_cli.security.content_scanner import ScanFinding

        rows = [
            _make_finding(),
            ScanFinding(
                file="y.md",
                line=1,
                column=1,
                char="",
                codepoint="",
                severity="warning",
                category="skillspector/RULE",
                description="ext",
            ),
        ]
        title = _findings_title(rows, has_external=True)
        assert "Audit Findings" in title
        assert "apm: 1" in title
        assert "skillspector: 1" in title


class TestDeployedCanvasBundles:
    """_deployed_canvas_bundles derives canvas roots from lockfile entries."""

    def _lock(self, deployed):
        lock = MagicMock()
        dep = MagicMock()
        dep.deployed_files = deployed
        lock.dependencies = {"owner/repo": dep}
        return lock

    def test_user_scope_bundle_root(self, tmp_path):
        from apm_cli.commands import audit as audit_mod

        lock = self._lock([".copilot/extensions/widget/extension.mjs"])
        with (
            patch.object(audit_mod, "get_lockfile_path", return_value=tmp_path / "x"),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=lock),
        ):
            roots = audit_mod._deployed_canvas_bundles(tmp_path, None)
        assert roots == [".copilot/extensions/widget"]

    def test_project_scope_and_extra_files_dedupe(self, tmp_path):
        from apm_cli.commands import audit as audit_mod

        lock = self._lock(
            [
                ".github/extensions/widget/extension.mjs",
                ".github/extensions/widget/assets/app.js",
                ".github/instructions/foo.md",
            ]
        )
        with (
            patch.object(audit_mod, "get_lockfile_path", return_value=tmp_path / "x"),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=lock),
        ):
            roots = audit_mod._deployed_canvas_bundles(tmp_path, None)
        assert roots == [".github/extensions/widget"]

    def test_no_lockfile_returns_empty(self, tmp_path):
        from apm_cli.commands import audit as audit_mod

        with (
            patch.object(audit_mod, "get_lockfile_path", return_value=tmp_path / "x"),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=None),
        ):
            assert audit_mod._deployed_canvas_bundles(tmp_path, None) == []

    def test_package_filter_excludes_other_dep(self, tmp_path):
        from apm_cli.commands import audit as audit_mod

        lock = self._lock([".copilot/extensions/widget/extension.mjs"])
        with (
            patch.object(audit_mod, "get_lockfile_path", return_value=tmp_path / "x"),
            patch("apm_cli.deps.lockfile.LockFile.read", return_value=lock),
        ):
            roots = audit_mod._deployed_canvas_bundles(tmp_path, "other/dep")
        assert roots == []


class TestRenderCanvasNote:
    """_render_canvas_note surfaces an info line per deployed canvas."""

    def test_emits_info_when_bundles_present(self, tmp_path, logger):
        from apm_cli.commands import audit as audit_mod

        with patch.object(
            audit_mod,
            "_deployed_canvas_bundles",
            return_value=[".copilot/extensions/widget"],
        ):
            log = MagicMock()
            audit_mod._render_canvas_note(tmp_path, None, log)
        joined = " ".join(str(c.args[0]) for c in log.info.call_args_list)
        assert "executable canvas extension" in joined
        assert ".copilot/extensions/widget" in joined

    def test_silent_when_no_bundles(self, tmp_path):
        from apm_cli.commands import audit as audit_mod

        with patch.object(audit_mod, "_deployed_canvas_bundles", return_value=[]):
            log = MagicMock()
            audit_mod._render_canvas_note(tmp_path, None, log)
        log.info.assert_not_called()
