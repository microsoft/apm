"""Tests for the content scanner module (filesystem, process and CLI boundary cases)."""

import tempfile  # noqa: F401
from pathlib import Path  # noqa: F401

import pytest

from apm_cli.security.content_scanner import (
    ContentScanner,
)

pytestmark = pytest.mark.component


class TestScanFile:
    """Tests for ContentScanner.scan_file()."""

    def test_scan_clean_file(self, tmp_path):
        f = tmp_path / "clean.md"
        f.write_text("# Clean file\nNo issues here.", encoding="utf-8")
        findings = ContentScanner.scan_file(f)
        assert findings == []

    def test_scan_file_with_findings(self, tmp_path):
        f = tmp_path / "suspicious.md"
        f.write_text("hello\u200bworld", encoding="utf-8")
        findings = ContentScanner.scan_file(f)
        assert len(findings) == 1
        assert findings[0].file == str(f)

    def test_binary_file_returns_empty(self, tmp_path):
        f = tmp_path / "binary.bin"
        f.write_bytes(b"\x80\x81\x82\xff\xfe")
        findings = ContentScanner.scan_file(f)
        assert findings == []

    def test_nonexistent_file_returns_empty(self, tmp_path):
        f = tmp_path / "does_not_exist.md"
        findings = ContentScanner.scan_file(f)
        assert findings == []

    def test_latin1_file_returns_empty(self, tmp_path):
        """Non-UTF-8 encoded files should be skipped gracefully."""
        f = tmp_path / "latin1.txt"
        f.write_bytes("Stra\xdfe".encode("latin-1"))
        findings = ContentScanner.scan_file(f)
        assert findings == []

    def test_bom_plus_critical_detected(self, tmp_path):
        """Files with BOM and critical chars should report both."""
        f = tmp_path / "bom_critical.md"
        f.write_text("\ufeff" + "tag\U000e0041char\n", encoding="utf-8")
        findings = ContentScanner.scan_file(f)
        severities = {fnd.severity for fnd in findings}
        assert "critical" in severities
        assert "info" in severities  # Leading BOM is info-level
