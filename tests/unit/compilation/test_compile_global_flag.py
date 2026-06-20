"""Unit tests for compile --global CLI flag.

Covers the _handle_global_flag function and --global integration in the compile command:

* _handle_global_flag: error when apm_modules missing
* _handle_global_flag: success when results present
* _handle_global_flag: result status printing (written, unchanged, would-write, etc.)
* _handle_global_flag: error accumulation and exit code
* compile command: --global with --watch rejected
* compile command: --global with --root rejected
* compile command: --global without errors exits 0
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(target: str, path: str | None, status: str) -> SimpleNamespace:
    """Create a result object as returned by compile_user_root_contexts."""
    return SimpleNamespace(target=target, path=Path(path) if path else None, status=status)


# ---------------------------------------------------------------------------
# _handle_global_flag tests
# ---------------------------------------------------------------------------


class TestHandleGlobalFlag:
    """Tests for _handle_global_flag()."""

    def test_no_apm_modules_returns_error(self, tmp_path):
        """apm_modules missing -> returns 1 and prints error."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        # apm_modules does NOT exist

        mock_rich_error = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.utils.console._rich_error",
                new=mock_rich_error,
            ),
        ):
            rc = _handle_global_flag(dry_run=False)

        assert rc == 1
        mock_rich_error.assert_called_once()
        assert "apm_modules not found" in str(mock_rich_error.call_args).lower()

    def test_success_written_status(self, tmp_path):
        """Result with 'written' status -> prints [+] and returns 0."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = [_make_result("claude", str(tmp_path / ".claude/CLAUDE.md"), "written")]

        mock_rich_success = MagicMock()
        mock_rich_info = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
            patch(
                "apm_cli.utils.console._rich_success",
                new=mock_rich_success,
            ),
            patch(
                "apm_cli.utils.console._rich_info",
                new=mock_rich_info,
            ),
        ):
            rc = _handle_global_flag(dry_run=False)

        assert rc == 0
        # Should call _rich_success with [+]
        calls_str = str(mock_rich_success.call_args_list)
        assert "[+]" in calls_str or "claude" in calls_str.lower()

    def test_success_would_write_status(self, tmp_path):
        """Result with 'would-write' status -> prints [*] and returns 0."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = [_make_result("claude", str(tmp_path / ".claude/CLAUDE.md"), "would-write")]

        mock_rich_info = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
            patch(
                "apm_cli.utils.console._rich_info",
                new=mock_rich_info,
            ),
        ):
            rc = _handle_global_flag(dry_run=True)

        assert rc == 0
        # Should call _rich_info with [*]
        calls_str = str(mock_rich_info.call_args_list)
        assert "[*]" in calls_str or "would" in calls_str.lower()

    def test_success_unchanged_status(self, tmp_path):
        """Result with 'unchanged' status -> prints [i] and returns 0."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = [_make_result("claude", str(tmp_path / ".claude/CLAUDE.md"), "unchanged")]

        mock_rich_info = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
            patch(
                "apm_cli.utils.console._rich_info",
                new=mock_rich_info,
            ),
        ):
            rc = _handle_global_flag(dry_run=False)

        assert rc == 0
        calls_str = str(mock_rich_info.call_args_list)
        assert "[i]" in calls_str or "unchanged" in calls_str.lower()

    def test_success_skipped_no_instructions(self, tmp_path):
        """Result with 'skipped-no-instructions' -> prints [i] and returns 0."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = [_make_result("claude", None, "skipped-no-instructions")]

        mock_rich_info = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
            patch(
                "apm_cli.utils.console._rich_info",
                new=mock_rich_info,
            ),
        ):
            rc = _handle_global_flag(dry_run=False)

        assert rc == 0
        calls_str = str(mock_rich_info.call_args_list)
        assert "[i]" in calls_str or "skipped" in calls_str.lower()

    def test_success_skipped_hand_authored(self, tmp_path):
        """Result with 'skipped-hand-authored' -> prints [i] and returns 0."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = [
            _make_result("claude", str(tmp_path / ".claude/CLAUDE.md"), "skipped-hand-authored")
        ]

        mock_rich_info = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
            patch(
                "apm_cli.utils.console._rich_info",
                new=mock_rich_info,
            ),
        ):
            rc = _handle_global_flag(dry_run=False)

        assert rc == 0

    def test_error_status_returns_1(self, tmp_path):
        """Result with 'error:...' status -> prints [x] and returns 1."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = [_make_result("claude", str(tmp_path / ".claude/CLAUDE.md"), "error:disk full")]

        mock_rich_error = MagicMock()
        mock_rich_info = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
            patch(
                "apm_cli.utils.console._rich_error",
                new=mock_rich_error,
            ),
            patch(
                "apm_cli.utils.console._rich_info",
                new=mock_rich_info,
            ),
        ):
            rc = _handle_global_flag(dry_run=False)

        assert rc == 1
        # Should call _rich_error
        mock_rich_error.assert_called()

    def test_multiple_results_mixed_status(self, tmp_path):
        """Multiple results with different status values."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = [
            _make_result("claude", str(tmp_path / ".claude/CLAUDE.md"), "written"),
            _make_result("vscode", str(tmp_path / ".vscode/AGENTS.md"), "unchanged"),
        ]

        mock_rich_success = MagicMock()
        mock_rich_info = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
            patch(
                "apm_cli.utils.console._rich_success",
                new=mock_rich_success,
            ),
            patch(
                "apm_cli.utils.console._rich_info",
                new=mock_rich_info,
            ),
        ):
            rc = _handle_global_flag(dry_run=False)

        assert rc == 0
        # Success and info should have been called
        assert mock_rich_success.called or mock_rich_info.called

    def test_no_results_returns_success(self, tmp_path):
        """Empty results list -> returns 0."""
        from apm_cli.commands.compile.cli import _handle_global_flag

        source_root = tmp_path / "source"
        source_root.mkdir()
        apm_modules = source_root / "apm_modules"
        apm_modules.mkdir()

        results = []

        mock_rich_info = MagicMock()

        with (
            patch(
                "apm_cli.core.scope.get_apm_dir",
                return_value=source_root,
            ),
            patch(
                "apm_cli.compilation.compile_user_root_contexts",
                return_value=results,
            ),
            patch(
                "apm_cli.utils.console._rich_info",
                new=mock_rich_info,
            ),
        ):
            rc = _handle_global_flag(dry_run=False)

        assert rc == 0


# ---------------------------------------------------------------------------
# compile command --global integration tests
# ---------------------------------------------------------------------------


class TestCompileGlobalCommand:
    """Tests for compile command with --global flag."""

    def test_global_with_watch_rejected(self):
        """--global and --watch together -> Click usage error."""
        from apm_cli.commands.compile.cli import compile as compile_cmd

        runner = CliRunner()

        result = runner.invoke(compile_cmd, ["--global", "--watch"])

        assert result.exit_code == 2
        assert "global" in result.output.lower()
        assert "watch" in result.output.lower()
        assert "Usage:" in result.output

    def test_global_with_root_rejected(self):
        """--global and --root together -> Click usage error."""
        from apm_cli.commands.compile.cli import compile as compile_cmd

        runner = CliRunner()

        result = runner.invoke(compile_cmd, ["--global", "--root", "/nonexistent"])

        assert result.exit_code == 2
        assert "global" in result.output.lower()
        assert "root" in result.output.lower()
        assert "Usage:" in result.output

    def test_global_with_target_rejected(self):
        """--global and --target together -> Click usage error."""
        from apm_cli.commands.compile.cli import compile as compile_cmd

        result = CliRunner().invoke(compile_cmd, ["--global", "--target", "claude"])

        assert result.exit_code == 2
        assert "global" in result.output.lower()
        assert "target" in result.output.lower()
        assert "Usage:" in result.output

    def test_global_with_output_rejected(self):
        """--global and --output together -> Click usage error."""
        from apm_cli.commands.compile.cli import compile as compile_cmd

        result = CliRunner().invoke(compile_cmd, ["--global", "--output", "AGENTS.md"])

        assert result.exit_code == 2
        assert "global" in result.output.lower()
        assert "output" in result.output.lower()
        assert "Usage:" in result.output

    def test_global_success_no_exit(self, tmp_path):
        """--global with successful _handle_global_flag -> returns normally."""
        from apm_cli.commands.compile.cli import compile as compile_cmd

        runner = CliRunner()

        source_root = tmp_path / "source"
        source_root.mkdir()

        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=source_root),
            patch(
                "apm_cli.commands.compile.cli._handle_global_flag",
                return_value=0,
            ),
        ):
            # Invoke with --global; should return 0 (success)
            result = runner.invoke(compile_cmd, ["--global"], standalone_mode=False)

            # Runner exit_code should be 0
            assert result.exit_code == 0

    def test_global_failure_exits_1(self, tmp_path):
        """--global with _handle_global_flag returning 1 -> sys.exit(1)."""
        from apm_cli.commands.compile.cli import compile as compile_cmd

        runner = CliRunner()

        source_root = tmp_path / "source"
        source_root.mkdir()

        with (
            patch("apm_cli.core.scope.get_apm_dir", return_value=source_root),
            patch(
                "apm_cli.commands.compile.cli._handle_global_flag",
                return_value=1,
            ),
        ):
            # Invoke with --global; _handle_global_flag returns 1 -> sys.exit(1)
            result = runner.invoke(compile_cmd, ["--global"])

            # Runner exit_code should be 1
            assert result.exit_code == 1
