"""Unit tests for the apm run and preview commands.

Tests cover:
- run: no script name, no 'start' script defined -> shows available scripts and exits 1
- run: no script name, 'start' script defined -> uses default 'start'
- run: explicit script name, ScriptRunner succeeds
- run: explicit script name, ScriptRunner returns False -> exits 1
- run: ScriptRunner import error (graceful degradation)
- run: ScriptRunner raises exception -> exits 1
- run: parameter parsing (--param flag)
- run: outer exception handling
- preview: no script name, no 'start' defined -> exits 1
- preview: script found, with compiled .prompt.md files
- preview: script found, no compiled files
- preview: script not found -> exits 1
- preview: ScriptRunner import error (graceful degradation)
- preview: outer exception handling
"""

import contextlib
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import clear_apm_yml_cache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_APM_YML_WITH_START = """\
name: test-project
version: 1.0.0
scripts:
  start: gh models run gpt-4o
  build: gh models run gpt-4o-mini
"""

_APM_YML_NO_START = """\
name: test-project
version: 1.0.0
scripts:
  build: gh models run gpt-4o-mini
"""

_APM_YML_EMPTY_SCRIPTS = """\
name: test-project
version: 1.0.0
scripts: {}
"""


@contextlib.contextmanager
def _tmp_project(apm_yml_content: str):
    """Create a temporary directory with an apm.yml and cd into it."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "apm.yml").write_text(apm_yml_content)
        old = os.getcwd()
        os.chdir(tmp)
        clear_apm_yml_cache()
        try:
            yield Path(tmp)
        finally:
            os.chdir(old)
            clear_apm_yml_cache()


# ---------------------------------------------------------------------------
# run command tests
# ---------------------------------------------------------------------------


class TestRunCommand:
    def test_no_script_no_start_exits_1(self):
        """When no script name given and no 'start' in apm.yml, exits with code 1."""
        runner = CliRunner()
        with _tmp_project(_APM_YML_NO_START):
            result = runner.invoke(cli, ["run"])
        assert result.exit_code == 1

    def test_no_script_no_start_shows_available_scripts(self):
        """When no 'start' defined, available scripts are listed."""
        runner = CliRunner()
        with _tmp_project(_APM_YML_NO_START):
            result = runner.invoke(cli, ["run"])
        assert "build" in result.output

    def test_no_script_empty_scripts_exits_1(self):
        """When no scripts at all, exits 1 without crashing."""
        runner = CliRunner()
        with _tmp_project(_APM_YML_EMPTY_SCRIPTS):
            result = runner.invoke(cli, ["run"])
        assert result.exit_code == 1

    def test_uses_start_script_when_no_name_given(self):
        """When no script name given but 'start' exists, ScriptRunner is invoked."""
        runner = CliRunner()
        mock_runner = MagicMock()
        mock_runner.run_script.return_value = True
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner", return_value=mock_runner
            ):
                result = runner.invoke(cli, ["run"])
        assert result.exit_code == 0
        mock_runner.run_script.assert_called_once_with("start", {})

    def test_explicit_script_success(self):
        """Explicit script name with ScriptRunner returning True -> exits 0."""
        runner = CliRunner()
        mock_runner = MagicMock()
        mock_runner.run_script.return_value = True
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner", return_value=mock_runner
            ):
                result = runner.invoke(cli, ["run", "build"])
        assert result.exit_code == 0
        mock_runner.run_script.assert_called_once_with("build", {})

    def test_explicit_script_failure_exits_1(self):
        """ScriptRunner returning False causes exit code 1."""
        runner = CliRunner()
        mock_runner = MagicMock()
        mock_runner.run_script.return_value = False
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner", return_value=mock_runner
            ):
                result = runner.invoke(cli, ["run", "build"])
        assert result.exit_code == 1

    def test_param_flag_passed_to_runner(self):
        """--param flags are parsed and forwarded to ScriptRunner.run_script."""
        runner = CliRunner()
        mock_runner = MagicMock()
        mock_runner.run_script.return_value = True
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner", return_value=mock_runner
            ):
                result = runner.invoke(
                    cli, ["run", "build", "--param", "model=gpt-4o", "--param", "temp=0.7"]
                )
        assert result.exit_code == 0
        mock_runner.run_script.assert_called_once_with(
            "build", {"model": "gpt-4o", "temp": "0.7"}
        )

    def test_param_without_equals_ignored(self):
        """--param without '=' separator is silently ignored."""
        runner = CliRunner()
        mock_runner = MagicMock()
        mock_runner.run_script.return_value = True
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner", return_value=mock_runner
            ):
                result = runner.invoke(cli, ["run", "build", "--param", "nodash"])
        assert result.exit_code == 0
        mock_runner.run_script.assert_called_once_with("build", {})

    def test_import_error_graceful_degradation(self):
        """ImportError from ScriptRunner import is handled gracefully (no crash)."""
        runner = CliRunner()
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner",
                side_effect=ImportError("no module"),
            ):
                result = runner.invoke(cli, ["run", "build"])
        assert result.exit_code == 0

    def test_script_runner_exception_exits_1(self):
        """Exception from ScriptRunner.run_script causes exit code 1."""
        runner = CliRunner()
        mock_runner = MagicMock()
        mock_runner.run_script.side_effect = RuntimeError("boom")
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner", return_value=mock_runner
            ):
                result = runner.invoke(cli, ["run", "build"])
        assert result.exit_code == 1

    def test_verbose_flag_accepted(self):
        """--verbose flag is accepted without error."""
        runner = CliRunner()
        mock_runner = MagicMock()
        mock_runner.run_script.return_value = True
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner", return_value=mock_runner
            ):
                result = runner.invoke(cli, ["run", "--verbose", "build"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# preview command tests
# ---------------------------------------------------------------------------


class TestPreviewCommand:
    def test_no_script_no_start_exits_1(self):
        """preview with no script name and no 'start' defined exits 1."""
        runner = CliRunner()
        with _tmp_project(_APM_YML_NO_START):
            result = runner.invoke(cli, ["preview"])
        assert result.exit_code == 1

    def test_script_not_found_exits_1(self):
        """preview for a script name not in apm.yml exits 1."""
        runner = CliRunner()
        mock_runner = MagicMock()
        mock_runner.list_scripts.return_value = {"build": "gh models run gpt-4o-mini"}
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner", return_value=mock_runner
            ):
                result = runner.invoke(cli, ["preview", "nonexistent"])
        assert result.exit_code == 1

    def test_preview_with_compiled_files(self):
        """preview displays compiled command and file list when .prompt.md files found."""
        runner = CliRunner()
        mock_runner = MagicMock()
        mock_runner.list_scripts.return_value = {"start": "gh models run gpt-4o"}
        mock_runner._auto_compile_prompts.return_value = (
            "gh models run gpt-4o --system .apm/compiled/system.txt",
            ["system.prompt.md"],
        )
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner", return_value=mock_runner
            ):
                result = runner.invoke(cli, ["preview", "start"])
        assert result.exit_code == 0

    def test_preview_no_compiled_files(self):
        """preview shows warning when no .prompt.md files are compiled."""
        runner = CliRunner()
        mock_runner = MagicMock()
        mock_runner.list_scripts.return_value = {"start": "gh models run gpt-4o"}
        mock_runner._auto_compile_prompts.return_value = (
            "gh models run gpt-4o",
            [],
        )
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner", return_value=mock_runner
            ):
                result = runner.invoke(cli, ["preview", "start"])
        assert result.exit_code == 0

    def test_preview_uses_start_script_when_no_name_given(self):
        """preview without script name uses 'start' script."""
        runner = CliRunner()
        mock_runner = MagicMock()
        mock_runner.list_scripts.return_value = {"start": "gh models run gpt-4o"}
        mock_runner._auto_compile_prompts.return_value = ("gh models run gpt-4o", [])
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner", return_value=mock_runner
            ):
                result = runner.invoke(cli, ["preview"])
        assert result.exit_code == 0
        mock_runner.list_scripts.assert_called_once()

    def test_preview_import_error_graceful_degradation(self):
        """ImportError from ScriptRunner in preview is handled gracefully."""
        runner = CliRunner()
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner",
                side_effect=ImportError("no module"),
            ):
                result = runner.invoke(cli, ["preview", "start"])
        assert result.exit_code == 0

    def test_preview_outer_exception_exits_1(self):
        """Unexpected exception in preview causes exit code 1."""
        runner = CliRunner()
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner",
                side_effect=Exception("unexpected"),
            ):
                result = runner.invoke(cli, ["preview", "start"])
        assert result.exit_code == 1

    def test_preview_param_flag_passed(self):
        """--param flags are parsed and forwarded during preview."""
        runner = CliRunner()
        mock_runner = MagicMock()
        mock_runner.list_scripts.return_value = {"start": "gh models run gpt-4o"}
        mock_runner._auto_compile_prompts.return_value = ("gh models run gpt-4o", [])
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner", return_value=mock_runner
            ):
                result = runner.invoke(
                    cli, ["preview", "start", "--param", "model=gpt-4o"]
                )
        assert result.exit_code == 0
        mock_runner._auto_compile_prompts.assert_called_once()
        call_args = mock_runner._auto_compile_prompts.call_args
        assert call_args[0][1] == {"model": "gpt-4o"}

    def test_preview_verbose_accepted(self):
        """--verbose flag is accepted in preview without error."""
        runner = CliRunner()
        mock_runner = MagicMock()
        mock_runner.list_scripts.return_value = {"start": "gh models run gpt-4o"}
        mock_runner._auto_compile_prompts.return_value = ("gh models run gpt-4o", [])
        with _tmp_project(_APM_YML_WITH_START):
            with patch(
                "apm_cli.core.script_runner.ScriptRunner", return_value=mock_runner
            ):
                result = runner.invoke(cli, ["preview", "--verbose", "start"])
        assert result.exit_code == 0
