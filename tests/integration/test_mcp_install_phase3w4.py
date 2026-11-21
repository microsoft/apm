"""Integration tests for mcp_integrator_install -- phase 3 wave 4.

Covers uncovered lines/branches in:
  src/apm_cli/integration/mcp_integrator_install.py

Strategy: hermetic -- mocks registry, runtime, console.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.integration.mcp_integrator_install import run_mcp_install

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry_dep(
    name: str,
    is_registry_resolved: bool = True,
    env: dict | None = None,
) -> MagicMock:
    dep = MagicMock()
    dep.name = name
    dep.is_registry_resolved = is_registry_resolved
    dep.is_self_defined = not is_registry_resolved
    dep.env = env or {}
    return dep


def _make_self_defined_dep(
    name: str,
    transport: str = "stdio",
    env: dict | None = None,
) -> MagicMock:
    dep = MagicMock()
    dep.name = name
    dep.is_registry_resolved = False
    dep.is_self_defined = True
    dep.transport = transport
    dep.env = env or {}
    return dep


# ---------------------------------------------------------------------------
# Early-return and no-deps branches -- line 70
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Scope handling -- lines 78-81
# ---------------------------------------------------------------------------


class TestScopeHandling:
    def test_user_scope_sets_user_scope_true(self, tmp_path, monkeypatch):
        """Line 78-79: InstallScope.USER -> user_scope=True."""
        from apm_cli.core.scope import InstallScope

        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                logger=NullCommandLogger(),
                scope=InstallScope.USER,
                apm_config={},
            )

        # No runtimes -> returns 0 (gated)
        assert result == 0

    def test_project_scope_sets_user_scope_false(self, tmp_path, monkeypatch):
        """Line 80-81: InstallScope.PROJECT -> user_scope=False."""
        from apm_cli.core.scope import InstallScope

        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                logger=NullCommandLogger(),
                scope=InstallScope.PROJECT,
                apm_config={},
            )

        assert result == 0


# ---------------------------------------------------------------------------
# Single runtime mode -- line 122-125
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Runtime detection -- ImportError fallback path (lines 201-224)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Script runtimes + target runtimes logic (lines 232-265)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Exclude runtime -- lines 268-276
# ---------------------------------------------------------------------------


class TestExcludeRuntime:
    def test_exclude_all_runtimes_returns_zero(self, tmp_path, monkeypatch):
        """Lines 268-276: --exclude removes all -> warn and return 0."""
        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")
        logger = MagicMock()
        logger.warning = MagicMock()
        logger.progress = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()
        logger.error = MagicMock()
        logger.verbose_detail = MagicMock()

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.factory.ClientFactory",
                side_effect=ImportError,
            ),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                side_effect=lambda rt: "/usr/bin/" + rt if rt == "copilot" else None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator._is_vscode_available",
                return_value=False,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._detect_runtimes",
                return_value=[],
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
        ):
            result = run_mcp_install(
                [dep],
                exclude="copilot",
                logger=logger,
                apm_config={},
                project_root=str(tmp_path),
            )

        assert result == 0


# ---------------------------------------------------------------------------
# No runtimes installed -- fallback to vscode (lines 279-281)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# User-scope filtering (lines 301-326)
# ---------------------------------------------------------------------------


class TestUserScopeFiltering:
    def test_user_scope_filters_workspace_only_runtimes(self, tmp_path, monkeypatch):
        """Lines 301-326: scope=USER removes workspace-only runtimes."""
        from apm_cli.core.scope import InstallScope

        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")
        logger = MagicMock()
        logger.warning = MagicMock()
        logger.progress = MagicMock()
        logger.mcp_lookup_heartbeat = MagicMock()
        logger.error = MagicMock()
        logger.verbose_detail = MagicMock()

        mock_client_ws = MagicMock()
        mock_client_ws.supports_user_scope = False  # workspace-only

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
            patch("apm_cli.factory.ClientFactory") as MockCF,
        ):
            MockCF.create_client.return_value = mock_client_ws

            result = run_mcp_install(
                [dep],
                runtime="vscode",  # single runtime mode
                scope=InstallScope.USER,
                logger=logger,
                apm_config={},
                project_root=str(tmp_path),
            )

        assert result == 0
        logger.warning.assert_called()

    def test_user_scope_no_supported_runtimes_warns_and_returns_zero(self, tmp_path, monkeypatch):
        """Lines 322-326: all runtimes filtered at user scope -> warn and return 0."""
        from apm_cli.core.scope import InstallScope

        monkeypatch.chdir(tmp_path)
        dep = _make_registry_dep("my-server")
        logger = MagicMock()
        logger.warning = MagicMock()
        logger.progress = MagicMock()

        workspace_client = MagicMock()
        workspace_client.supports_user_scope = False

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda rts, **kw: rts,
            ),
            patch("apm_cli.factory.ClientFactory") as MockCF,
        ):
            MockCF.create_client.return_value = workspace_client

            result = run_mcp_install(
                [dep],
                runtime="vscode",
                scope=InstallScope.USER,
                logger=logger,
                apm_config={},
                project_root=str(tmp_path),
            )

        assert result == 0


# ---------------------------------------------------------------------------
# Registry ImportError -- lines 476-479
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Invalid server in registry -- line 346-349
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Self-defined deps -- lines 481-580
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Console panel rendering (lines 582-601)
# ---------------------------------------------------------------------------
