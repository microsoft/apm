"""Tests for Windows platform support in RuntimeManager and ScriptRunner."""

import sys  # noqa: F401
from unittest.mock import MagicMock, patch

import pytest  # noqa: F401

from apm_cli.core.script_runner import ScriptRunner

# Import modules at module level BEFORE any sys.platform patching,
# to avoid triggering Windows-only import paths (msvcrt, CREATE_NO_WINDOW) on Unix.
from apm_cli.runtime.manager import RuntimeManager


def _make_manager(platform: str) -> RuntimeManager:
    """Create a RuntimeManager with a specific platform."""
    with patch("sys.platform", platform):
        return RuntimeManager()


class TestRuntimeManagerPlatformDetection:
    """Test RuntimeManager selects correct scripts per platform."""

    def test_selects_ps1_scripts_on_windows(self):
        manager = _make_manager("win32")
        for name, runtime_info in manager.supported_runtimes.items():
            assert runtime_info["script"].endswith(".ps1"), (
                f"Runtime '{name}' should use .ps1 on Windows, got {runtime_info['script']}"
            )

    def test_selects_sh_scripts_on_unix(self):
        manager = _make_manager("darwin")
        for name, runtime_info in manager.supported_runtimes.items():
            assert runtime_info["script"].endswith(".sh"), (
                f"Runtime '{name}' should use .sh on Unix, got {runtime_info['script']}"
            )

    def test_selects_sh_scripts_on_linux(self):
        manager = _make_manager("linux")
        for name, runtime_info in manager.supported_runtimes.items():
            assert runtime_info["script"].endswith(".sh"), (
                f"Runtime '{name}' should use .sh on Linux, got {runtime_info['script']}"
            )

    def test_common_script_is_ps1_on_windows(self):
        manager = _make_manager("win32")
        with (
            patch("sys.platform", "win32"),
            patch.object(manager, "get_embedded_script", return_value="# ps1 content") as mock,
        ):
            manager.get_common_script()
            mock.assert_called_once_with("setup-common.ps1")

    def test_common_script_is_sh_on_unix(self):
        manager = _make_manager("darwin")
        with (
            patch("sys.platform", "darwin"),
            patch.object(manager, "get_embedded_script", return_value="# sh content") as mock,
        ):
            manager.get_common_script()
            mock.assert_called_once_with("setup-common.sh")


class TestRuntimeManagerTokenHelper:
    """Test token helper script platform behavior."""

    def test_token_helper_returns_empty_on_windows(self):
        manager = _make_manager("win32")
        with patch("sys.platform", "win32"):
            result = manager.get_token_helper_script()
        assert result == "", "Token helper should return empty string on Windows"

    def test_token_helper_loads_script_on_unix(self):
        manager = _make_manager("darwin")
        with (
            patch("sys.platform", "darwin"),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value="#!/bin/bash\n# token helper"),
        ):
            result = manager.get_token_helper_script()
            assert result == "#!/bin/bash\n# token helper"


class TestRuntimeManagerExecution:
    """Test RuntimeManager uses correct shell per platform."""

    def test_uses_powershell_on_windows(self):
        """Verify PowerShell is used for script execution on Windows."""
        manager = _make_manager("win32")
        with (
            patch("sys.platform", "win32"),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
            patch(
                "shutil.which",
                return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            ),
            patch.object(manager, "get_token_helper_script", return_value=""),
        ):
            manager.run_embedded_script("# script", "# common")

        cmd = mock_run.call_args[0][0]
        assert "powershell" in cmd[0].lower() or "pwsh" in cmd[0].lower(), (
            f"Expected powershell/pwsh in command, got: {cmd[0]}"
        )

    def test_powershell_uses_bypass_execution_policy(self):
        """Verify -ExecutionPolicy Bypass is passed on Windows."""
        manager = _make_manager("win32")
        with (
            patch("sys.platform", "win32"),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
            patch(
                "shutil.which",
                return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            ),
            patch.object(manager, "get_token_helper_script", return_value=""),
        ):
            manager.run_embedded_script("# script", "# common")

        cmd = mock_run.call_args[0][0]
        assert "-ExecutionPolicy" in cmd
        assert "Bypass" in cmd

    def test_windows_writes_ps1_temp_files(self):
        """Verify temp files use .ps1 extension on Windows."""
        manager = _make_manager("win32")
        with (
            patch("sys.platform", "win32"),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
            patch(
                "shutil.which",
                return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            ),
            patch.object(manager, "get_token_helper_script", return_value=""),
        ):
            manager.run_embedded_script("# script content", "# common content")

        cmd = mock_run.call_args[0][0]
        file_arg_idx = cmd.index("-File") + 1
        assert cmd[file_arg_idx].endswith(".ps1"), (
            f"Expected .ps1 temp file, got: {cmd[file_arg_idx]}"
        )

    def test_uses_bash_on_unix(self):
        """Verify bash is used for script execution on Unix."""
        manager = _make_manager("linux")
        with (
            patch("sys.platform", "linux"),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value="#!/bin/bash\n# token helper"),
        ):
            manager.run_embedded_script("# script", "# common")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "bash", f"Expected bash, got: {cmd[0]}"

    def test_unix_writes_sh_temp_files(self):
        """Verify temp files use .sh extension on Unix."""
        manager = _make_manager("linux")
        with (
            patch("sys.platform", "linux"),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value="#!/bin/bash"),
        ):
            manager.run_embedded_script("# script content", "# common content")

        cmd = mock_run.call_args[0][0]
        assert cmd[1].endswith(".sh"), f"Expected .sh temp file, got: {cmd[1]}"

    def test_script_args_forwarded_on_windows(self):
        """Verify script arguments are forwarded to PowerShell."""
        manager = _make_manager("win32")
        with (
            patch("sys.platform", "win32"),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
            patch(
                "shutil.which",
                return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            ),
            patch.object(manager, "get_token_helper_script", return_value=""),
        ):
            manager.run_embedded_script("# script", "# common", ["-Vanilla"])

        cmd = mock_run.call_args[0][0]
        assert "-Vanilla" in cmd

    def test_setup_runtime_uses_ps_args_on_windows(self):
        """Verify setup_runtime translates args to PowerShell style on Windows."""
        manager = _make_manager("win32")
        with (
            patch("sys.platform", "win32"),
            patch.object(manager, "get_embedded_script", return_value="# ps1"),
            patch.object(manager, "get_common_script", return_value="# common"),
            patch.object(manager, "run_embedded_script", return_value=True) as mock_run,
        ):
            manager.setup_runtime("codex", version="0.1.0", vanilla=True)

        args = mock_run.call_args[0][2]  # script_args is the 3rd positional arg
        assert "-Version" in args
        assert "0.1.0" in args
        assert "-Vanilla" in args
        assert "--vanilla" not in args

    def test_setup_runtime_uses_unix_args_on_linux(self):
        """Verify setup_runtime keeps Unix-style args on Linux."""
        manager = _make_manager("linux")
        with (
            patch("sys.platform", "linux"),
            patch.object(manager, "get_embedded_script", return_value="# bash"),
            patch.object(manager, "get_common_script", return_value="# common"),
            patch.object(manager, "run_embedded_script", return_value=True) as mock_run,
        ):
            manager.setup_runtime("codex", version="0.1.0", vanilla=True)

        args = mock_run.call_args[0][2]
        assert "0.1.0" in args
        assert "--vanilla" in args
        assert "-Vanilla" not in args


class TestScriptRunnerWindowsParsing:
    """Test ScriptRunner handles Windows command parsing."""

    def test_execute_runtime_command_uses_shlex_on_windows(self):
        """On Windows, _execute_runtime_command should use shlex.split(posix=False)."""
        runner = ScriptRunner()
        env = {"PATH": "/usr/bin"}

        with (
            patch("sys.platform", "win32"),
            patch("apm_cli.core.script_runner.shutil.which", return_value=None),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
        ):
            runner._execute_runtime_command("codex --quiet", "prompt content", env)
            call_args = mock_run.call_args[0][0]
            assert "codex" in call_args
            assert "--quiet" in call_args

    def test_execute_runtime_command_preserves_quotes_on_windows(self):
        """On Windows, quoted arguments should be preserved by shlex.split(posix=False)."""
        runner = ScriptRunner()
        env = {"PATH": "/usr/bin"}

        with (
            patch("sys.platform", "win32"),
            patch("apm_cli.core.script_runner.shutil.which", return_value=None),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
        ):
            runner._execute_runtime_command('codex --model "gpt-4o mini"', "prompt content", env)
            call_args = mock_run.call_args[0][0]
            assert "codex" in call_args
            # shlex.split(posix=False) keeps the quotes around the value
            assert any("gpt-4o mini" in arg or '"gpt-4o mini"' in arg for arg in call_args)

    def test_execute_runtime_command_uses_shlex_on_unix(self):
        """On Unix, _execute_runtime_command should use shlex.split()."""
        runner = ScriptRunner()
        env = {"PATH": "/usr/bin"}

        with (
            patch("sys.platform", "linux"),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
        ):
            runner._execute_runtime_command("codex --quiet", "prompt content", env)
            call_args = mock_run.call_args[0][0]
            assert "codex" in call_args
            assert "--quiet" in call_args

    def test_script_runner_has_runtime_command_method(self):
        """Verify ScriptRunner has _execute_runtime_command method."""
        runner = ScriptRunner()
        assert hasattr(runner, "_execute_runtime_command")
        assert callable(runner._execute_runtime_command)


class TestIsWindowsProperty:
    """Test _is_windows property on RuntimeManager."""

    def test_is_windows_true(self):
        manager = _make_manager("win32")
        with patch("sys.platform", "win32"):
            assert manager._is_windows is True

    def test_is_windows_false_on_macos(self):
        manager = _make_manager("darwin")
        with patch("sys.platform", "darwin"):
            assert manager._is_windows is False

    def test_is_windows_false_on_linux(self):
        manager = _make_manager("linux")
        with patch("sys.platform", "linux"):
            assert manager._is_windows is False
