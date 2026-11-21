"""Tests for the platform-aware update command (pure logic cases)."""

import unittest
from unittest.mock import patch

import pytest

import apm_cli.commands.self_update as update_module

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-self-update-release-resolution")]


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

    def test_installer_run_command_unix_bash_found(self):
        with (
            patch.object(update_module.sys, "platform", "linux"),
            patch.object(update_module.shutil, "which", return_value="/usr/bin/bash"),
        ):
            cmd = update_module._get_installer_run_command("/tmp/install.sh")
        self.assertEqual(cmd, ["/usr/bin/bash", "/tmp/install.sh"])

    def test_installer_run_command_unix_fallback_to_bin_bash(self):
        with (
            patch.object(update_module.sys, "platform", "linux"),
            patch.object(update_module.shutil, "which", return_value=None),
            patch.object(update_module.os.path, "exists", return_value=True),
        ):
            cmd = update_module._get_installer_run_command("/tmp/install.sh")
        self.assertEqual(cmd, ["/bin/bash", "/tmp/install.sh"])

    def test_installer_run_command_unix_no_bash_raises(self):
        with (
            patch.object(update_module.sys, "platform", "linux"),
            patch.object(update_module.shutil, "which", return_value=None),
            patch.object(update_module.os.path, "exists", return_value=False),
        ):
            with self.assertRaises(FileNotFoundError):
                update_module._get_installer_run_command("/tmp/install.sh")

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


if __name__ == "__main__":
    unittest.main()
