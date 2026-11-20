"""Unit tests for audit report serialization (JSON and SARIF) (filesystem, process and CLI boundary cases)."""

import json

import pytest

from apm_cli.security.audit_report import (
    findings_to_json,
    write_report,
)

pytestmark = pytest.mark.component


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
