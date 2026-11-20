"""Unit tests for apm_cli.commands.audit (pure logic cases)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

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


# ---------------------------------------------------------------------------
# _preview_strip
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _audit_content_scan
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _audit_ci_gate  -- skipping/no-drift
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _audit_content_scan -- package not found
# ---------------------------------------------------------------------------


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
