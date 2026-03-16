"""Tests for the platform-aware update command."""

import re
import unittest
from unittest.mock import Mock, patch

from click.testing import CliRunner

import apm_cli.commands.update as update_module
from apm_cli.cli import cli


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes for plain-text assertions."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)

class TestUpdateCommand(unittest.TestCase):
    """Verify update command behavior across supported installer platforms."""

    def setUp(self):
        self.runner = CliRunner()

    def test_manual_update_command_uses_windows_installer(self):
        """Windows manual update instructions should point to install.ps1."""
        with patch.object(update_module.sys, "platform", "win32"):
            command = update_module._get_manual_update_command()

        self.assertIn("install.ps1", command)
        self.assertIn("powershell", command.lower())

    @patch("requests.get")
    @patch("subprocess.run")
    @patch("apm_cli.commands.update.get_version", return_value="0.6.3")
    @patch("apm_cli.commands.update.shutil.which", return_value="powershell.exe")
    @patch("apm_cli.commands.update.os.chmod")
    @patch("apm_cli.utils.version_checker.get_latest_version_from_github", return_value="0.7.0")
    def test_update_uses_powershell_installer_on_windows(
        self,
        mock_latest,
        mock_chmod,
        mock_which,
        mock_version,
        mock_run,
        mock_get,
    ):
        """Windows updates should execute the PowerShell installer path."""
        mock_response = Mock()
        mock_response.text = "Write-Host 'install'"
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response
        mock_run.return_value = Mock(returncode=0)

        with patch.object(update_module.sys, "platform", "win32"):
            result = self.runner.invoke(cli, ["update"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Successfully updated to version 0.7.0", _strip_ansi(result.output))
        mock_get.assert_called_once()
        self.assertTrue(mock_get.call_args.args[0].endswith("install.ps1"))
        mock_run.assert_called_once()
        run_command = mock_run.call_args.args[0]
        self.assertEqual(run_command[:3], ["powershell.exe", "-ExecutionPolicy", "Bypass"])
        self.assertEqual(run_command[3], "-File")
        mock_chmod.assert_not_called()

    @patch("requests.get")
    @patch("subprocess.run")
    @patch("apm_cli.commands.update.get_version", return_value="0.6.3")
    @patch("apm_cli.commands.update.os.chmod")
    @patch("apm_cli.utils.version_checker.get_latest_version_from_github", return_value="0.7.0")
    def test_update_uses_shell_installer_on_unix(
        self,
        mock_latest,
        mock_chmod,
        mock_version,
        mock_run,
        mock_get,
    ):
        """Unix updates should continue to execute the shell installer path."""
        mock_response = Mock()
        mock_response.text = "echo install"
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response
        mock_run.return_value = Mock(returncode=0)

        with patch.object(update_module.sys, "platform", "darwin"), \
             patch("apm_cli.commands.update.os.path.exists", return_value=True):
            result = self.runner.invoke(cli, ["update"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Successfully updated to version 0.7.0", _strip_ansi(result.output))
        mock_get.assert_called_once()
        self.assertTrue(mock_get.call_args.args[0].endswith("install.sh"))
        mock_run.assert_called_once()
        run_command = mock_run.call_args.args[0]
        self.assertEqual(run_command[0], "/bin/sh")
        self.assertEqual(run_command[1][-3:], ".sh")
        mock_chmod.assert_called_once()


if __name__ == "__main__":
    unittest.main()