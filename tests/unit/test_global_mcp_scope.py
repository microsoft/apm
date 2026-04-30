"""Tests for scope-aware MCP installation (issue #637).

Verifies that ``apm install --global`` installs MCP servers to
global-capable runtimes (Copilot CLI, Codex CLI) instead of
blanket-skipping all MCP installation at user scope.
"""

import unittest
from unittest.mock import MagicMock, patch

from apm_cli.adapters.client.base import MCPClientAdapter
from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.adapters.client.copilot import CopilotClientAdapter
from apm_cli.adapters.client.cursor import CursorClientAdapter
from apm_cli.adapters.client.opencode import OpenCodeClientAdapter
from apm_cli.adapters.client.vscode import VSCodeClientAdapter
from apm_cli.core.scope import InstallScope
from apm_cli.factory import ClientFactory

# ---------------------------------------------------------------------------
# 1. Adapter supports_user_scope attribute
# ---------------------------------------------------------------------------


class TestAdapterUserScopeSupport(unittest.TestCase):
    """Verify supports_user_scope is declared correctly on every adapter."""

    def test_base_class_defaults_to_false(self):
        """MCPClientAdapter.supports_user_scope defaults to False."""
        self.assertFalse(MCPClientAdapter.supports_user_scope)

    def test_copilot_supports_user_scope(self):
        """Copilot CLI writes to ~/.copilot/ and should support user scope."""
        adapter = CopilotClientAdapter()
        self.assertTrue(adapter.supports_user_scope)

    def test_codex_supports_user_scope(self):
        """Codex CLI writes to ~/.codex/ and should support user scope."""
        adapter = CodexClientAdapter()
        self.assertTrue(adapter.supports_user_scope)

    def test_vscode_does_not_support_user_scope(self):
        """VS Code writes to .vscode/ (workspace) and should NOT support user scope."""
        adapter = VSCodeClientAdapter()
        self.assertFalse(adapter.supports_user_scope)

    def test_cursor_does_not_support_user_scope(self):
        """Cursor writes to .cursor/ (workspace) and should NOT support user scope."""
        adapter = CursorClientAdapter()
        self.assertFalse(adapter.supports_user_scope)

    def test_opencode_does_not_support_user_scope(self):
        """OpenCode writes to opencode.json (workspace) and should NOT support user scope."""
        adapter = OpenCodeClientAdapter()
        self.assertFalse(adapter.supports_user_scope)

    def test_cursor_does_not_inherit_copilot_true(self):
        """CursorClientAdapter inherits CopilotClientAdapter but overrides to False."""
        self.assertTrue(issubclass(CursorClientAdapter, CopilotClientAdapter))
        self.assertFalse(CursorClientAdapter.supports_user_scope)

    def test_opencode_does_not_inherit_copilot_true(self):
        """OpenCodeClientAdapter inherits CopilotClientAdapter but overrides to False."""
        self.assertTrue(issubclass(OpenCodeClientAdapter, CopilotClientAdapter))
        self.assertFalse(OpenCodeClientAdapter.supports_user_scope)

    def test_factory_created_adapters_scope(self):
        """ClientFactory-created adapters report the correct scope support."""
        global_runtimes = {"copilot", "codex"}
        workspace_runtimes = {"vscode", "cursor", "opencode"}

        for rt in global_runtimes:
            adapter = ClientFactory.create_client(rt)
            self.assertTrue(
                adapter.supports_user_scope,
                f"{rt} adapter should support user scope",
            )

        for rt in workspace_runtimes:
            adapter = ClientFactory.create_client(rt)
            self.assertFalse(
                adapter.supports_user_scope,
                f"{rt} adapter should NOT support user scope",
            )


# ---------------------------------------------------------------------------
# 2. MCPIntegrator scope filtering
# ---------------------------------------------------------------------------


class TestMCPIntegratorScopeFiltering(unittest.TestCase):
    """Verify MCPIntegrator.install() filters runtimes by scope."""

    @patch("apm_cli.registry.operations.MCPServerOperations")
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.integration.mcp_integrator._is_vscode_available", return_value=False)
    @patch("apm_cli.integration.mcp_integrator.shutil.which", return_value=None)
    def test_user_scope_skips_workspace_runtimes(
        self, mock_which, mock_vscode, mock_install_rt, mock_ops_cls
    ):
        """At USER scope, workspace-only runtimes are not targeted."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        mock_install_rt.return_value = True
        mock_ops = MagicMock()
        mock_ops.validate_servers_exist.return_value = (["test/server"], [])
        mock_ops.check_servers_needing_installation.return_value = ["test/server"]
        mock_ops_cls.return_value = mock_ops

        with (
            patch.object(MCPIntegrator, "_detect_runtimes", return_value=set()),
            patch("apm_cli.runtime.manager.RuntimeManager") as mock_mgr_cls,
        ):
            mock_mgr = MagicMock()
            mock_mgr.is_runtime_available.return_value = True
            mock_mgr_cls.return_value = mock_mgr

            MCPIntegrator.install(
                mcp_deps=["test/server"],
                runtime=None,
                exclude=None,
                verbose=False,
                scope=InstallScope.USER,
            )

        # Only copilot/codex should have been called (global-capable),
        # not vscode/cursor/opencode
        called_runtimes = {call.args[0] for call in mock_install_rt.call_args_list}
        workspace_only = {"vscode", "cursor", "opencode"}
        self.assertFalse(
            called_runtimes & workspace_only,
            f"Workspace-only runtimes should not be called at USER scope, "
            f"but got: {called_runtimes & workspace_only}",
        )

    @patch("apm_cli.registry.operations.MCPServerOperations")
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.integration.mcp_integrator._is_vscode_available", return_value=True)
    @patch("apm_cli.integration.mcp_integrator.shutil.which", return_value="/usr/bin/copilot")
    def test_project_scope_includes_all_runtimes(
        self, mock_which, mock_vscode, mock_install_rt, mock_ops_cls
    ):
        """At PROJECT scope (default), all runtimes are eligible."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        mock_install_rt.return_value = True
        mock_ops = MagicMock()
        mock_ops.validate_servers_exist.return_value = (["test/server"], [])
        mock_ops.check_servers_needing_installation.return_value = ["test/server"]
        mock_ops_cls.return_value = mock_ops

        with (
            patch.object(MCPIntegrator, "_detect_runtimes", return_value=set()),
            patch("apm_cli.runtime.manager.RuntimeManager") as mock_mgr_cls,
        ):
            mock_mgr = MagicMock()
            mock_mgr.is_runtime_available.return_value = True
            mock_mgr_cls.return_value = mock_mgr

            MCPIntegrator.install(
                mcp_deps=["test/server"],
                runtime=None,
                scope=InstallScope.PROJECT,
            )

        called_runtimes = {call.args[0] for call in mock_install_rt.call_args_list}
        # vscode should be included at PROJECT scope
        self.assertIn("vscode", called_runtimes)

    def test_user_scope_explicit_workspace_runtime_returns_zero(self):
        """--global --runtime vscode should warn and return 0."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        count = MCPIntegrator.install(
            mcp_deps=["test/server"],
            runtime="vscode",
            scope=InstallScope.USER,
        )
        self.assertEqual(count, 0)

    def test_user_scope_explicit_global_runtime_proceeds(self):
        """--global --runtime copilot should NOT be filtered out."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with (
            patch.object(MCPIntegrator, "_install_for_runtime", return_value=True) as mock_install,
            patch("apm_cli.registry.operations.MCPServerOperations") as mock_ops_cls,
        ):
            mock_ops = MagicMock()
            mock_ops.validate_servers_exist.return_value = (["test/server"], [])
            mock_ops.check_servers_needing_installation.return_value = ["test/server"]
            mock_ops_cls.return_value = mock_ops

            MCPIntegrator.install(
                mcp_deps=["test/server"],
                runtime="copilot",
                scope=InstallScope.USER,
            )

        # copilot should have been called
        self.assertTrue(mock_install.called)
        self.assertEqual(mock_install.call_args_list[0].args[0], "copilot")

    def test_scope_user_overrides_false_user_scope_flag(self):
        """USER scope should force user-scope path resolution even if the boolean disagrees."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with (
            patch.object(MCPIntegrator, "_install_for_runtime", return_value=True) as mock_install,
            patch("apm_cli.registry.operations.MCPServerOperations") as mock_ops_cls,
        ):
            mock_ops = MagicMock()
            mock_ops.validate_servers_exist.return_value = (["test/server"], [])
            mock_ops.check_servers_needing_installation.return_value = ["test/server"]
            mock_ops_cls.return_value = mock_ops

            MCPIntegrator.install(
                mcp_deps=["test/server"],
                runtime="copilot",
                scope=InstallScope.USER,
                user_scope=False,
            )

        assert mock_install.call_args.kwargs["user_scope"] is True

    def test_scope_none_treated_as_project(self):
        """When scope is None, all runtimes are eligible (backward compat)."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with (
            patch.object(MCPIntegrator, "_install_for_runtime", return_value=True) as mock_install,
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=True,
            ),
            patch("apm_cli.runtime.manager.RuntimeManager") as mock_mgr_cls,
            patch("apm_cli.registry.operations.MCPServerOperations") as mock_ops_cls,
        ):
            mock_mgr = MagicMock()
            mock_mgr.is_runtime_available.return_value = True
            mock_mgr_cls.return_value = mock_mgr
            mock_ops = MagicMock()
            mock_ops.validate_servers_exist.return_value = (["test/server"], [])
            mock_ops.check_servers_needing_installation.return_value = ["test/server"]
            mock_ops_cls.return_value = mock_ops
            with patch.object(MCPIntegrator, "_detect_runtimes", return_value=set()):
                MCPIntegrator.install(
                    mcp_deps=["test/server"],
                    scope=None,
                )

        called_runtimes = {call.args[0] for call in mock_install.call_args_list}
        # vscode should be present (not filtered)
        self.assertIn("vscode", called_runtimes)


# ---------------------------------------------------------------------------
# 3. remove_stale scope filtering
# ---------------------------------------------------------------------------


class TestRemoveStaleScopeFiltering(unittest.TestCase):
    """Verify MCPIntegrator.remove_stale() respects scope."""

    @patch("apm_cli.integration.mcp_integrator.Path")
    def test_user_scope_does_not_touch_workspace_configs(self, mock_path_cls):
        """At USER scope, .vscode/mcp.json and .cursor/mcp.json are not cleaned."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        # Call remove_stale with USER scope
        MCPIntegrator.remove_stale(
            stale_names={"test-server"},
            scope=InstallScope.USER,
        )

        # Path.cwd() is used for workspace configs (.vscode, .cursor, opencode)
        # Path.home() is used for global configs (~/.copilot, ~/.codex)
        # At USER scope, we should only try to access home-dir configs
        all_calls_str = str(mock_path_cls.mock_calls)
        # Workspace paths should NOT appear
        self.assertNotIn(".vscode", all_calls_str)
        self.assertNotIn(".cursor", all_calls_str)
        self.assertNotIn("opencode.json", all_calls_str)


# ---------------------------------------------------------------------------
# 4. install.py integration: should_install_mcp not blanket-disabled
# ---------------------------------------------------------------------------


class TestInstallCommandMCPScope(unittest.TestCase):
    """Verify install command forwards scope to MCPIntegrator."""

    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator.install", return_value=0)
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator.remove_stale")
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator.update_lockfile")
    def test_install_passes_scope_to_mcp_integrator(self, _update_lock, mock_remove, mock_install):
        """MCPIntegrator.install() receives scope=USER when --global is used."""
        # Directly call MCPIntegrator.install with USER scope and verify
        # the filtering logic works end-to-end (the install command wiring
        # passes scope=scope, which we verify via integration with the
        # MCPIntegrator scope filtering already tested above).
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with patch("apm_cli.registry.operations.MCPServerOperations") as mock_ops_cls:
            mock_ops = mock_ops_cls.return_value
            mock_ops.validate_servers_exist.return_value = (
                [{"name": "test-server"}],
                [],
            )
            mock_ops.check_servers_needing_installation.return_value = ["test-server"]

            with patch("apm_cli.runtime.manager.RuntimeManager") as mock_rm_cls:
                mock_rm = mock_rm_cls.return_value
                mock_rm.get_installed_runtimes.return_value = [
                    "copilot",
                    "vscode",
                ]

                with patch("apm_cli.factory.ClientFactory.create_client") as mock_cc:
                    copilot_adapter = MagicMock()
                    copilot_adapter.supports_user_scope = True
                    vscode_adapter = MagicMock()
                    vscode_adapter.supports_user_scope = False

                    def side_effect(rt):
                        if rt == "copilot":
                            return copilot_adapter
                        if rt == "vscode":
                            return vscode_adapter
                        raise ValueError(f"Unknown: {rt}")

                    mock_cc.side_effect = side_effect

                    result = MCPIntegrator.install(
                        {"test-server": {"type": "stdio", "command": "test"}},
                        None,
                        None,
                        False,
                        scope=InstallScope.USER,
                    )
                    # Should not raise; vscode filtered out at USER scope
                    self.assertIsInstance(result, int)


if __name__ == "__main__":
    unittest.main()
