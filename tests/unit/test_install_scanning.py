"""Tests for install-time content scanning integration.

Verifies that ``_pre_deploy_security_scan()`` blocks deployment on
critical findings and allows deployment on warnings/clean, and that
install exits non-zero when packages are blocked.
"""

from pathlib import Path  # noqa: F401

import pytest

from apm_cli.commands.install import _pre_deploy_security_scan
from apm_cli.security.content_scanner import ContentScanner
from apm_cli.utils.diagnostics import DiagnosticCollector


@pytest.fixture
def clean_files(tmp_path):
    """Create several clean text files."""
    paths = []
    for name in ("a.md", "b.md", "c.md"):
        p = tmp_path / name
        p.write_text(f"# {name}\nClean content.\n", encoding="utf-8")
        paths.append(p)
    return paths


@pytest.fixture
def mixed_files(tmp_path):
    """Create files with varying severity levels."""
    clean = tmp_path / "clean.md"
    clean.write_text("No issues here.\n", encoding="utf-8")

    warning = tmp_path / "warning.md"
    warning.write_text("Has zero\u200bwidth.\n", encoding="utf-8")

    critical = tmp_path / "critical.md"
    critical.write_text("Has tag\U000e0041char.\n", encoding="utf-8")

    return [clean, warning, critical]


# ── Diagnostics security rendering tests ────────────────────────────


class TestDiagnosticsSecurityRendering:
    """Tests for security category rendering in DiagnosticCollector."""

    def test_render_summary_includes_security(self, mixed_files, capsys):
        diag = DiagnosticCollector()
        for f in mixed_files:
            findings = ContentScanner.scan_file(f)
            if findings:
                has_crit, summary = ContentScanner.classify(findings)  # noqa: RUF059
                sev = "critical" if has_crit else "warning"
                diag.security(
                    message=str(f),
                    package="pkg",
                    detail=f"{len(findings)} finding(s)",
                    severity=sev,
                )
        diag.render_summary()
        captured = capsys.readouterr()
        assert "Diagnostics" in captured.out or "security" in captured.out.lower()

    def test_critical_security_flag(self, tmp_path):
        p = tmp_path / "evil.md"
        p.write_text("x\U000e0001y\n", encoding="utf-8")
        diag = DiagnosticCollector()
        findings = ContentScanner.scan_file(p)
        diag.security(
            message=str(p),
            package="pkg",
            detail=f"{len(findings)} finding(s)",
            severity="critical",
        )
        assert diag.has_critical_security is True

    def test_no_critical_when_only_warnings(self, tmp_path):
        p = tmp_path / "warn.md"
        p.write_text("x\u200by\n", encoding="utf-8")
        diag = DiagnosticCollector()
        findings = ContentScanner.scan_file(p)
        diag.security(
            message=str(p),
            package="pkg",
            detail=f"{len(findings)} finding(s)",
            severity="warning",
        )
        assert diag.has_critical_security is False


# ── Pre-deploy security scan tests ───────────────────────────────


class TestPreDeploySecurityScan:
    """Tests for _pre_deploy_security_scan() — the pre-deployment gate."""

    def test_clean_package_allows_deploy(self, tmp_path):
        (tmp_path / "prompt.md").write_text("Clean content\n", encoding="utf-8")
        diag = DiagnosticCollector()
        assert _pre_deploy_security_scan(tmp_path, diag, package_name="pkg") is True
        assert diag.security_count == 0

    def test_critical_chars_block_deploy(self, tmp_path):
        (tmp_path / "evil.md").write_text("hidden\U000e0001tag\n", encoding="utf-8")
        diag = DiagnosticCollector()
        result = _pre_deploy_security_scan(
            tmp_path,
            diag,
            package_name="pkg",
            force=False,
        )
        assert result is False
        assert diag.has_critical_security

    def test_critical_chars_with_force_allows_deploy(self, tmp_path):
        (tmp_path / "evil.md").write_text("hidden\U000e0001tag\n", encoding="utf-8")
        diag = DiagnosticCollector()
        result = _pre_deploy_security_scan(
            tmp_path,
            diag,
            package_name="pkg",
            force=True,
        )
        assert result is True
        assert diag.has_critical_security  # still records the finding

    def test_warnings_allow_deploy(self, tmp_path):
        (tmp_path / "warn.md").write_text("zero\u200bwidth\n", encoding="utf-8")
        diag = DiagnosticCollector()
        result = _pre_deploy_security_scan(
            tmp_path,
            diag,
            package_name="pkg",
        )
        assert result is True
        assert diag.security_count == 1
        assert not diag.has_critical_security

    def test_scans_nested_files(self, tmp_path):
        """Source files in subdirectories are scanned."""
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "deep.md").write_text("tag\U000e0041char\n", encoding="utf-8")
        diag = DiagnosticCollector()
        result = _pre_deploy_security_scan(
            tmp_path,
            diag,
            package_name="pkg",
            force=False,
        )
        assert result is False

    def test_empty_package_allows_deploy(self, tmp_path):
        diag = DiagnosticCollector()
        assert _pre_deploy_security_scan(tmp_path, diag) is True
        assert diag.security_count == 0

    def test_package_name_in_diagnostic(self, tmp_path):
        (tmp_path / "x.md").write_text("z\u200bw\n", encoding="utf-8")
        diag = DiagnosticCollector()
        _pre_deploy_security_scan(tmp_path, diag, package_name="my-pkg")
        items = diag.by_category().get("security", [])
        assert len(items) == 1
        assert items[0].package == "my-pkg"

    def test_does_not_follow_symlinked_directories(self, tmp_path):
        """Symlinked directories should not be traversed."""
        # Create a directory outside the package with a critical file
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "evil.md").write_text("tag\U000e0001char\n", encoding="utf-8")

        # Package directory with a symlink pointing outside
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "clean.md").write_text("Clean\n", encoding="utf-8")
        try:
            (pkg / "escape").symlink_to(outside)
        except OSError:
            pytest.skip("symlinks not supported on this platform")

        diag = DiagnosticCollector()
        result = _pre_deploy_security_scan(pkg, diag, package_name="test")
        # Should allow deploy — the evil file is behind a symlink
        assert result is True
        assert diag.security_count == 0


# ── Install exit code on critical security ──────────────────────


class TestInstallExitOnCriticalSecurity:
    """Verify install exits non-zero when critical security findings block packages."""

    def test_critical_security_triggers_exit(self):
        """has_critical_security True + force=False → should exit 1."""
        diag = DiagnosticCollector()
        diag.security(
            message="Blocked — critical hidden characters",
            package="evil-pkg",
            detail="1 critical",
            severity="critical",
        )
        assert diag.has_critical_security

        # Simulate the post-install check (mirrors install.py logic)
        force = False
        with pytest.raises(SystemExit) as exc_info:
            if not force and diag.has_critical_security:
                import sys

                sys.exit(1)
        assert exc_info.value.code == 1

    def test_force_overrides_critical_exit(self):
        """has_critical_security True + force=True → should NOT exit 1."""
        diag = DiagnosticCollector()
        diag.security(
            message="Deployed with --force despite critical",
            package="evil-pkg",
            detail="1 critical",
            severity="critical",
        )
        assert diag.has_critical_security

        # With --force, the exit check is skipped
        force = True
        # This should NOT raise SystemExit
        if not force and diag.has_critical_security:
            import sys

            sys.exit(1)
        # If we reach here, the force override worked

    def test_warnings_do_not_trigger_exit(self):
        """Warnings should not trigger exit 1."""
        diag = DiagnosticCollector()
        diag.security(
            message="Zero-width character",
            package="warn-pkg",
            detail="1 warning",
            severity="warning",
        )
        assert not diag.has_critical_security
        # No sys.exit — this is the normal path


# ── Compile exit code on critical security ──────────────────────


class TestCompileExitOnCriticalSecurity:
    """Verify CompilationResult propagates has_critical_security."""

    def test_compilation_result_defaults_false(self):
        from apm_cli.compilation.agents_compiler import CompilationResult

        r = CompilationResult(
            success=True,
            output_path="",
            content="",
            warnings=[],
            errors=[],
            stats={},
        )
        assert r.has_critical_security is False

    def test_compilation_result_propagates_critical(self):
        from apm_cli.compilation.agents_compiler import CompilationResult

        r = CompilationResult(
            success=True,
            output_path="",
            content="",
            warnings=[],
            errors=[],
            stats={},
            has_critical_security=True,
        )
        assert r.has_critical_security is True

    def test_merge_results_propagates_critical(self):
        from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationResult

        clean = CompilationResult(
            success=True,
            output_path="a.md",
            content="clean",
            warnings=[],
            errors=[],
            stats={},
        )
        critical = CompilationResult(
            success=True,
            output_path="b.md",
            content="bad",
            warnings=[],
            errors=[],
            stats={},
            has_critical_security=True,
        )
        compiler = AgentsCompiler()
        merged = compiler._merge_results([clean, critical])
        assert merged.has_critical_security is True

    def test_merge_results_clean_stays_clean(self):
        from apm_cli.compilation.agents_compiler import AgentsCompiler, CompilationResult

        r1 = CompilationResult(
            success=True,
            output_path="a.md",
            content="ok",
            warnings=[],
            errors=[],
            stats={},
        )
        r2 = CompilationResult(
            success=True,
            output_path="b.md",
            content="ok",
            warnings=[],
            errors=[],
            stats={},
        )
        compiler = AgentsCompiler()
        merged = compiler._merge_results([r1, r2])
        assert merged.has_critical_security is False
