"""Tests for the platform-aware update command."""

import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from click.testing import CliRunner

import apm_cli.commands.update as update_module
from apm_cli.cli import cli


class TestUpdateCommand(unittest.TestCase):
    """Verify update command behavior across supported installer platforms."""

    def setUp(self):
        self.runner = CliRunner()
        # Pin APM_TEMP_DIR to an isolated temp directory so the installer
        # script that `apm update` writes via `get_apm_temp_dir()` lands in
        # a hermetic, writable location regardless of developer-machine
        # environment / ~/.apm/config.json contents.
        self._tempdir = tempfile.TemporaryDirectory()
        self._prev_apm_temp_dir = os.environ.get("APM_TEMP_DIR")
        os.environ["APM_TEMP_DIR"] = self._tempdir.name

    def tearDown(self):
        if self._prev_apm_temp_dir is None:
            os.environ.pop("APM_TEMP_DIR", None)
        else:
            os.environ["APM_TEMP_DIR"] = self._prev_apm_temp_dir
        self._tempdir.cleanup()

    def test_manual_update_command_uses_windows_installer(self):
        """Windows manual update instructions should point to aka.ms/apm-windows."""
        with patch.object(update_module.sys, "platform", "win32"):
            command = update_module._get_manual_update_command()

        self.assertIn("aka.ms/apm-windows", command)
        self.assertIn("powershell", command.lower())

    @patch("apm_cli.commands.update.is_self_update_enabled", return_value=False)
    @patch(
        "apm_cli.commands.update.get_self_update_disabled_message",
        return_value="Update with: pixi update apm-cli",
    )
    @patch("subprocess.run")
    @patch("requests.get")
    def test_update_command_respects_disabled_policy(
        self,
        mock_get,
        mock_run,
        mock_message,
        mock_enabled,
    ):
        """Disabled self-update policy should print guidance and skip installer."""
        result = self.runner.invoke(cli, ["update"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Update with: pixi update apm-cli", result.output)
        mock_get.assert_not_called()
        mock_run.assert_not_called()

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
        self.assertIn("Successfully updated to version 0.7.0", result.output)
        mock_get.assert_called_once()
        self.assertTrue(mock_get.call_args.args[0].endswith("apm-windows"))
        mock_run.assert_called_once()
        run_command = mock_run.call_args.args[0]
        self.assertEqual(run_command[:3], ["powershell.exe", "-ExecutionPolicy", "Bypass"])
        self.assertEqual(run_command[3], "-File")
        mock_chmod.assert_not_called()
        # The installer is always spawned with an explicit sanitised env;
        # see issue #894.  On Windows the helper is effectively a no-op, but
        # passing env= unconditionally keeps one code path across platforms.
        self.assertIn("env", mock_run.call_args.kwargs)
        self.assertIsNotNone(mock_run.call_args.kwargs["env"])

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

        with (
            patch.object(update_module.sys, "platform", "darwin"),
            patch("apm_cli.commands.update.os.path.exists", return_value=True),
        ):
            result = self.runner.invoke(cli, ["update"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Successfully updated to version 0.7.0", result.output)
        mock_get.assert_called_once()
        self.assertTrue(mock_get.call_args.args[0].endswith("apm-unix"))
        mock_run.assert_called_once()
        run_command = mock_run.call_args.args[0]
        self.assertEqual(run_command[0], "/bin/sh")
        self.assertEqual(run_command[1][-3:], ".sh")
        mock_chmod.assert_called_once()
        # Regression guard for issue #894: the installer must be spawned with
        # a sanitised env so system curl / tar do not inherit PyInstaller's
        # LD_LIBRARY_PATH pointing at the bundle's _internal directory.
        self.assertIn("env", mock_run.call_args.kwargs)
        self.assertIsNotNone(mock_run.call_args.kwargs["env"])


class TestUpdatePlatformHelpers(unittest.TestCase):
    """Tests for platform-detection helper functions."""

    def test_is_windows_platform_true_on_win32(self):
        with patch.object(update_module.sys, "platform", "win32"):
            self.assertTrue(update_module._is_windows_platform())

    def test_is_windows_platform_false_on_linux(self):
        with patch.object(update_module.sys, "platform", "linux"):
            self.assertFalse(update_module._is_windows_platform())

    def test_is_windows_platform_false_on_darwin(self):
        with patch.object(update_module.sys, "platform", "darwin"):
            self.assertFalse(update_module._is_windows_platform())

    def test_installer_url_windows(self):
        with patch.object(update_module.sys, "platform", "win32"):
            url = update_module._get_update_installer_url()
        self.assertEqual(url, "https://aka.ms/apm-windows")

    def test_installer_url_unix(self):
        with patch.object(update_module.sys, "platform", "linux"):
            url = update_module._get_update_installer_url()
        self.assertEqual(url, "https://aka.ms/apm-unix")

    def test_installer_suffix_windows(self):
        with patch.object(update_module.sys, "platform", "win32"):
            suffix = update_module._get_update_installer_suffix()
        self.assertEqual(suffix, ".ps1")

    def test_installer_suffix_unix(self):
        with patch.object(update_module.sys, "platform", "linux"):
            suffix = update_module._get_update_installer_suffix()
        self.assertEqual(suffix, ".sh")

    def test_manual_update_command_unix(self):
        with patch.object(update_module.sys, "platform", "linux"):
            command = update_module._get_manual_update_command()
        self.assertIn("aka.ms/apm-unix", command)
        self.assertIn("curl", command)

    def test_installer_run_command_unix_bin_sh_exists(self):
        with (
            patch.object(update_module.sys, "platform", "linux"),
            patch.object(update_module.os.path, "exists", return_value=True),
        ):
            cmd = update_module._get_installer_run_command("/tmp/install.sh")
        self.assertEqual(cmd, ["/bin/sh", "/tmp/install.sh"])

    def test_installer_run_command_unix_fallback_to_sh(self):
        with (
            patch.object(update_module.sys, "platform", "linux"),
            patch.object(update_module.os.path, "exists", return_value=False),
        ):
            cmd = update_module._get_installer_run_command("/tmp/install.sh")
        self.assertEqual(cmd, ["sh", "/tmp/install.sh"])

    def test_installer_run_command_windows_powershell_not_found(self):
        with (
            patch.object(update_module.sys, "platform", "win32"),
            patch.object(update_module.shutil, "which", return_value=None),
        ):
            with self.assertRaises(FileNotFoundError):
                update_module._get_installer_run_command("/tmp/install.ps1")

    def test_installer_run_command_windows_pwsh_fallback(self):
        def _which(name):
            return "pwsh.exe" if name == "pwsh" else None

        with (
            patch.object(update_module.sys, "platform", "win32"),
            patch.object(update_module.shutil, "which", side_effect=_which),
        ):
            cmd = update_module._get_installer_run_command("/tmp/install.ps1")
        self.assertEqual(cmd[0], "pwsh.exe")
        self.assertIn("-File", cmd)


class TestUpdateCommandLogic(unittest.TestCase):
    """Tests for the update click command business logic."""

    def setUp(self):
        self.runner = CliRunner()
        # Same hermetic isolation as TestUpdateCommand: pin APM_TEMP_DIR to
        # a per-test temp directory so the installer script written by
        # `apm update` cannot escape into a developer-configured path.
        self._tempdir = tempfile.TemporaryDirectory()
        self._prev_apm_temp_dir = os.environ.get("APM_TEMP_DIR")
        os.environ["APM_TEMP_DIR"] = self._tempdir.name

    def tearDown(self):
        if self._prev_apm_temp_dir is None:
            os.environ.pop("APM_TEMP_DIR", None)
        else:
            os.environ["APM_TEMP_DIR"] = self._prev_apm_temp_dir
        self._tempdir.cleanup()

    @patch("apm_cli.commands.update.get_version", return_value="unknown")
    def test_update_dev_version_warns_and_returns(self, mock_version):
        result = self.runner.invoke(cli, ["update"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("development mode", result.output)

    @patch("apm_cli.commands.update.get_version", return_value="unknown")
    def test_update_dev_version_check_flag_no_reinstall_hint(self, mock_version):
        """When --check is passed with dev version, reinstall hint should be suppressed."""
        result = self.runner.invoke(cli, ["update", "--check"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("development mode", result.output)
        self.assertNotIn("reinstall", result.output)

    @patch("apm_cli.utils.version_checker.get_latest_version_from_github", return_value=None)
    @patch("apm_cli.commands.update.get_version", return_value="1.0.0")
    def test_update_cannot_fetch_latest_exits_1(self, mock_version, mock_latest):
        result = self.runner.invoke(cli, ["update"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Unable to fetch latest version", result.output)

    @patch("apm_cli.utils.version_checker.get_latest_version_from_github", return_value="1.0.0")
    @patch("apm_cli.commands.update.get_version", return_value="1.0.0")
    def test_update_already_on_latest(self, mock_version, mock_latest):
        result = self.runner.invoke(cli, ["update"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("latest version", result.output)

    @patch("apm_cli.utils.version_checker.get_latest_version_from_github", return_value="1.1.0")
    @patch("apm_cli.commands.update.get_version", return_value="1.0.0")
    def test_update_check_flag_shows_available_no_install(self, mock_version, mock_latest):
        result = self.runner.invoke(cli, ["update", "--check"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("1.0.0", result.output)
        self.assertIn("1.1.0", result.output)

    @patch("requests.get")
    @patch("subprocess.run")
    @patch("apm_cli.commands.update.get_version", return_value="1.0.0")
    @patch("apm_cli.commands.update.os.chmod")
    @patch("apm_cli.utils.version_checker.get_latest_version_from_github", return_value="1.1.0")
    def test_update_installer_failure_exits_1(
        self, mock_latest, mock_chmod, mock_version, mock_run, mock_get
    ):
        mock_response = Mock()
        mock_response.text = "echo install"
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response
        mock_run.return_value = Mock(returncode=1)

        with (
            patch.object(update_module.sys, "platform", "linux"),
            patch("apm_cli.commands.update.os.path.exists", return_value=True),
        ):
            result = self.runner.invoke(cli, ["update"])

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Installation failed", result.output)

    @patch("apm_cli.commands.update.get_version", return_value="1.0.0")
    @patch("apm_cli.utils.version_checker.get_latest_version_from_github", return_value="1.1.0")
    def test_update_requests_not_available_exits_1(self, mock_latest, mock_version):
        """When requests library is missing, exit with clear message."""
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "requests":
                raise ImportError("No module named 'requests'")
            return real_import(name, *args, **kwargs)

        with (
            patch("builtins.__import__", side_effect=mock_import),
            patch.object(update_module.sys, "platform", "linux"),
        ):
            result = self.runner.invoke(cli, ["update"])

        self.assertEqual(result.exit_code, 1)
        self.assertIn("requests", result.output)

    @patch("requests.get")
    @patch("apm_cli.commands.update.get_version", return_value="1.0.0")
    @patch("apm_cli.commands.update.os.chmod")
    @patch("apm_cli.utils.version_checker.get_latest_version_from_github", return_value="1.1.0")
    def test_update_network_error_exits_1(self, mock_latest, mock_chmod, mock_version, mock_get):
        mock_get.side_effect = Exception("Network error")

        with patch.object(update_module.sys, "platform", "linux"):
            result = self.runner.invoke(cli, ["update"])

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Update failed", result.output)

    @patch("requests.get")
    @patch("subprocess.run")
    @patch("apm_cli.commands.update.get_version", return_value="1.0.0")
    @patch("apm_cli.commands.update.os.chmod")
    @patch("apm_cli.utils.version_checker.get_latest_version_from_github", return_value="1.1.0")
    def test_update_temp_file_cleanup_on_success(
        self, mock_latest, mock_chmod, mock_version, mock_run, mock_get
    ):
        """Verify temporary script is deleted after successful install."""
        deleted_paths = []
        original_unlink = update_module.os.unlink

        def tracking_unlink(path):
            deleted_paths.append(path)
            original_unlink(path)

        mock_response = Mock()
        mock_response.text = "echo install"
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response
        mock_run.return_value = Mock(returncode=0)

        with (
            patch.object(update_module.sys, "platform", "linux"),
            patch("apm_cli.commands.update.os.path.exists", return_value=True),
            patch.object(update_module.os, "unlink", side_effect=tracking_unlink),
        ):
            result = self.runner.invoke(cli, ["update"])

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(deleted_paths), 1)
        self.assertTrue(deleted_paths[0].endswith(".sh"))


if __name__ == "__main__":
    unittest.main()
