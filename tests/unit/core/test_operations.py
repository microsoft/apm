"""Unit tests for core/operations.py."""

import unittest
from unittest.mock import MagicMock, patch


class TestConfigureClient(unittest.TestCase):
    """Tests for configure_client()."""

    def _call(self, client_type, config_updates):
        from apm_cli.core.operations import configure_client

        return configure_client(client_type, config_updates)

    def test_success(self):
        mock_client = MagicMock()
        with patch("apm_cli.core.operations.ClientFactory") as mock_factory:
            mock_factory.create_client.return_value = mock_client
            result = self._call("vscode", {"key": "value"})
        self.assertTrue(result)
        mock_factory.create_client.assert_called_once_with("vscode")
        mock_client.update_config.assert_called_once_with({"key": "value"})

    def test_exception_returns_false(self):
        with patch("apm_cli.core.operations.ClientFactory") as mock_factory:
            mock_factory.create_client.side_effect = RuntimeError("client error")
            result = self._call("vscode", {})
        self.assertFalse(result)

    def test_update_config_exception_returns_false(self):
        mock_client = MagicMock()
        mock_client.update_config.side_effect = ValueError("bad config")
        with patch("apm_cli.core.operations.ClientFactory") as mock_factory:
            mock_factory.create_client.return_value = mock_client
            result = self._call("vscode", {"k": "v"})
        self.assertFalse(result)


class TestInstallPackage(unittest.TestCase):
    """Tests for install_package()."""

    def _make_summary(self, installed=(), skipped=(), failed=()):
        summary = MagicMock()
        summary.installed = list(installed)
        summary.skipped = list(skipped)
        summary.failed = list(failed)
        return summary

    def _call(self, client_type="vscode", package_name="owner/repo", **kwargs):
        from apm_cli.core.operations import install_package

        return install_package(client_type, package_name, **kwargs)

    def test_success_no_shared_vars(self):
        summary = self._make_summary(installed=["owner/repo"])
        with patch("apm_cli.core.operations.SafeMCPInstaller") as mock_cls:
            mock_inst = mock_cls.return_value
            mock_inst.install_servers.return_value = summary
            result = self._call()
        self.assertTrue(result["success"])
        self.assertTrue(result["installed"])
        self.assertFalse(result["skipped"])
        self.assertFalse(result["failed"])
        mock_inst.install_servers.assert_called_once_with(["owner/repo"])

    def test_success_with_shared_env_vars(self):
        summary = self._make_summary(installed=["owner/repo"])
        with patch("apm_cli.core.operations.SafeMCPInstaller") as mock_cls:
            mock_inst = mock_cls.return_value
            mock_inst.install_servers.return_value = summary
            result = self._call(shared_env_vars={"KEY": "val"})
        self.assertTrue(result["success"])
        mock_inst.install_servers.assert_called_once_with(
            ["owner/repo"],
            env_overrides={"KEY": "val"},
            server_info_cache=None,
            runtime_vars=None,
        )

    def test_success_with_server_info_cache(self):
        summary = self._make_summary(skipped=["owner/repo"])
        with patch("apm_cli.core.operations.SafeMCPInstaller") as mock_cls:
            mock_inst = mock_cls.return_value
            mock_inst.install_servers.return_value = summary
            result = self._call(server_info_cache={"owner/repo": {}})
        self.assertTrue(result["success"])
        self.assertFalse(result["installed"])
        self.assertTrue(result["skipped"])

    def test_success_with_shared_runtime_vars(self):
        summary = self._make_summary()
        with patch("apm_cli.core.operations.SafeMCPInstaller") as mock_cls:
            mock_inst = mock_cls.return_value
            mock_inst.install_servers.return_value = summary
            result = self._call(shared_runtime_vars={"NODE": "/usr/bin/node"})
        self.assertTrue(result["success"])
        mock_inst.install_servers.assert_called_once_with(
            ["owner/repo"],
            env_overrides=None,
            server_info_cache=None,
            runtime_vars={"NODE": "/usr/bin/node"},
        )

    def test_success_with_all_shared_vars(self):
        summary = self._make_summary(installed=["owner/repo"])
        with patch("apm_cli.core.operations.SafeMCPInstaller") as mock_cls:
            mock_inst = mock_cls.return_value
            mock_inst.install_servers.return_value = summary
            result = self._call(
                shared_env_vars={"K": "v"},
                server_info_cache={"owner/repo": {}},
                shared_runtime_vars={"R": "v"},
            )
        self.assertTrue(result["success"])
        mock_inst.install_servers.assert_called_once_with(
            ["owner/repo"],
            env_overrides={"K": "v"},
            server_info_cache={"owner/repo": {}},
            runtime_vars={"R": "v"},
        )

    def test_failed_install_reflected_in_result(self):
        summary = self._make_summary(failed=["owner/repo"])
        with patch("apm_cli.core.operations.SafeMCPInstaller") as mock_cls:
            mock_inst = mock_cls.return_value
            mock_inst.install_servers.return_value = summary
            result = self._call()
        self.assertTrue(result["success"])
        self.assertFalse(result["installed"])
        self.assertTrue(result["failed"])

    def test_exception_returns_failure_dict(self):
        with patch("apm_cli.core.operations.SafeMCPInstaller") as mock_cls:
            mock_cls.side_effect = RuntimeError("installer error")
            result = self._call()
        self.assertFalse(result["success"])
        self.assertFalse(result["installed"])
        self.assertTrue(result["failed"])


class TestUninstallPackage(unittest.TestCase):
    """Tests for uninstall_package()."""

    def _call(self, client_type="vscode", package_name="owner/repo"):
        from apm_cli.core.operations import uninstall_package

        return uninstall_package(client_type, package_name)

    def test_success_no_legacy_config(self):
        mock_client = MagicMock()
        mock_client.get_current_config.return_value = {}
        mock_pm = MagicMock()
        mock_pm.uninstall.return_value = True
        with (
            patch("apm_cli.core.operations.ClientFactory") as mock_cf,
            patch("apm_cli.core.operations.PackageManagerFactory") as mock_pmf,
        ):
            mock_cf.create_client.return_value = mock_client
            mock_pmf.create_package_manager.return_value = mock_pm
            result = self._call()
        self.assertTrue(result)
        mock_pm.uninstall.assert_called_once_with("owner/repo")
        mock_client.update_config.assert_not_called()

    def test_success_with_legacy_config_entry(self):
        mock_client = MagicMock()
        mock_client.get_current_config.return_value = {
            "mcp.package.owner/repo.enabled": True
        }
        mock_pm = MagicMock()
        mock_pm.uninstall.return_value = True
        with (
            patch("apm_cli.core.operations.ClientFactory") as mock_cf,
            patch("apm_cli.core.operations.PackageManagerFactory") as mock_pmf,
        ):
            mock_cf.create_client.return_value = mock_client
            mock_pmf.create_package_manager.return_value = mock_pm
            result = self._call()
        self.assertTrue(result)
        mock_client.update_config.assert_called_once_with(
            {"mcp.package.owner/repo.enabled": None}
        )

    def test_exception_returns_false(self):
        with patch("apm_cli.core.operations.ClientFactory") as mock_cf:
            mock_cf.create_client.side_effect = RuntimeError("factory error")
            result = self._call()
        self.assertFalse(result)

    def test_uninstall_returns_false_when_pm_fails(self):
        mock_client = MagicMock()
        mock_client.get_current_config.return_value = {}
        mock_pm = MagicMock()
        mock_pm.uninstall.return_value = False
        with (
            patch("apm_cli.core.operations.ClientFactory") as mock_cf,
            patch("apm_cli.core.operations.PackageManagerFactory") as mock_pmf,
        ):
            mock_cf.create_client.return_value = mock_client
            mock_pmf.create_package_manager.return_value = mock_pm
            result = self._call()
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
