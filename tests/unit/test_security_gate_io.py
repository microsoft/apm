"""Unit tests for SecurityGate - centralized scan-classify-decide-report (filesystem, process and CLI boundary cases)."""

import os  # noqa: F401
import textwrap  # noqa: F401
from pathlib import Path

import pytest

from apm_cli.security.gate import (
    BLOCK_POLICY,
    REPORT_POLICY,
    WARN_POLICY,
    SecurityGate,
)

pytestmark = pytest.mark.component

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
        assert v.scanned_files == frozenset({"a.md", "b.md"})

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

    def test_explicit_paths_scan_without_walking_source_only_directories(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Authorized install plans scan only their exact paths."""
        _write_file(tmp_path / "authorized.md", "clean content")
        _write_file(tmp_path / "source-only" / "hostile.md", f"critical {CRITICAL_CHAR}")

        def fail_walk(_root, **_kwargs):
            raise AssertionError("explicit paths must not walk the source tree")

        monkeypatch.setattr("apm_cli.security.gate.os.walk", fail_walk)
        verdict = SecurityGate.scan_files(
            tmp_path,
            policy=BLOCK_POLICY,
            paths=frozenset({"authorized.md"}),
        )

        assert verdict.should_block is False
        assert verdict.scanned_files == frozenset({"authorized.md"})

    def test_explicit_paths_reject_symlinked_parent_escape(self, tmp_path):
        outside = tmp_path.parent / f"{tmp_path.name}-outside"
        outside.mkdir()
        _write_file(outside / "hostile.md", f"critical {CRITICAL_CHAR}")
        (tmp_path / "link").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="contains a symlink"):
            SecurityGate.scan_files(
                tmp_path,
                policy=BLOCK_POLICY,
                paths=frozenset({"link/hostile.md"}),
            )

    def test_report_policy_ignores_critical(self, tmp_path):
        _write_file(tmp_path / "evil.md", f"critical {CRITICAL_CHAR}")
        v = SecurityGate.scan_files(tmp_path, policy=REPORT_POLICY)
        assert v.has_critical
        assert not v.should_block  # never blocks


# ---------------------------------------------------------------------------
# scan_text
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ScanVerdict properties
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Policy presets
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


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
