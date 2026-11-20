"""Tests for helper utility functions."""

import sys
import unittest

import pytest

from apm_cli.utils.helpers import (
    detect_platform,
    get_available_package_managers,
    is_tool_available,
)

pytestmark = pytest.mark.unit


class TestHelpers(unittest.TestCase):
    """Test cases for helper utility functions."""

    def test_is_tool_available(self):
        """Test is_tool_available function with known commands."""
        # Python should always be available in the test environment
        self.assertTrue(is_tool_available("python"))

        # Test a command that almost certainly doesn't exist
        self.assertFalse(is_tool_available("this_command_does_not_exist_12345"))

    def test_detect_platform(self):
        """Test detect_platform function."""
        platform = detect_platform()
        self.assertIn(platform, ["macos", "linux", "windows", "unknown"])

    def test_get_available_package_managers(self):
        """Test get_available_package_managers function."""
        managers = get_available_package_managers()
        self.assertIsInstance(managers, dict)

        # The function should return a valid dict
        # If any managers are found, they should have valid string values
        for name, path in managers.items():
            self.assertIsInstance(name, str)
            self.assertIsInstance(path, str)
            self.assertTrue(len(name) > 0)
            self.assertTrue(len(path) > 0)

        # On most Unix systems, at least one package manager should be available
        # This is a reasonable expectation but not guaranteed on minimal systems

        if sys.platform != "win32":
            # Skip this assertion on Windows since it might not have any
            # On Unix systems, we expect at least one package manager
            self.assertGreater(
                len(managers),
                0,
                "Expected at least one package manager on Unix systems",
            )


if __name__ == "__main__":
    unittest.main()


# ── Extended coverage tests ──────────────────────────────────────────


class TestDetectPlatformAllBranches:
    """Tests covering linux, windows, unknown in detect_platform."""

    def test_linux(self, monkeypatch):
        import platform

        monkeypatch.setattr(platform, "system", lambda: "Linux")
        assert detect_platform() == "linux"

    def test_windows(self, monkeypatch):
        import platform

        monkeypatch.setattr(platform, "system", lambda: "Windows")
        assert detect_platform() == "windows"

    def test_unknown(self, monkeypatch):
        import platform

        monkeypatch.setattr(platform, "system", lambda: "FreeBSD")
        assert detect_platform() == "unknown"


class TestGetAvailablePackageManagersBranches:
    """Tests covering specific package manager availability paths."""

    def test_pipx_included_when_available(self, monkeypatch):
        import shutil

        def fake_which(name):
            if name == "pipx":
                return "/usr/bin/pipx"
            return None

        monkeypatch.setattr(shutil, "which", fake_which)
        managers = get_available_package_managers()
        assert "pipx" in managers

    def test_apt_included_when_available(self, monkeypatch):
        import shutil

        def fake_which(name):
            return "/usr/bin/apt" if name == "apt" else None

        monkeypatch.setattr(shutil, "which", fake_which)
        managers = get_available_package_managers()
        assert "apt" in managers

    def test_yum_included_when_available(self, monkeypatch):
        import shutil

        def fake_which(name):
            return "/usr/bin/yum" if name == "yum" else None

        monkeypatch.setattr(shutil, "which", fake_which)
        managers = get_available_package_managers()
        assert "yum" in managers

    def test_dnf_included_when_available(self, monkeypatch):
        import shutil

        def fake_which(name):
            return "/usr/bin/dnf" if name == "dnf" else None

        monkeypatch.setattr(shutil, "which", fake_which)
        managers = get_available_package_managers()
        assert "dnf" in managers

    def test_apk_included_when_available(self, monkeypatch):
        import shutil

        def fake_which(name):
            return "/sbin/apk" if name == "apk" else None

        monkeypatch.setattr(shutil, "which", fake_which)
        managers = get_available_package_managers()
        assert "apk" in managers

    def test_pacman_included_when_available(self, monkeypatch):
        import shutil

        def fake_which(name):
            return "/usr/bin/pacman" if name == "pacman" else None

        monkeypatch.setattr(shutil, "which", fake_which)
        managers = get_available_package_managers()
        assert "pacman" in managers


# ── core/operations.py coverage ──────────────────────────────────────


class TestCoreOperations:
    """Tests covering missed paths in core/operations.py."""

    def test_configure_client_success(self, monkeypatch):
        """configure_client returns True on success."""
        from unittest.mock import MagicMock

        from apm_cli.core import operations as ops

        mock_client = MagicMock()
        monkeypatch.setattr(
            "apm_cli.core.operations.ClientFactory.create_client",
            lambda *a, **kw: mock_client,
        )
        result = ops.configure_client("cursor", {"key": "val"})
        assert result is True
        mock_client.update_config.assert_called_once()

    def test_configure_client_exception_returns_false(self, monkeypatch):
        """configure_client returns False and prints on exception (lines 36-38)."""
        from apm_cli.core import operations as ops

        def raise_err(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr("apm_cli.core.operations.ClientFactory.create_client", raise_err)
        result = ops.configure_client("cursor", {})
        assert result is False

    def test_install_package_else_branch(self, monkeypatch):
        """install_package uses else branch when no shared params passed (line 89)."""
        from unittest.mock import MagicMock

        from apm_cli.core import operations as ops

        mock_summary = MagicMock()
        mock_summary.installed = []
        mock_summary.skipped = []
        mock_summary.failed = []

        mock_installer = MagicMock()
        mock_installer.install_servers.return_value = mock_summary
        monkeypatch.setattr(
            "apm_cli.core.operations.SafeMCPInstaller",
            lambda *a, **kw: mock_installer,
        )

        result = ops.install_package("cursor", "my-server")
        assert result["success"] is True
        mock_installer.install_servers.assert_called_once_with(
            ["my-server"],
            replace_existing=False,
            managed_fields=None,
        )

    def test_install_package_exception_returns_false(self, monkeypatch):
        """install_package returns failure dict on exception."""
        from apm_cli.core import operations as ops

        def raise_err(*a, **kw):
            raise ValueError("bad package")

        monkeypatch.setattr("apm_cli.core.operations.SafeMCPInstaller", raise_err)
        result = ops.install_package("cursor", "pkg")
        assert result["success"] is False
        assert result["installed"] is False

    def test_uninstall_package_removes_legacy_config(self, monkeypatch):
        """uninstall_package removes legacy config entry when present (lines 135-140)."""
        from unittest.mock import MagicMock

        from apm_cli.core import operations as ops

        package_name = "my-pkg"
        mock_client = MagicMock()
        mock_client.get_current_config.return_value = {f"mcp.package.{package_name}.enabled": True}

        mock_pm = MagicMock()
        mock_pm.uninstall.return_value = True

        monkeypatch.setattr(
            "apm_cli.core.operations.ClientFactory.create_client",
            lambda *a, **kw: mock_client,
        )
        monkeypatch.setattr(
            "apm_cli.core.operations.PackageManagerFactory.create_package_manager",
            lambda: mock_pm,
        )

        result = ops.uninstall_package("cursor", package_name)
        assert result is True
        mock_client.update_config.assert_called_once()

    def test_uninstall_package_no_legacy_config(self, monkeypatch):
        """uninstall_package skips config update when key not present (line 136 False branch)."""
        from unittest.mock import MagicMock

        from apm_cli.core import operations as ops

        mock_client = MagicMock()
        mock_client.get_current_config.return_value = {}

        mock_pm = MagicMock()
        mock_pm.uninstall.return_value = True

        monkeypatch.setattr(
            "apm_cli.core.operations.ClientFactory.create_client",
            lambda *a, **kw: mock_client,
        )
        monkeypatch.setattr(
            "apm_cli.core.operations.PackageManagerFactory.create_package_manager",
            lambda: mock_pm,
        )

        result = ops.uninstall_package("cursor", "pkg")
        assert result is True
        mock_client.update_config.assert_not_called()

    def test_uninstall_package_exception_returns_false(self, monkeypatch):
        """uninstall_package returns False on exception (lines 143-145)."""
        from apm_cli.core import operations as ops

        def raise_err(*a, **kw):
            raise RuntimeError("no client")

        monkeypatch.setattr("apm_cli.core.operations.ClientFactory.create_client", raise_err)
        result = ops.uninstall_package("cursor", "pkg")
        assert result is False
