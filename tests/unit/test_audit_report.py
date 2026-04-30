"""Unit tests for audit report serialization (JSON and SARIF)."""

import json
from pathlib import Path

from apm_cli.security.audit_report import (
    detect_format_from_extension,
    findings_to_json,
    findings_to_markdown,
    findings_to_sarif,
    serialize_report,
    write_report,
)
from apm_cli.security.content_scanner import ScanFinding


def _make_finding(
    severity="critical",
    category="tag-character",
    file="test.md",
    line=1,
    column=1,
):
    return ScanFinding(
        file=file,
        line=line,
        column=column,
        char="'\U000e0041'",
        codepoint="U+E0041",
        severity=severity,
        category=category,
        description="Tag Latin small letter A",
    )


class TestJsonReport:
    def test_empty_findings(self):
        report = findings_to_json({}, files_scanned=5, exit_code=0)
        assert report["version"] == "1"
        assert report["exit_code"] == 0
        assert report["summary"]["files_scanned"] == 5
        assert report["findings"] == []

    def test_findings_counted_by_severity(self):
        findings = {
            "a.md": [
                _make_finding(severity="critical"),
                _make_finding(severity="warning"),
            ],
            "b.md": [_make_finding(severity="info")],
        }
        report = findings_to_json(findings, files_scanned=10, exit_code=1)
        assert report["summary"]["critical"] == 1
        assert report["summary"]["warning"] == 1
        assert report["summary"]["info"] == 1
        assert report["summary"]["files_affected"] == 2
        assert len(report["findings"]) == 3

    def test_finding_fields_complete(self):
        findings = {"test.md": [_make_finding(line=14, column=23)]}
        report = findings_to_json(findings, files_scanned=1, exit_code=1)
        f = report["findings"][0]
        assert f["severity"] == "critical"
        assert f["file"] == "test.md"
        assert f["line"] == 14
        assert f["column"] == 23
        assert f["codepoint"] == "U+E0041"
        assert f["category"] == "tag-character"

    def test_serializable(self):
        findings = {"test.md": [_make_finding()]}
        report = findings_to_json(findings, files_scanned=1, exit_code=1)
        text = serialize_report(report)
        assert '"version": "1"' in text


class TestSarifReport:
    def test_sarif_schema_present(self):
        report = findings_to_sarif({}, files_scanned=0)
        assert report["version"] == "2.1.0"
        assert "$schema" in report
        assert len(report["runs"]) == 1

    def test_sarif_rules_deduped(self):
        findings = {
            "a.md": [
                _make_finding(category="tag-character"),
                _make_finding(category="tag-character"),
                _make_finding(category="bidi-override"),
            ],
        }
        report = findings_to_sarif(findings, files_scanned=1)
        rules = report["runs"][0]["tool"]["driver"]["rules"]
        rule_ids = [r["id"] for r in rules]
        assert "apm/hidden-unicode/tag-character" in rule_ids
        assert "apm/hidden-unicode/bidi-override" in rule_ids
        assert len(rule_ids) == 2

    def test_sarif_severity_mapping(self):
        findings = {
            "a.md": [
                _make_finding(severity="critical"),
                _make_finding(severity="warning"),
                _make_finding(severity="info"),
            ],
        }
        report = findings_to_sarif(findings, files_scanned=1)
        levels = [r["level"] for r in report["runs"][0]["results"]]
        assert levels == ["error", "warning", "note"]

    def test_sarif_location_uses_relative_paths(self):
        findings = {
            "some/path/file.md": [
                _make_finding(file="some/path/file.md", line=5, column=10),
            ],
        }
        report = findings_to_sarif(findings, files_scanned=1)
        loc = report["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
        assert loc["artifactLocation"]["uri"] == "some/path/file.md"
        assert loc["region"]["startLine"] == 5
        assert loc["region"]["startColumn"] == 10

    def test_sarif_no_content_snippets(self):
        """SARIF must NOT include file content snippets for privacy."""
        findings = {"test.md": [_make_finding()]}
        report = findings_to_sarif(findings, files_scanned=1)
        text = json.dumps(report)
        assert "snippet" not in text

    def test_sarif_files_scanned_in_invocation(self):
        report = findings_to_sarif({}, files_scanned=42)
        inv = report["runs"][0]["invocations"][0]
        assert inv["properties"]["filesScanned"] == 42


class TestFormatDetection:
    def test_sarif_extension(self):
        assert detect_format_from_extension(Path("report.sarif")) == "sarif"

    def test_sarif_json_extension(self):
        assert detect_format_from_extension(Path("report.sarif.json")) == "sarif"

    def test_json_extension(self):
        assert detect_format_from_extension(Path("report.json")) == "json"

    def test_unknown_extension_defaults_text(self):
        assert detect_format_from_extension(Path("report.txt")) == "text"

    def test_md_extension(self):
        assert detect_format_from_extension(Path("report.md")) == "markdown"


class TestWriteReport:
    def test_write_creates_file(self, tmp_path):
        report = findings_to_json({}, files_scanned=0, exit_code=0)
        out = tmp_path / "sub" / "report.json"
        write_report(report, out)
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["version"] == "1"

    def test_write_trailing_newline(self, tmp_path):
        report = findings_to_json({}, files_scanned=0, exit_code=0)
        out = tmp_path / "report.json"
        write_report(report, out)
        assert out.read_text(encoding="utf-8").endswith("\n")


class TestMarkdownReport:
    def test_clean_report(self):
        md = findings_to_markdown({}, files_scanned=15)
        assert "## APM Audit Report" in md
        assert "**Clean**" in md
        assert "15 files" in md

    def test_findings_report_header(self):
        findings = {
            "skills/fix.md": [_make_finding(severity="critical", file="skills/fix.md", line=42)],
            "rules/.cursorrules": [
                _make_finding(severity="warning", file="rules/.cursorrules", line=1),
                _make_finding(severity="warning", file="rules/.cursorrules", line=3),
            ],
        }
        md = findings_to_markdown(findings, files_scanned=15)
        assert "**3 findings**" in md
        assert "2 files" in md
        assert "1 critical" in md
        assert "2 warnings" in md
        assert "15 files scanned" in md

    def test_findings_sorted_critical_first(self):
        findings = {
            "b.md": [_make_finding(severity="warning", file="b.md", line=5)],
            "a.md": [_make_finding(severity="critical", file="a.md", line=1)],
        }
        md = findings_to_markdown(findings, files_scanned=2)
        lines = md.split("\n")
        table_rows = [l for l in lines if l.startswith("| CRITICAL") or l.startswith("| WARNING")]  # noqa: E741
        assert table_rows[0].startswith("| CRITICAL")
        assert table_rows[1].startswith("| WARNING")

    def test_table_has_backticks(self):
        findings = {"test.md": [_make_finding(file="test.md")]}
        md = findings_to_markdown(findings, files_scanned=1)
        assert "`test.md`" in md
        assert "`U+E0041`" in md

    def test_info_findings_included(self):
        findings = {"test.md": [_make_finding(severity="info", file="test.md")]}
        md = findings_to_markdown(findings, files_scanned=1)
        assert "| INFO |" in md

    def test_strip_hint_present(self):
        findings = {"test.md": [_make_finding(file="test.md")]}
        md = findings_to_markdown(findings, files_scanned=1)
        assert "`apm audit --strip`" in md
