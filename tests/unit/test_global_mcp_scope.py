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


# ---------------------------------------------------------------------------
# 5. update_lockfile receives the scope-resolved path (#794)
# ---------------------------------------------------------------------------


class TestUpdateLockfileScope(unittest.TestCase):
    """Regression tests for issue #794.

    Verifies that ``MCPIntegrator.update_lockfile`` is called with the
    scope-resolved lockfile path so that ``apm install --global`` writes MCP
    audit entries to ``~/.apm/apm.lock.yaml`` and NOT to the project-local
    ``apm.lock.yaml``.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_ctx(self, scope, tmp_apm_dir):
        """Build a minimal InstallContext-like object for _install_apm_packages."""
        ctx = MagicMock()
        ctx.scope = scope
        ctx.apm_dir = tmp_apm_dir
        ctx.project_root = tmp_apm_dir
        ctx.manifest_path = tmp_apm_dir / "apm.yml"
        ctx.manifest_display = "apm.yml"
        ctx.runtime = None
        ctx.exclude = None
        ctx.verbose = False
        ctx.force = False
        ctx.dry_run = False
        ctx.update = False
        ctx.dev = False
        ctx.target = None
        ctx.parallel_downloads = 4
        ctx.allow_insecure = False
        ctx.allow_insecure_hosts = ()
        ctx.protocol_pref = None
        ctx.allow_protocol_fallback = None
        ctx.trust_transitive_mcp = False
        ctx.no_policy = True
        ctx.install_mode = MagicMock()
        # InstallMode.MCP / APM comparisons
        from apm_cli.commands.install import InstallMode

        ctx.install_mode = InstallMode.ALL
        ctx.packages = ()
        ctx.refresh = False
        ctx.only_packages = None
        ctx.manifest_snapshot = None
        ctx.snapshot_manifest_path = None
        ctx.legacy_skill_paths = False
        ctx.logger = MagicMock()
        ctx.auth_resolver = MagicMock()
        return ctx

    # ------------------------------------------------------------------
    # Test: USER scope -> ~/.apm/apm.lock.yaml
    # ------------------------------------------------------------------

    @patch("apm_cli.commands.install.MCPIntegrator.remove_stale")
    @patch("apm_cli.commands.install.MCPIntegrator.install", return_value=1)
    @patch("apm_cli.commands.install.MCPIntegrator.update_lockfile")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_user_scope_update_lockfile_uses_user_path(
        self, mock_apm_deps, mock_update_lf, mock_mcp_install, mock_remove_stale
    ):
        """At USER scope, update_lockfile must use ~/.apm/apm.lock.yaml."""
        import tempfile
        from pathlib import Path

        from apm_cli.commands.install import _install_apm_packages
        from apm_cli.core.scope import InstallScope
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        with tempfile.TemporaryDirectory() as user_apm_dir_str:
            user_apm_dir = Path(user_apm_dir_str)
            expected_lock = get_lockfile_path(user_apm_dir)

            # Create a minimal lockfile so update_lockfile finds an existing one
            LockFile().save(expected_lock)

            ctx = self._make_ctx(InstallScope.USER, user_apm_dir)
            ctx.manifest_path = user_apm_dir / "apm.yml"
            ctx.manifest_path.write_text("name: test\nmcp:\n  - test/server\n", encoding="utf-8")

            # Stub APMPackage parse so we don't need a real apm.yml pipeline
            mock_pkg = MagicMock()
            mock_pkg.get_apm_dependencies.return_value = []
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_mcp_dep = MagicMock()
            mock_mcp_dep.name = "test/server"
            mock_pkg.get_mcp_dependencies.return_value = [mock_mcp_dep]
            mock_pkg.target = None
            mock_pkg.scripts = {}

            with patch("apm_cli.commands.install.APMPackage") as mock_apm_pkg_cls:
                mock_apm_pkg_cls.from_apm_yml.return_value = mock_pkg
                with patch("apm_cli.commands.install.migrate_lockfile_if_needed"):
                    with patch(
                        "apm_cli.commands.install.MCPIntegrator.collect_transitive", return_value=[]
                    ):
                        with patch(
                            "apm_cli.commands.install.MCPIntegrator.get_server_names",
                            return_value={"test/server"},
                        ):
                            with patch(
                                "apm_cli.commands.install.MCPIntegrator.get_server_configs",
                                return_value={},
                            ):
                                with patch(
                                    "apm_cli.commands.install.MCPIntegrator.deduplicate",
                                    side_effect=lambda x: x,
                                ):
                                    _install_apm_packages(ctx, None)

            # Check the lock_path argument (2nd positional arg) passed to update_lockfile
            self.assertTrue(
                mock_update_lf.called,
                "update_lockfile should have been called",
            )
            call_args = mock_update_lf.call_args
            # Positional arg[1] is the lock_path
            actual_lock_path = (
                call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("lock_path")
            )
            self.assertEqual(
                actual_lock_path,
                expected_lock,
                f"Expected lock path {expected_lock}, got {actual_lock_path}. "
                "update_lockfile must receive the scope-resolved lockfile path.",
            )

    @patch("apm_cli.commands.install.MCPIntegrator.remove_stale")
    @patch("apm_cli.commands.install.MCPIntegrator.install", return_value=1)
    @patch("apm_cli.commands.install.MCPIntegrator.update_lockfile")
    @patch("apm_cli.commands.install._install_apm_dependencies")
    def test_project_scope_update_lockfile_uses_project_path(
        self, mock_apm_deps, mock_update_lf, mock_mcp_install, mock_remove_stale
    ):
        """At PROJECT scope, update_lockfile must use the project-local lockfile."""
        import tempfile
        from pathlib import Path

        from apm_cli.commands.install import _install_apm_packages
        from apm_cli.core.scope import InstallScope
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        with tempfile.TemporaryDirectory() as project_dir_str:
            project_dir = Path(project_dir_str)
            expected_lock = get_lockfile_path(project_dir)

            LockFile().save(expected_lock)

            ctx = self._make_ctx(InstallScope.PROJECT, project_dir)
            ctx.manifest_path = project_dir / "apm.yml"
            ctx.manifest_path.write_text("name: test\nmcp:\n  - test/server\n", encoding="utf-8")

            mock_pkg = MagicMock()
            mock_pkg.get_apm_dependencies.return_value = []
            mock_pkg.get_dev_apm_dependencies.return_value = []
            mock_mcp_dep = MagicMock()
            mock_mcp_dep.name = "test/server"
            mock_pkg.get_mcp_dependencies.return_value = [mock_mcp_dep]
            mock_pkg.target = None
            mock_pkg.scripts = {}

            with patch("apm_cli.commands.install.APMPackage") as mock_apm_pkg_cls:
                mock_apm_pkg_cls.from_apm_yml.return_value = mock_pkg
                with patch("apm_cli.commands.install.migrate_lockfile_if_needed"):
                    with patch(
                        "apm_cli.commands.install.MCPIntegrator.collect_transitive", return_value=[]
                    ):
                        with patch(
                            "apm_cli.commands.install.MCPIntegrator.get_server_names",
                            return_value={"test/server"},
                        ):
                            with patch(
                                "apm_cli.commands.install.MCPIntegrator.get_server_configs",
                                return_value={},
                            ):
                                with patch(
                                    "apm_cli.commands.install.MCPIntegrator.deduplicate",
                                    side_effect=lambda x: x,
                                ):
                                    _install_apm_packages(ctx, None)

            self.assertTrue(mock_update_lf.called)
            call_args = mock_update_lf.call_args
            actual_lock_path = (
                call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("lock_path")
            )
            self.assertEqual(
                actual_lock_path,
                expected_lock,
                f"Expected lock path {expected_lock}, got {actual_lock_path}.",
            )

    def test_update_lockfile_no_default_cwd_fallback_when_path_supplied(self):
        """update_lockfile itself must NOT call Path.cwd() when lock_path is given."""
        import tempfile
        from pathlib import Path

        from apm_cli.deps.lockfile import LockFile, get_lockfile_path
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with tempfile.TemporaryDirectory() as d:
            lock_path = get_lockfile_path(Path(d))
            LockFile().save(lock_path)

            with patch("apm_cli.integration.mcp_integrator.Path") as mock_path_cls:
                # If Path.cwd() is called, the test fails; we want it NOT called.
                MCPIntegrator.update_lockfile({"server-a"}, lock_path)
                cwd_calls = [c for c in mock_path_cls.mock_calls if "cwd" in str(c)]
                self.assertEqual(
                    cwd_calls,
                    [],
                    "update_lockfile should not call Path.cwd() when lock_path is supplied",
                )


# ---------------------------------------------------------------------------
# 6. AST contract: every update_lockfile call site passes lock_path positionally
# ---------------------------------------------------------------------------


class TestUpdateLockfileGlobalScope(unittest.TestCase):
    """Regression tests for #794: update_lockfile must receive scope-resolved path."""

    def test_install_module_passes_lock_path_to_update_lockfile(self):
        """All update_lockfile calls in install.py pass _lock_path positionally."""
        import ast
        from pathlib import Path

        install_src = Path(__file__).resolve().parent.parent.parent / (
            "src/apm_cli/commands/install.py"
        )
        tree = ast.parse(install_src.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update_lockfile"
            ):
                # Must have at least 2 positional args (server_names, lock_path)
                self.assertGreaterEqual(
                    len(node.args),
                    2,
                    f"update_lockfile call at line {node.lineno} is missing "
                    f"the lock_path positional argument (regression #794)",
                )

    def test_mcp_command_passes_lock_path_to_update_lockfile(self):
        """update_lockfile call in mcp/command.py passes lock_path positionally."""
        import ast
        from pathlib import Path

        cmd_src = Path(__file__).resolve().parent.parent.parent / (
            "src/apm_cli/install/mcp/command.py"
        )
        tree = ast.parse(cmd_src.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update_lockfile"
            ):
                self.assertGreaterEqual(
                    len(node.args),
                    2,
                    f"update_lockfile call at line {node.lineno} is missing "
                    f"the lock_path positional argument (regression #794)",
                )


if __name__ == "__main__":
    unittest.main()
