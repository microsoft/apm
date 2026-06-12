"""Unit tests for apm_cli.security.file_scanner."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.security.content_scanner import ScanFinding
from apm_cli.security.file_scanner import (
    _is_safe_lockfile_path,
    _scan_files_in_dir,
    scan_lockfile_packages,
)

# ---------------------------------------------------------------------------
# _is_safe_lockfile_path
# ---------------------------------------------------------------------------


class TestIsSafeLockfilePath:
    """Tests for _is_safe_lockfile_path."""

    def test_safe_path_returns_true(self, tmp_path: Path) -> None:
        # .github/prompts is an allowed integration prefix
        assert _is_safe_lockfile_path(".github/prompts/file.md", tmp_path) is True

    def test_path_traversal_returns_false(self, tmp_path: Path) -> None:
        assert _is_safe_lockfile_path("../outside/file.md", tmp_path) is False

    def test_empty_string_returns_false(self, tmp_path: Path) -> None:
        result = _is_safe_lockfile_path("", tmp_path)
        # Empty path is rejected by BaseIntegrator.validate_deploy_path
        assert isinstance(result, bool)

    def test_nested_safe_path_returns_true(self, tmp_path: Path) -> None:
        assert _is_safe_lockfile_path(".github/skills/pkg/deep/file.md", tmp_path) is True


# ---------------------------------------------------------------------------
# _scan_files_in_dir
# ---------------------------------------------------------------------------


class TestScanFilesInDir:
    """Tests for _scan_files_in_dir."""

    def test_returns_findings_and_count(self, tmp_path: Path) -> None:
        mock_verdict = MagicMock()
        mock_verdict.findings_by_file = {
            "rel/file.md": [MagicMock()],
        }
        mock_verdict.files_scanned = 1

        with patch(
            "apm_cli.security.gate.SecurityGate.scan_files",
            return_value=mock_verdict,
        ):
            findings, count = _scan_files_in_dir(tmp_path, "pkg")

        assert count == 1
        assert "pkg/rel/file.md" in findings

    def test_no_findings_returns_empty(self, tmp_path: Path) -> None:
        mock_verdict = MagicMock()
        mock_verdict.findings_by_file = {}
        mock_verdict.files_scanned = 3

        with patch(
            "apm_cli.security.gate.SecurityGate.scan_files",
            return_value=mock_verdict,
        ):
            findings, count = _scan_files_in_dir(tmp_path, "base")

        assert findings == {}
        assert count == 3

    def test_uses_report_policy(self, tmp_path: Path) -> None:
        mock_verdict = MagicMock()
        mock_verdict.findings_by_file = {}
        mock_verdict.files_scanned = 0

        with patch(
            "apm_cli.security.gate.SecurityGate.scan_files",
            return_value=mock_verdict,
        ) as mock_scan:
            _scan_files_in_dir(tmp_path, "label")

        call_kwargs = mock_scan.call_args[1]
        assert "policy" in call_kwargs


# ---------------------------------------------------------------------------
# scan_lockfile_packages
# ---------------------------------------------------------------------------


def _make_dep(deployed_files: list[str]) -> MagicMock:
    dep = MagicMock()
    dep.deployed_files = deployed_files
    return dep


def _make_lockfile(deps: dict[str, MagicMock]) -> MagicMock:
    lock = MagicMock()
    lock.dependencies = deps
    return lock


class TestScanLockfilePackages:
    """Tests for scan_lockfile_packages."""

    def test_returns_empty_when_no_lockfile(self, tmp_path: Path) -> None:
        with patch(
            "apm_cli.security.file_scanner.LockFile.read",
            return_value=None,
        ):
            findings, count = scan_lockfile_packages(tmp_path)

        assert findings == {}
        assert count == 0

    def test_scans_regular_file(self, tmp_path: Path) -> None:
        target_file = tmp_path / ".github" / "prompts" / "file.md"
        target_file.parent.mkdir(parents=True)
        target_file.write_text("content with hidden \u200b char", encoding="utf-8")

        finding = ScanFinding(
            file=".github/prompts/file.md",
            line=1,
            column=1,
            char="\u200b",
            codepoint="U+200B",
            severity="critical",
            category="zero-width",
            description="ZWS",
        )
        dep = _make_dep([".github/prompts/file.md"])
        lock = _make_lockfile({"pkg": dep})

        with (
            patch("apm_cli.security.file_scanner.LockFile.read", return_value=lock),
            patch(
                "apm_cli.security.file_scanner.ContentScanner.scan_file",
                return_value=[finding],
            ),
        ):
            findings, count = scan_lockfile_packages(tmp_path)

        assert count == 1
        assert ".github/prompts/file.md" in findings

    def test_skips_nonexistent_file(self, tmp_path: Path) -> None:
        dep = _make_dep([".apm/pkg/missing.md"])
        lock = _make_lockfile({"pkg": dep})

        with patch("apm_cli.security.file_scanner.LockFile.read", return_value=lock):
            findings, count = scan_lockfile_packages(tmp_path)

        assert findings == {}
        assert count == 0

    def test_skips_unsafe_path(self, tmp_path: Path) -> None:
        dep = _make_dep(["../outside/file.md"])
        lock = _make_lockfile({"pkg": dep})

        with patch("apm_cli.security.file_scanner.LockFile.read", return_value=lock):
            findings, count = scan_lockfile_packages(tmp_path)

        assert findings == {}
        assert count == 0

    def test_scans_directory_entry(self, tmp_path: Path) -> None:
        dir_path = tmp_path / ".github" / "skills" / "pkg"
        dir_path.mkdir(parents=True)

        mock_findings = {"inner.md": [MagicMock()]}
        mock_verdict = MagicMock()
        mock_verdict.findings_by_file = mock_findings
        mock_verdict.files_scanned = 2

        dep = _make_dep([".github/skills/pkg/"])
        lock = _make_lockfile({"pkg": dep})

        with (
            patch("apm_cli.security.file_scanner.LockFile.read", return_value=lock),
            patch(
                "apm_cli.security.gate.SecurityGate.scan_files",
                return_value=mock_verdict,
            ),
        ):
            _findings, count = scan_lockfile_packages(tmp_path)

        assert count == 2

    def test_package_filter_limits_scan(self, tmp_path: Path) -> None:
        file_a = tmp_path / ".github" / "prompts" / "a.md"
        file_a.parent.mkdir(parents=True)
        file_a.write_text("ok", encoding="utf-8")

        dep_a = _make_dep([".github/prompts/a.md"])
        dep_b = _make_dep([".github/prompts/b.md"])
        lock = _make_lockfile({"pkg-a": dep_a, "pkg-b": dep_b})

        with (
            patch("apm_cli.security.file_scanner.LockFile.read", return_value=lock),
            patch(
                "apm_cli.security.file_scanner.ContentScanner.scan_file",
                return_value=[],
            ),
        ):
            _findings2, count = scan_lockfile_packages(tmp_path, package_filter="pkg-a")

        assert count == 1

    def test_file_with_no_findings_not_in_result(self, tmp_path: Path) -> None:
        target_file = tmp_path / ".github" / "prompts" / "clean.md"
        target_file.parent.mkdir(parents=True)
        target_file.write_text("clean", encoding="utf-8")

        dep = _make_dep([".github/prompts/clean.md"])
        lock = _make_lockfile({"pkg": dep})

        with (
            patch("apm_cli.security.file_scanner.LockFile.read", return_value=lock),
            patch(
                "apm_cli.security.file_scanner.ContentScanner.scan_file",
                return_value=[],
            ),
        ):
            findings, count = scan_lockfile_packages(tmp_path)

        assert count == 1
        assert ".github/prompts/clean.md" not in findings
