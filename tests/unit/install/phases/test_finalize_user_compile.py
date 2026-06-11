"""Unit tests for finalize.py post-install compile hook.

Covers _compile_user_root_contexts_after_install and its integration in run():

* _compile_user_root_contexts_after_install: calls compile_user_root_contexts
* _compile_user_root_contexts_after_install: logs when files are written
* run(): does NOT call compile for PROJECT scope
* run(): DOES call compile for USER scope
* run(): passes correct source_root to compile
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_install_context(scope=None, logger=None):
    """Create a mock InstallContext."""
    ctx = MagicMock()
    ctx.scope = scope
    ctx.logger = logger
    ctx.total_links_resolved = 0
    ctx.total_commands_integrated = 0
    ctx.total_hooks_integrated = 0
    ctx.total_instructions_integrated = 0
    ctx.installed_count = 1
    ctx.unpinned_count = 0
    ctx.installed_packages = []
    ctx.package_types = {}
    ctx.diagnostics = MagicMock()
    return ctx


# ---------------------------------------------------------------------------
# _compile_user_root_contexts_after_install tests
# ---------------------------------------------------------------------------


class TestCompileUserRootContextsAfterInstall:
    """Tests for _compile_user_root_contexts_after_install()."""

    def test_calls_compile_user_root_contexts(self):
        """Function calls compile_user_root_contexts with correct arguments."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import (
            _compile_user_root_contexts_after_install,
        )

        source_root = Path.home() / ".apm"
        ctx = _make_install_context(scope=InstallScope.USER)

        mock_compile = MagicMock(return_value=[])

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                side_effect=mock_compile,
            ),
        ):
            _compile_user_root_contexts_after_install(ctx)

        # Should have called compile_user_root_contexts
        mock_compile.assert_called_once()
        call_args = mock_compile.call_args
        # Check that source_root was passed
        assert call_args[0][1] == source_root
        # Check that dry_run=False
        assert call_args[1]["dry_run"] is False

    def test_logs_when_files_written(self):
        """When files are written, logger.verbose_detail is called."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import (
            _compile_user_root_contexts_after_install,
        )

        source_root = Path.home() / ".apm"
        mock_logger = MagicMock()
        ctx = _make_install_context(scope=InstallScope.USER, logger=mock_logger)

        # Two written files
        results = [
            {"target": "claude", "path": Path(".claude/CLAUDE.md"), "status": "written"},
            {"target": "vscode", "path": Path(".vscode/AGENTS.md"), "status": "written"},
        ]

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
        ):
            _compile_user_root_contexts_after_install(ctx)

        # Logger should have been called with verbose_detail
        mock_logger.verbose_detail.assert_called_once()
        call_str = str(mock_logger.verbose_detail.call_args)
        assert "claude" in call_str
        assert "vscode" in call_str

    def test_no_logging_when_no_files_written(self):
        """When no files written, logger not called."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import (
            _compile_user_root_contexts_after_install,
        )

        source_root = Path.home() / ".apm"
        mock_logger = MagicMock()
        ctx = _make_install_context(scope=InstallScope.USER, logger=mock_logger)

        # No written files
        results = [
            {"target": "claude", "path": None, "status": "skipped-no-instructions"},
        ]

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
        ):
            _compile_user_root_contexts_after_install(ctx)

        # Logger should NOT have been called
        mock_logger.verbose_detail.assert_not_called()

    def test_no_logging_when_logger_none(self):
        """When logger is None, no logging occurs."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import (
            _compile_user_root_contexts_after_install,
        )

        source_root = Path.home() / ".apm"
        ctx = _make_install_context(scope=InstallScope.USER, logger=None)

        # Files written, but logger is None
        results = [
            {"target": "claude", "path": Path(".claude/CLAUDE.md"), "status": "written"},
        ]

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
        ):
            # Should not raise
            _compile_user_root_contexts_after_install(ctx)


# ---------------------------------------------------------------------------
# finalize.run() integration tests
# ---------------------------------------------------------------------------


class TestFinalizeRunIntegration:
    """Tests for run() function's integration of compile hook."""

    def test_project_scope_no_compile(self):
        """When ctx.scope is PROJECT, compile hook is NOT called."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import run

        ctx = _make_install_context(scope=InstallScope.PROJECT)

        mock_compile = MagicMock()

        with patch(
            "apm_cli.install.phases.finalize._compile_user_root_contexts_after_install",
            side_effect=mock_compile,
        ):
            run(ctx)

        # Compile hook should NOT have been called
        mock_compile.assert_not_called()

    def test_user_scope_compile_called(self):
        """When ctx.scope is USER, compile hook IS called."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import run

        ctx = _make_install_context(scope=InstallScope.USER, logger=None)

        mock_compile = MagicMock()

        with patch(
            "apm_cli.install.phases.finalize._compile_user_root_contexts_after_install",
            side_effect=mock_compile,
        ):
            result = run(ctx)

        # Compile hook SHOULD have been called
        mock_compile.assert_called_once()
        mock_compile.assert_called_once_with(ctx)
        # Result should still be valid
        assert result is not None

    def test_none_scope_no_compile(self):
        """When ctx.scope is None, compile hook is NOT called."""
        from apm_cli.install.phases.finalize import run

        ctx = _make_install_context(scope=None)

        mock_compile = MagicMock()

        with patch(
            "apm_cli.install.phases.finalize._compile_user_root_contexts_after_install",
            side_effect=mock_compile,
        ):
            run(ctx)

        # Compile hook should NOT have been called
        mock_compile.assert_not_called()

    def test_run_returns_install_result(self):
        """run() returns an InstallResult object."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import run
        from apm_cli.models.results import InstallResult

        ctx = _make_install_context(scope=InstallScope.USER, logger=None)

        with patch(
            "apm_cli.install.phases.finalize._compile_user_root_contexts_after_install",
        ):
            result = run(ctx)

        # Should be an InstallResult
        assert isinstance(result, InstallResult)
        assert result.installed_count == 1

    def test_user_scope_compile_receives_context(self):
        """compile hook receives the correct context object."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import run

        ctx = _make_install_context(scope=InstallScope.USER, logger=None)

        mock_compile = MagicMock()

        with patch(
            "apm_cli.install.phases.finalize._compile_user_root_contexts_after_install",
            side_effect=mock_compile,
        ):
            run(ctx)

        # Verify the same context object was passed
        mock_compile.assert_called_once_with(ctx)

    def test_all_stats_collected_before_compile(self):
        """Stats are collected before compile hook is called."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.install.phases.finalize import run

        ctx = _make_install_context(scope=InstallScope.USER, logger=None)
        ctx.total_links_resolved = 5
        ctx.total_commands_integrated = 2
        ctx.total_hooks_integrated = 3
        ctx.total_instructions_integrated = 1

        compile_called = False

        def mock_compile_fn(call_ctx):
            nonlocal compile_called
            compile_called = True
            # At this point, the context should still have its stats
            assert call_ctx.total_links_resolved == 5
            assert call_ctx.total_commands_integrated == 2

        with patch(
            "apm_cli.install.phases.finalize._compile_user_root_contexts_after_install",
            side_effect=mock_compile_fn,
        ):
            run(ctx)

        assert compile_called
