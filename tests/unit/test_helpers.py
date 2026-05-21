"""Tests for helper utility functions."""

import json
import sys
import unittest
from pathlib import Path

from apm_cli.utils.helpers import (
    detect_platform,
    find_plugin_json,
    get_available_package_managers,
    is_tool_available,
)


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
        import sys

        if sys.platform != "win32":
            # Skip this assertion on Windows since it might not have any
            # On Unix systems, we expect at least one package manager
            self.assertGreater(
                len(managers),
                0,
                "Expected at least one package manager on Unix systems",
            )


class TestFindPluginJson(unittest.TestCase):
    """Test cases for find_plugin_json deterministic location check."""

    def test_finds_root_plugin_json(self, tmp_path=None):
        """Root plugin.json is returned when present."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pj = root / "plugin.json"
            pj.write_text(json.dumps({"name": "test"}))
            assert find_plugin_json(root) == pj

    def test_finds_github_plugin_json(self):
        """plugin.json under .github/plugin/ is found."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / ".github" / "plugin" / "plugin.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({"name": "gh"}))
            assert find_plugin_json(root) == target

    def test_finds_claude_plugin_json(self):
        """plugin.json under .claude-plugin/ is found."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / ".claude-plugin" / "plugin.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({"name": "claude"}))
            assert find_plugin_json(root) == target

    def test_finds_cursor_plugin_json(self):
        """plugin.json under .cursor-plugin/ is found."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / ".cursor-plugin" / "plugin.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({"name": "cursor"}))
            assert find_plugin_json(root) == target

    def test_priority_order(self):
        """Root wins over .github/plugin/ which wins over .claude-plugin/ which wins over .cursor-plugin/."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for sub in [
                "plugin.json",
                ".github/plugin/plugin.json",
                ".claude-plugin/plugin.json",
                ".cursor-plugin/plugin.json",
            ]:
                p = root / sub
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"name": sub}))
            assert find_plugin_json(root) == root / "plugin.json"

    def test_cursor_plugin_found_when_only_option(self):
        """When only .cursor-plugin/ has plugin.json, it is found."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / ".cursor-plugin" / "plugin.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({"name": "cursor-only"}))
            # No root, .github, or .claude-plugin plugin.json
            assert find_plugin_json(root) == target

    def test_ignores_unrelated_locations(self):
        """plugin.json buried in node_modules or other dirs is NOT found."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            hidden = root / "node_modules" / "evil" / "plugin.json"
            hidden.parent.mkdir(parents=True)
            hidden.write_text(json.dumps({"name": "evil"}))
            assert find_plugin_json(root) is None

    def test_returns_none_when_absent(self):
        """None is returned when no plugin.json exists anywhere."""
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            assert find_plugin_json(Path(d)) is None


if __name__ == "__main__":
    unittest.main()


# ── Extended coverage tests ──────────────────────────────────────────


class TestIsToolAvailableEdgeCases:
    """Tests covering Windows path and exception fallback in is_tool_available."""

    def test_exception_in_subprocess_returns_false(self, monkeypatch):
        """subprocess.run raising an exception returns False."""
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        import subprocess

        monkeypatch.setattr(
            subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("no subprocess"))
        )
        assert is_tool_available("no-such-tool-xyz") is False

    def test_windows_path_returns_true_on_zero_returncode(self, monkeypatch):
        """Simulate win32 with subprocess returning rc=0."""
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(sys, "platform", "win32")

        mock_result = type("R", (), {"returncode": 0})()
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
        assert is_tool_available("some-tool") is True

    def test_windows_path_returns_false_on_nonzero_returncode(self, monkeypatch):
        """Simulate win32 with subprocess returning rc=1."""
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(sys, "platform", "win32")

        mock_result = type("R", (), {"returncode": 1})()
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
        assert is_tool_available("nonexistent") is False


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

    def test_no_tools_returns_empty(self, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        import subprocess

        mock_result = type("R", (), {"returncode": 1})()
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
        managers = get_available_package_managers()
        assert managers == {}


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
        mock_installer.install_servers.assert_called_once_with(["my-server"])

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
