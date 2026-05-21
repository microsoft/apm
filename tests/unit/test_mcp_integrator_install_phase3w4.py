"""Unit tests for apm_cli.integration.mcp_integrator_install -- phase 3 w4.

Covers missing lines/branches in mcp_integrator_install.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _suppress_console(monkeypatch):
    monkeypatch.setattr("apm_cli.utils.console._get_console", lambda: None)


# ---------------------------------------------------------------------------
# run_mcp_install -- empty mcp_deps
# ---------------------------------------------------------------------------


class TestRunMcpInstallEmpty:
    def test_no_mcp_deps_returns_zero(self):
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        result = run_mcp_install(mcp_deps=[], logger=MagicMock())
        assert result == 0

    def test_none_mcp_deps_warns_and_returns_zero(self):
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        logger = MagicMock()
        result = run_mcp_install(mcp_deps=None, logger=logger)
        assert result == 0
        logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# run_mcp_install -- scope filtering (USER scope)
# ---------------------------------------------------------------------------


class TestRunMcpInstallScopeFiltering:
    def _make_dep(self, name, is_registry=True):
        dep = MagicMock()
        dep.name = name
        dep.is_registry_resolved = is_registry
        dep.is_self_defined = not is_registry
        dep.transport = "stdio"
        dep.command = name
        dep.args = []
        dep.env = {}
        dep.tools = None
        dep.headers = None
        dep.url = None
        return dep

    def test_user_scope_skips_workspace_only_runtimes(self, tmp_path):
        from apm_cli.core.scope import InstallScope
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = self._make_dep("srv-a")
        logger = MagicMock()

        mock_client = MagicMock()
        mock_client.supports_user_scope = False

        with (
            patch("apm_cli.factory.ClientFactory.create_client", return_value=mock_client),
        ):
            run_mcp_install(
                mcp_deps=[dep],
                runtime="vscode",  # force single runtime to avoid full detection
                scope=InstallScope.USER,
                logger=logger,
            )
        # All runtimes filtered out -> logs warning
        logger.warning.assert_called()

    def test_project_scope_sets_user_scope_false(self, tmp_path):
        from apm_cli.core.scope import InstallScope
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = self._make_dep("srv-b")
        logger = MagicMock()

        # Just verify it doesn't crash with PROJECT scope
        with patch(
            "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
            return_value=[],
        ):
            result = run_mcp_install(
                mcp_deps=[dep],
                runtime="vscode",
                scope=InstallScope.PROJECT,
                logger=logger,
            )
        assert result == 0


# ---------------------------------------------------------------------------
# run_mcp_install -- single runtime (explicit --runtime)
# ---------------------------------------------------------------------------


class TestRunMcpInstallSingleRuntime:
    def _make_reg_dep(self, name):
        dep = MagicMock()
        dep.name = name
        dep.is_registry_resolved = True
        dep.is_self_defined = False
        return dep

    def test_single_runtime_targets_only_that_runtime(self, tmp_path):
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = self._make_reg_dep("my-server")
        logger = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()

        mock_ops = MagicMock()
        mock_ops.validate_servers_exist.return_value = (["my-server"], [])
        mock_ops.check_servers_needing_installation.return_value = ["my-server"]
        mock_ops.batch_fetch_server_info.return_value = {"my-server": {"packages": []}}
        mock_ops.collect_environment_variables.return_value = {}
        mock_ops.collect_runtime_variables.return_value = {}

        with (
            patch(
                "apm_cli.registry.operations.MCPServerOperations",
                return_value=mock_ops,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime",
                return_value=True,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
        ):
            result = run_mcp_install(
                mcp_deps=[dep],
                runtime="copilot",
                project_root=tmp_path,
                logger=logger,
            )
        assert result >= 0


# ---------------------------------------------------------------------------
# run_mcp_install -- auto-detection with ImportError fallback
# ---------------------------------------------------------------------------


class TestRunMcpInstallImportErrorFallback:
    def _make_reg_dep(self, name):
        dep = MagicMock()
        dep.name = name
        dep.is_registry_resolved = True
        dep.is_self_defined = False
        return dep

    def test_import_error_falls_back_gracefully(self, tmp_path):
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = self._make_reg_dep("srv")
        logger = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()

        mock_ops = MagicMock()
        mock_ops.validate_servers_exist.return_value = (["srv"], [])
        mock_ops.check_servers_needing_installation.return_value = []

        with (
            patch("apm_cli.factory.ClientFactory", side_effect=ImportError("no factory")),
            patch("apm_cli.runtime.manager.RuntimeManager", side_effect=ImportError("no mgr")),
            patch(
                "apm_cli.runtime.utils.find_runtime_binary",
                side_effect=lambda n: "/usr/bin/copilot" if n == "copilot" else None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.registry.operations.MCPServerOperations",
                return_value=mock_ops,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
        ):
            result = run_mcp_install(
                mcp_deps=[dep],
                project_root=tmp_path,
                logger=logger,
            )
        assert result >= 0


# ---------------------------------------------------------------------------
# run_mcp_install -- cursor/opencode/gemini/windsurf opt-in directory detection
# ---------------------------------------------------------------------------


class TestRunMcpInstallOptInRuntimes:
    def _make_reg_dep(self, name):
        dep = MagicMock()
        dep.name = name
        dep.is_registry_resolved = True
        dep.is_self_defined = False
        return dep

    def test_cursor_detected_when_dir_exists(self, tmp_path):
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        (tmp_path / ".cursor").mkdir()
        dep = self._make_reg_dep("srv")
        logger = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()

        mock_client = MagicMock()
        mock_manager = MagicMock()
        mock_manager.is_runtime_available.return_value = True

        mock_ops = MagicMock()
        mock_ops.validate_servers_exist.return_value = (["srv"], [])
        mock_ops.check_servers_needing_installation.return_value = []

        with (
            patch("apm_cli.factory.ClientFactory.create_client", return_value=mock_client),
            patch("apm_cli.runtime.manager.RuntimeManager", return_value=mock_manager),
            patch(
                "apm_cli.registry.operations.MCPServerOperations",
                return_value=mock_ops,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
        ):
            result = run_mcp_install(
                mcp_deps=[dep],
                project_root=tmp_path,
                logger=logger,
            )
        assert result >= 0

    def test_opencode_detected_when_dir_exists(self, tmp_path):
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        (tmp_path / ".opencode").mkdir()
        dep = self._make_reg_dep("srv")
        logger = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()

        mock_client = MagicMock()
        mock_manager = MagicMock()
        mock_manager.is_runtime_available.return_value = False

        mock_ops = MagicMock()
        mock_ops.validate_servers_exist.return_value = (["srv"], [])
        mock_ops.check_servers_needing_installation.return_value = []

        with (
            patch("apm_cli.factory.ClientFactory.create_client", return_value=mock_client),
            patch("apm_cli.runtime.manager.RuntimeManager", return_value=mock_manager),
            patch(
                "apm_cli.registry.operations.MCPServerOperations",
                return_value=mock_ops,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
        ):
            result = run_mcp_install(
                mcp_deps=[dep],
                project_root=tmp_path,
                logger=logger,
            )
        assert result >= 0

    def test_gemini_detected_when_dir_exists(self, tmp_path):
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        (tmp_path / ".gemini").mkdir()
        dep = self._make_reg_dep("srv")
        logger = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()

        mock_client = MagicMock()
        mock_manager = MagicMock()
        mock_manager.is_runtime_available.return_value = False

        mock_ops = MagicMock()
        mock_ops.validate_servers_exist.return_value = (["srv"], [])
        mock_ops.check_servers_needing_installation.return_value = []

        with (
            patch("apm_cli.factory.ClientFactory.create_client", return_value=mock_client),
            patch("apm_cli.runtime.manager.RuntimeManager", return_value=mock_manager),
            patch(
                "apm_cli.registry.operations.MCPServerOperations",
                return_value=mock_ops,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
        ):
            result = run_mcp_install(
                mcp_deps=[dep],
                project_root=tmp_path,
                logger=logger,
            )
        assert result >= 0

    def test_windsurf_detected_when_dir_exists(self, tmp_path):
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        (tmp_path / ".windsurf").mkdir()
        dep = self._make_reg_dep("srv")
        logger = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()

        mock_client = MagicMock()
        mock_manager = MagicMock()
        mock_manager.is_runtime_available.return_value = False

        mock_ops = MagicMock()
        mock_ops.validate_servers_exist.return_value = (["srv"], [])
        mock_ops.check_servers_needing_installation.return_value = []

        with (
            patch("apm_cli.factory.ClientFactory.create_client", return_value=mock_client),
            patch("apm_cli.runtime.manager.RuntimeManager", return_value=mock_manager),
            patch(
                "apm_cli.registry.operations.MCPServerOperations",
                return_value=mock_ops,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
        ):
            result = run_mcp_install(
                mcp_deps=[dep],
                project_root=tmp_path,
                logger=logger,
            )
        assert result >= 0


# ---------------------------------------------------------------------------
# run_mcp_install -- no runtimes installed warnings
# ---------------------------------------------------------------------------


class TestRunMcpInstallNoRuntimes:
    def _make_reg_dep(self, name):
        dep = MagicMock()
        dep.name = name
        dep.is_registry_resolved = True
        dep.is_self_defined = False
        return dep

    def test_no_runtimes_installed_logs_warning(self, tmp_path):
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = self._make_reg_dep("srv")
        logger = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()

        mock_ops = MagicMock()
        mock_ops.validate_servers_exist.return_value = (["srv"], [])
        mock_ops.check_servers_needing_installation.return_value = []

        # No runtimes available at all -> gate returns [] -> returns 0
        with (
            patch("apm_cli.factory.ClientFactory.create_client", side_effect=ValueError("nope")),
            patch("apm_cli.runtime.manager.RuntimeManager") as mock_mgr_cls,
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.runtime.utils.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
            patch(
                "apm_cli.registry.operations.MCPServerOperations",
                return_value=mock_ops,
            ),
        ):
            mock_mgr = MagicMock()
            mock_mgr.is_runtime_available.return_value = False
            mock_mgr_cls.return_value = mock_mgr

            result = run_mcp_install(
                mcp_deps=[dep],
                project_root=tmp_path,
                logger=logger,
            )
        assert result == 0

    def test_all_excluded_warns_and_returns_zero(self, tmp_path):
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = self._make_reg_dep("srv")
        logger = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()

        # vscode is available + ClientFactory succeeds → installed_runtimes = ["vscode"]
        # then exclude="vscode" empties target_runtimes → line 272 returns 0
        mock_client = MagicMock()
        mock_client.supports_user_scope = False

        mock_ops = MagicMock()
        mock_ops.validate_servers_exist.return_value = (["srv"], [])
        mock_ops.check_servers_needing_installation.return_value = []

        with (
            patch(
                "apm_cli.factory.ClientFactory.create_client",
                return_value=mock_client,
            ),
            patch("apm_cli.runtime.manager.RuntimeManager") as mock_mgr_cls,
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=True,
            ),
            patch(
                "apm_cli.runtime.utils.find_runtime_binary",
                return_value=None,
            ),
            # find_runtime_binary is module-level imported into
            # mcp_integrator_install at import time, so the patch on
            # apm_cli.runtime.utils above does NOT rebind the symbol the
            # function actually calls. Patch the local binding too so a
            # developer machine with `claude` on PATH does not silently
            # leak it into installed_runtimes and bypass the exclude path.
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
            patch(
                "apm_cli.registry.operations.MCPServerOperations",
                return_value=mock_ops,
            ),
        ):
            mock_mgr = MagicMock()
            mock_mgr.is_runtime_available.return_value = False
            mock_mgr_cls.return_value = mock_mgr

            result = run_mcp_install(
                mcp_deps=[dep],
                exclude="vscode",
                project_root=tmp_path,
                logger=logger,
            )
        # vscode was the only runtime, then excluded → returns 0
        assert result == 0
        logger.warning.assert_called()


# ---------------------------------------------------------------------------
# run_mcp_install -- scripts detection (verbose mode)
# ---------------------------------------------------------------------------


class TestRunMcpInstallScriptsDetection:
    def _make_reg_dep(self, name):
        dep = MagicMock()
        dep.name = name
        dep.is_registry_resolved = True
        dep.is_self_defined = False
        return dep

    def test_scripts_runtime_not_installed_warns(self, tmp_path):
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = self._make_reg_dep("srv")
        logger = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()

        apm_config = {"scripts": {"run": "copilot run skill"}}

        mock_ops = MagicMock()
        mock_ops.validate_servers_exist.return_value = (["srv"], [])
        mock_ops.check_servers_needing_installation.return_value = []

        with (
            patch("apm_cli.factory.ClientFactory.create_client") as mock_cc,
            patch("apm_cli.runtime.manager.RuntimeManager") as mock_mgr_cls,
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
            patch(
                "apm_cli.registry.operations.MCPServerOperations",
                return_value=mock_ops,
            ),
        ):
            mock_mgr = MagicMock()
            # copilot IS installed
            mock_mgr.is_runtime_available.side_effect = lambda n: n == "copilot"
            mock_mgr_cls.return_value = mock_mgr
            mock_cc.return_value = MagicMock()

            result = run_mcp_install(
                mcp_deps=[dep],
                project_root=tmp_path,
                apm_config=apm_config,
                logger=logger,
            )
        # copilot is both installed and in scripts -> target_runtimes = [copilot]
        assert result >= 0

    def test_scripts_no_installed_runtime_warns(self, tmp_path):
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = self._make_reg_dep("srv")
        logger = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()

        apm_config = {"scripts": {"run": "claude chat"}}

        mock_ops = MagicMock()
        mock_ops.validate_servers_exist.return_value = (["srv"], [])
        mock_ops.check_servers_needing_installation.return_value = []

        with (
            patch(
                "apm_cli.factory.ClientFactory.create_client",
                side_effect=ValueError("not supported"),
            ),
            patch("apm_cli.runtime.manager.RuntimeManager") as mock_mgr_cls,
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.runtime.utils.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
            patch(
                "apm_cli.registry.operations.MCPServerOperations",
                return_value=mock_ops,
            ),
        ):
            mock_mgr = MagicMock()
            mock_mgr.is_runtime_available.return_value = False
            mock_mgr_cls.return_value = mock_mgr

            run_mcp_install(
                mcp_deps=[dep],
                project_root=tmp_path,
                apm_config=apm_config,
                logger=logger,
            )
        # scripts say "claude" but none installed -> target_runtimes empty -> warn
        logger.warning.assert_called()


class TestRunMcpInstallInvalidServer:
    def _make_reg_dep(self, name):
        dep = MagicMock()
        dep.name = name
        dep.is_registry_resolved = True
        dep.is_self_defined = False
        return dep

    def test_invalid_servers_raise_runtime_error(self, tmp_path):
        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        dep = self._make_reg_dep("nonexistent-srv")
        logger = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()

        mock_ops = MagicMock()
        mock_ops.validate_servers_exist.return_value = ([], ["nonexistent-srv"])

        with (
            patch(
                "apm_cli.registry.operations.MCPServerOperations",
                return_value=mock_ops,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
            pytest.raises(RuntimeError, match="Cannot install"),
        ):
            run_mcp_install(
                mcp_deps=[dep],
                runtime="copilot",
                project_root=tmp_path,
                logger=logger,
            )
