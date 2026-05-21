"""Comprehensive unit tests for ``apm_cli.commands.run`` (phase 3).

Covers:
- ``run`` command: no-script-name paths (start / no-start), param parsing,
  ScriptRunner success/failure/ImportError, rich-console and plain-echo branches.
- ``preview`` command: no-script-name, script-not-found, compiled/uncompiled
  prompt files, rich-panel / fallback rendering, ImportError, general exception.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from apm_cli.commands.run import preview, run

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_script_runner(
    *,
    run_success: bool = True,
    scripts: dict[str, str] | None = None,
    auto_compile_result: tuple[str, list[str]] | None = None,
) -> MagicMock:
    """Build a MagicMock that looks like a ScriptRunner instance."""
    runner = MagicMock()
    runner.run_script.return_value = run_success
    runner.list_scripts.return_value = scripts or {"start": "echo hello"}
    if auto_compile_result is not None:
        compiled_cmd, prompt_files = auto_compile_result
        runner._auto_compile_prompts.return_value = (compiled_cmd, prompt_files)
    return runner


# ===========================================================================
# ``run`` command tests
# ===========================================================================


class TestRunNoScriptName:
    """Tests for ``apm run`` when no script name is supplied."""

    def test_no_script_no_start_exits_1(self) -> None:
        """When no script is given and no 'start' script exists, exit code 1."""
        with (
            patch("apm_cli.commands.run._get_default_script", return_value=None),
            patch(
                "apm_cli.commands.run._list_available_scripts",
                return_value={"build": "make all"},
            ),
            patch("apm_cli.commands.run._get_console", return_value=None),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(run, [])
        assert result.exit_code == 1

    def test_no_script_no_start_prints_available_scripts_plain(self) -> None:
        """Available scripts are printed via click.echo when no rich console."""
        with (
            patch("apm_cli.commands.run._get_default_script", return_value=None),
            patch(
                "apm_cli.commands.run._list_available_scripts",
                return_value={"build": "make all", "test": "pytest"},
            ),
            patch("apm_cli.commands.run._get_console", return_value=None),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(run, [])
        assert "build" in result.output
        assert "test" in result.output

    def test_no_script_no_start_prints_table_with_rich_console(self) -> None:
        """When a rich Console is available, a Table is rendered."""
        mock_console = MagicMock()
        with (
            patch("apm_cli.commands.run._get_default_script", return_value=None),
            patch(
                "apm_cli.commands.run._list_available_scripts",
                return_value={"deploy": "apm pack"},
            ),
            patch("apm_cli.commands.run._get_console", return_value=mock_console),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(run, [])
        assert result.exit_code == 1
        # console.print should have been called with a Table
        mock_console.print.assert_called_once()

    def test_no_script_with_start_script_uses_it(self) -> None:
        """When apm.yml defines a 'start' script, run uses it automatically."""
        mock_runner = _make_script_runner(run_success=True)
        mock_cls = MagicMock(return_value=mock_runner)
        with (
            patch("apm_cli.commands.run._get_default_script", return_value="start"),
            patch("apm_cli.core.script_runner.ScriptRunner", mock_cls),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(run, [])
        assert result.exit_code == 0
        mock_runner.run_script.assert_called_once_with("start", {})

    def test_rich_table_import_error_falls_back_to_click_echo(self) -> None:
        """If importing rich.table raises ImportError, fall back to click.echo."""
        mock_console = MagicMock()

        import builtins

        real_import = builtins.__import__

        def _broken_import(name: str, *args: Any, **kwargs: Any):
            if name == "rich.table":
                raise ImportError("no rich.table")
            return real_import(name, *args, **kwargs)

        with (
            patch("apm_cli.commands.run._get_default_script", return_value=None),
            patch(
                "apm_cli.commands.run._list_available_scripts",
                return_value={"build": "make"},
            ),
            patch("apm_cli.commands.run._get_console", return_value=mock_console),
            patch("apm_cli.commands.run._rich_blank_line"),
            patch("builtins.__import__", side_effect=_broken_import),
        ):
            result = CliRunner().invoke(run, [])
        assert result.exit_code == 1
        # Falls back: click.echo produces output
        assert "build" in result.output


class TestRunWithScriptName:
    """Tests for ``apm run <script_name>`` with a name supplied."""

    def test_run_success(self) -> None:
        """Successful ScriptRunner.run_script → exit 0."""
        mock_runner = _make_script_runner(run_success=True)
        with (
            patch("apm_cli.core.script_runner.ScriptRunner", MagicMock(return_value=mock_runner)),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(run, ["myscript"])
        assert result.exit_code == 0
        mock_runner.run_script.assert_called_once_with("myscript", {})

    def test_run_failure_exits_1(self) -> None:
        """ScriptRunner returns False → exit 1."""
        mock_runner = _make_script_runner(run_success=False)
        with (
            patch("apm_cli.core.script_runner.ScriptRunner", MagicMock(return_value=mock_runner)),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(run, ["myscript"])
        assert result.exit_code == 1

    def test_run_params_parsed_correctly(self) -> None:
        """--param name=value pairs are parsed into a dict."""
        mock_runner = _make_script_runner(run_success=True)
        with (
            patch("apm_cli.core.script_runner.ScriptRunner", MagicMock(return_value=mock_runner)),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(
                run, ["myscript", "--param", "key1=val1", "--param", "key2=val2"]
            )
        assert result.exit_code == 0
        mock_runner.run_script.assert_called_once_with("myscript", {"key1": "val1", "key2": "val2"})

    def test_param_without_equals_is_ignored(self) -> None:
        """A --param without '=' does not crash; ignored silently."""
        mock_runner = _make_script_runner(run_success=True)
        with (
            patch("apm_cli.core.script_runner.ScriptRunner", MagicMock(return_value=mock_runner)),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(run, ["myscript", "--param", "noequals"])
        assert result.exit_code == 0
        mock_runner.run_script.assert_called_once_with("myscript", {})

    def test_run_import_error_is_handled(self) -> None:
        """ImportError from ScriptRunner is caught; command exits 0 (graceful)."""
        import sys as _sys

        with patch.dict(_sys.modules, {"apm_cli.core.script_runner": None}):
            result = CliRunner().invoke(run, ["myscript"])
        # ImportError path: logs a warning but does NOT call sys.exit
        assert result.exit_code == 0

    def test_run_general_exception_exits_1(self) -> None:
        """An unexpected exception from ScriptRunner exits 1."""
        mock_runner = MagicMock()
        mock_runner.run_script.side_effect = RuntimeError("boom")
        with (
            patch("apm_cli.core.script_runner.ScriptRunner", MagicMock(return_value=mock_runner)),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(run, ["myscript"])
        assert result.exit_code == 1

    def test_run_verbose_flag_accepted(self) -> None:
        """--verbose flag is accepted without error."""
        mock_runner = _make_script_runner(run_success=True)
        with (
            patch("apm_cli.core.script_runner.ScriptRunner", MagicMock(return_value=mock_runner)),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(run, ["myscript", "--verbose"])
        assert result.exit_code == 0

    def test_run_outer_exception_exits_1(self) -> None:
        """Exception raised outside the ScriptRunner block exits 1."""
        with patch(
            "apm_cli.commands.run._get_default_script",
            side_effect=RuntimeError("outer error"),
        ):
            result = CliRunner().invoke(run, [])
        assert result.exit_code == 1


# ===========================================================================
# ``preview`` command tests
# ===========================================================================


class TestPreviewNoScriptName:
    """Tests for ``apm preview`` when no script name is supplied."""

    def test_no_script_no_start_exits_1(self) -> None:
        """No script + no start → exit 1."""
        with patch("apm_cli.commands.run._get_default_script", return_value=None):
            result = CliRunner().invoke(preview, [])
        assert result.exit_code == 1

    def test_no_script_with_start_uses_it(self) -> None:
        """'start' script is used automatically when no name given."""
        mock_runner = _make_script_runner(
            scripts={"start": "echo hi"},
            auto_compile_result=("echo hi", []),
        )
        with (
            patch("apm_cli.commands.run._get_default_script", return_value="start"),
            patch("apm_cli.core.script_runner.ScriptRunner", MagicMock(return_value=mock_runner)),
            patch("apm_cli.commands.run._rich_panel"),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(preview, [])
        assert result.exit_code == 0


class TestPreviewScriptFound:
    """Tests for ``apm preview <script>`` when the script exists."""

    def test_preview_no_prompt_files(self) -> None:
        """When no .prompt.md files compiled, shows 'no compilation' panel."""
        mock_runner = _make_script_runner(
            scripts={"build": "make all"},
            auto_compile_result=("make all", []),
        )
        with (
            patch("apm_cli.core.script_runner.ScriptRunner", MagicMock(return_value=mock_runner)),
            patch("apm_cli.commands.run._rich_panel") as mock_panel,
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(preview, ["build"])
        assert result.exit_code == 0
        # At least one panel was rendered
        assert mock_panel.call_count >= 1

    def test_preview_with_prompt_files(self) -> None:
        """When .prompt.md files are compiled, shows compiled command panel."""
        mock_runner = _make_script_runner(
            scripts={"run": "apm run my.prompt.md"},
            auto_compile_result=("apm run compiled.txt", ["my.prompt.md"]),
        )
        with (
            patch("apm_cli.core.script_runner.ScriptRunner", MagicMock(return_value=mock_runner)),
            patch("apm_cli.commands.run._rich_panel") as mock_panel,
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(preview, ["run"])
        assert result.exit_code == 0
        assert mock_panel.call_count >= 2

    def test_preview_script_not_found_exits_1(self) -> None:
        """Script name not in list_scripts → exit 1."""
        mock_runner = _make_script_runner(scripts={"other": "noop"})
        with (
            patch("apm_cli.core.script_runner.ScriptRunner", MagicMock(return_value=mock_runner)),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(preview, ["missing"])
        assert result.exit_code == 1

    def test_preview_params_are_forwarded(self) -> None:
        """--param values are forwarded to _auto_compile_prompts."""
        mock_runner = _make_script_runner(
            scripts={"s": "cmd"},
            auto_compile_result=("cmd", []),
        )
        with (
            patch("apm_cli.core.script_runner.ScriptRunner", MagicMock(return_value=mock_runner)),
            patch("apm_cli.commands.run._rich_panel"),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            CliRunner().invoke(preview, ["s", "--param", "env=prod"])
        mock_runner._auto_compile_prompts.assert_called_once_with("cmd", {"env": "prod"})

    def test_preview_compiled_file_path_stem(self) -> None:
        """Output filename is stem with .prompt removed + .txt extension."""
        mock_runner = _make_script_runner(
            scripts={"s": "cmd agent.prompt.md"},
            auto_compile_result=("cmd compiled.txt", ["agent.prompt.md"]),
        )
        with (
            patch("apm_cli.core.script_runner.ScriptRunner", MagicMock(return_value=mock_runner)),
            patch("apm_cli.commands.run._rich_panel") as mock_panel,
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(preview, ["s"])
        assert result.exit_code == 0
        # Check that the panel with compiled file paths was called
        all_panel_calls = [str(c) for c in mock_panel.call_args_list]
        assert any("agent.txt" in text for text in all_panel_calls)


class TestPreviewFallbackRendering:
    """Tests for preview fallback when _rich_panel raises ImportError/NameError."""

    def test_fallback_with_no_prompt_files(self) -> None:
        """Fallback path (no rich) prints command via click.echo."""
        mock_runner = _make_script_runner(
            scripts={"s": "make all"},
            auto_compile_result=("make all", []),
        )
        with (
            patch("apm_cli.core.script_runner.ScriptRunner", MagicMock(return_value=mock_runner)),
            patch(
                "apm_cli.commands.run._rich_panel",
                side_effect=ImportError("no rich"),
            ),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(preview, ["s"])
        assert result.exit_code == 0
        assert "make all" in result.output

    def test_fallback_with_prompt_files(self) -> None:
        """Fallback path (no rich) prints compiled command and file list."""
        mock_runner = _make_script_runner(
            scripts={"s": "run a.prompt.md"},
            auto_compile_result=("run compiled.txt", ["a.prompt.md"]),
        )
        with (
            patch("apm_cli.core.script_runner.ScriptRunner", MagicMock(return_value=mock_runner)),
            patch(
                "apm_cli.commands.run._rich_panel",
                side_effect=ImportError("no rich"),
            ),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(preview, ["s"])
        assert result.exit_code == 0
        assert "compiled.txt" in result.output

    def test_preview_import_error_from_script_runner(self) -> None:
        """ImportError from ScriptRunner is caught; preview exits 0 gracefully."""
        import sys as _sys

        with patch.dict(_sys.modules, {"apm_cli.core.script_runner": None}):
            result = CliRunner().invoke(preview, ["myscript"])
        assert result.exit_code == 0

    def test_preview_outer_exception_exits_1(self) -> None:
        """Uncaught outer exception in preview exits 1."""
        with patch(
            "apm_cli.commands.run._get_default_script",
            side_effect=RuntimeError("outer"),
        ):
            result = CliRunner().invoke(preview, [])
        assert result.exit_code == 1

    def test_preview_verbose_flag_accepted(self) -> None:
        """--verbose flag is accepted by preview command."""
        mock_runner = _make_script_runner(
            scripts={"s": "cmd"},
            auto_compile_result=("cmd", []),
        )
        with (
            patch("apm_cli.core.script_runner.ScriptRunner", MagicMock(return_value=mock_runner)),
            patch("apm_cli.commands.run._rich_panel"),
            patch("apm_cli.commands.run._rich_blank_line"),
        ):
            result = CliRunner().invoke(preview, ["s", "--verbose"])
        assert result.exit_code == 0
