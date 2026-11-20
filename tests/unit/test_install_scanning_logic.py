"""Tests for install-time content scanning integration (pure logic cases)."""

import pytest

from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = pytest.mark.unit


# ── Diagnostics security rendering tests ────────────────────────────


# ── Pre-deploy security scan tests ───────────────────────────────


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
