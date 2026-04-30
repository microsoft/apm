"""Unit tests for the apm list command.

Tests cover:
- No scripts defined in apm.yml (shows panel / fallback)
- Scripts found with Rich table (console available)
- Scripts found with fallback text (no console)
- 'start' script highlighted as default
- Exception during script listing (sys.exit(1))
"""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from apm_cli.commands.list_cmd import list as list_command

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCRIPTS_MULTI = {
    "start": "apm run main.prompt.md",
    "fast": "llm prompt main.prompt.md -m github/gpt-4o-mini",
}

_SCRIPTS_NO_DEFAULT = {
    "build": "echo building",
    "test": "pytest",
}


# ---------------------------------------------------------------------------
# No-scripts cases
# ---------------------------------------------------------------------------


class TestListNoScripts:
    def test_no_scripts_shows_warning(self):
        """When no scripts are defined, a warning is printed."""
        runner = CliRunner()
        with patch("apm_cli.commands.list_cmd._list_available_scripts", return_value={}):
            result = runner.invoke(list_command, obj={})
        assert result.exit_code == 0
        assert "No scripts found" in result.output

    def test_no_scripts_rich_panel_shown(self):
        """When no scripts are defined, a rich panel with an example is attempted."""
        runner = CliRunner()
        panel_called = []

        def _fake_panel(content, title="", style=""):
            panel_called.append(content)

        with (
            patch("apm_cli.commands.list_cmd._list_available_scripts", return_value={}),
            patch("apm_cli.commands.list_cmd._rich_panel", side_effect=_fake_panel),
        ):
            result = runner.invoke(list_command, obj={})
        assert result.exit_code == 0
        assert len(panel_called) == 1
        assert "scripts:" in panel_called[0]

    def test_no_scripts_fallback_when_panel_raises_import_error(self):
        """When _rich_panel raises ImportError, fallback echo is used."""
        runner = CliRunner()
        with (
            patch("apm_cli.commands.list_cmd._list_available_scripts", return_value={}),
            patch(
                "apm_cli.commands.list_cmd._rich_panel",
                side_effect=ImportError("no rich"),
            ),
        ):
            result = runner.invoke(list_command, obj={})
        assert result.exit_code == 0
        assert "scripts:" in result.output
        assert "start" in result.output

    def test_no_scripts_fallback_when_panel_raises_name_error(self):
        """NameError from _rich_panel also triggers fallback."""
        runner = CliRunner()
        with (
            patch("apm_cli.commands.list_cmd._list_available_scripts", return_value={}),
            patch(
                "apm_cli.commands.list_cmd._rich_panel",
                side_effect=NameError("name error"),
            ),
        ):
            result = runner.invoke(list_command, obj={})
        assert result.exit_code == 0
        assert "scripts:" in result.output


# ---------------------------------------------------------------------------
# Scripts found - rich table path
# ---------------------------------------------------------------------------


class TestListWithScripts:
    def test_fallback_renders_scripts_once(self):
        """The non-Rich fallback should render the scripts list only once."""
        runner = CliRunner()
        with (
            patch(
                "apm_cli.commands.list_cmd._list_available_scripts",
                return_value={"start": "python main.py", "lint": "ruff check ."},
            ),
            patch("apm_cli.commands.list_cmd._get_console", return_value=None),
        ):
            result = runner.invoke(list_command, obj={})
        assert result.exit_code == 0
        assert result.output.count("Available scripts:") == 1

    def test_scripts_rich_table_rendered(self):
        """Scripts listed via Rich table when console is available."""
        runner = CliRunner()
        mock_console = MagicMock()
        with (
            patch(
                "apm_cli.commands.list_cmd._list_available_scripts",
                return_value=_SCRIPTS_MULTI,
            ),
            patch("apm_cli.commands.list_cmd._get_console", return_value=mock_console),
        ):
            result = runner.invoke(list_command, obj={})
        assert result.exit_code == 0
        mock_console.print.assert_called()

    def test_start_is_default_in_fallback(self):
        """'start' script should be marked as default in fallback output."""
        runner = CliRunner()
        with (
            patch(
                "apm_cli.commands.list_cmd._list_available_scripts",
                return_value={"start": "apm run main.prompt.md"},
            ),
            patch("apm_cli.commands.list_cmd._get_console", return_value=None),
        ):
            result = runner.invoke(list_command, obj={})
        assert result.exit_code == 0
        assert "start" in result.output
        assert "default script" in result.output

    def test_no_default_annotation_without_start(self):
        """When no 'start' script, no 'default script' annotation shown."""
        runner = CliRunner()
        with (
            patch(
                "apm_cli.commands.list_cmd._list_available_scripts",
                return_value=_SCRIPTS_NO_DEFAULT,
            ),
            patch("apm_cli.commands.list_cmd._get_console", return_value=None),
        ):
            result = runner.invoke(list_command, obj={})
        assert result.exit_code == 0
        assert "build" in result.output
        assert "default script" not in result.output

    def test_all_scripts_shown_in_fallback(self):
        """All scripts appear when rendered via fallback path."""
        runner = CliRunner()
        with (
            patch(
                "apm_cli.commands.list_cmd._list_available_scripts",
                return_value=_SCRIPTS_NO_DEFAULT,
            ),
            patch("apm_cli.commands.list_cmd._get_console", return_value=None),
        ):
            result = runner.invoke(list_command, obj={})
        assert result.exit_code == 0
        for name in _SCRIPTS_NO_DEFAULT:
            assert name in result.output

    def test_fallback_when_rich_table_raises(self):
        """Exception in Rich table rendering falls back to plain text."""
        runner = CliRunner()
        mock_console = MagicMock()
        mock_console.print.side_effect = Exception("rich crash")
        with (
            patch(
                "apm_cli.commands.list_cmd._list_available_scripts",
                return_value=_SCRIPTS_MULTI,
            ),
            patch("apm_cli.commands.list_cmd._get_console", return_value=mock_console),
        ):
            result = runner.invoke(list_command, obj={})
        assert result.exit_code == 0
        assert "fast" in result.output

    def test_start_default_shown_in_rich_table(self):
        """Console.print called twice when 'start' is present (table + note)."""
        runner = CliRunner()
        mock_console = MagicMock()
        with (
            patch(
                "apm_cli.commands.list_cmd._list_available_scripts",
                return_value=_SCRIPTS_MULTI,
            ),
            patch("apm_cli.commands.list_cmd._get_console", return_value=mock_console),
        ):
            result = runner.invoke(list_command, obj={})
        assert result.exit_code == 0
        # Table + default note = 2 calls
        assert mock_console.print.call_count >= 2


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestListErrorHandling:
    def test_exception_exits_with_code_1(self):
        """Unexpected exception causes sys.exit(1)."""
        runner = CliRunner()
        with patch(
            "apm_cli.commands.list_cmd._list_available_scripts",
            side_effect=RuntimeError("something broke"),
        ):
            result = runner.invoke(list_command, obj={})
        assert result.exit_code == 1

    def test_exception_shows_error_message(self):
        """Error message is shown when an exception occurs."""
        runner = CliRunner()
        with patch(
            "apm_cli.commands.list_cmd._list_available_scripts",
            side_effect=RuntimeError("disk error"),
        ):
            result = runner.invoke(list_command, obj={})
        assert result.exit_code == 1
        assert "Error listing scripts" in result.output
        assert "disk error" in result.output
