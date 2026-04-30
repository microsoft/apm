"""Unit tests for SecurityGate — centralized scan→classify→decide→report."""

import os  # noqa: F401
import textwrap  # noqa: F401
from pathlib import Path
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# U+E0100 is a VS17 variation selector — always "critical"
CRITICAL_CHAR = "\U000e0100"
# U+200B is a zero-width space — "warning"
WARNING_CHAR = "\u200b"


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# scan_files
# ---------------------------------------------------------------------------


class TestScanFiles:
    def test_clean_directory(self, tmp_path):
        _write_file(tmp_path / "a.md", "clean content")
        _write_file(tmp_path / "b.md", "also clean")
        v = SecurityGate.scan_files(tmp_path, policy=BLOCK_POLICY)
        assert not v.has_findings
        assert not v.has_critical
        assert not v.should_block
        assert v.files_scanned == 2

    def test_critical_blocks(self, tmp_path):
        _write_file(tmp_path / "evil.md", f"payload {CRITICAL_CHAR} here")
        v = SecurityGate.scan_files(tmp_path, policy=BLOCK_POLICY)
        assert v.has_critical
        assert v.should_block
        assert v.critical_count >= 1

    def test_critical_with_force_does_not_block(self, tmp_path):
        _write_file(tmp_path / "evil.md", f"payload {CRITICAL_CHAR} here")
        v = SecurityGate.scan_files(tmp_path, policy=BLOCK_POLICY, force=True)
        assert v.has_critical
        assert not v.should_block  # force overrides

    def test_warn_policy_never_blocks(self, tmp_path):
        _write_file(tmp_path / "evil.md", f"payload {CRITICAL_CHAR} here")
        v = SecurityGate.scan_files(tmp_path, policy=WARN_POLICY)
        assert v.has_critical
        assert not v.should_block

    def test_warning_findings_dont_block(self, tmp_path):
        _write_file(tmp_path / "warn.md", f"zero-width{WARNING_CHAR}space")
        v = SecurityGate.scan_files(tmp_path, policy=BLOCK_POLICY)
        assert v.has_findings
        assert not v.has_critical
        assert not v.should_block
        assert v.warning_count >= 1

    def test_symlinks_skipped(self, tmp_path):
        target = tmp_path / "real.md"
        _write_file(target, f"critical {CRITICAL_CHAR}")
        link = tmp_path / "link.md"
        link.symlink_to(target)
        # Only real.md should be scanned, link.md skipped
        v = SecurityGate.scan_files(tmp_path, policy=BLOCK_POLICY)
        assert v.files_scanned == 1  # only real.md

    def test_scans_all_files_for_complete_report(self, tmp_path):
        """All files are scanned even when critical is found — no short-circuit."""
        _write_file(tmp_path / "a.md", f"critical {CRITICAL_CHAR}")
        _write_file(tmp_path / "b.md", f"also critical {CRITICAL_CHAR}")
        v = SecurityGate.scan_files(tmp_path, policy=BLOCK_POLICY)
        assert v.should_block
        assert v.has_critical
        # Both files must be in findings (no early termination)
        assert len(v.findings_by_file) == 2

    def test_report_policy_ignores_critical(self, tmp_path):
        _write_file(tmp_path / "evil.md", f"critical {CRITICAL_CHAR}")
        v = SecurityGate.scan_files(tmp_path, policy=REPORT_POLICY)
        assert v.has_critical
        assert not v.should_block  # never blocks


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


class TestRobustness:
    def test_unreadable_file_skipped(self, tmp_path):
        """scan_files gracefully skips files that raise OSError."""
        good = tmp_path / "ok.md"
        _write_file(good, "clean")
        bad = tmp_path / "bad.md"
        _write_file(bad, "also clean")
        # Make file unreadable
        bad.chmod(0o000)
        try:
            v = SecurityGate.scan_files(tmp_path, policy=BLOCK_POLICY)
            assert not v.has_findings
            # At least the readable file was scanned
            assert v.files_scanned >= 1
        finally:
            bad.chmod(0o644)
