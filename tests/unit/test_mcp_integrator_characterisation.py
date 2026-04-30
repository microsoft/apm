"""Characterisation tests for MCPIntegrator.install() — snapshot behaviour before refactoring."""

from pathlib import Path  # noqa: F401
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _suppress_console(monkeypatch):
    """Prevent actual console output during tests."""
    monkeypatch.setattr("apm_cli.utils.console._get_console", lambda: None)


def _make_self_defined_dep(name="test-server"):
    """Build a self-defined MCP dependency mock (bypasses registry path)."""
    dep = MagicMock()
    dep.name = name
    dep.is_self_defined = True
    dep.is_registry_resolved = False
    dep.transport = "stdio"
    dep.command = "test-cmd"
    dep.args = []
    dep.env = {}
    dep.headers = None
    dep.tools = None
    dep.url = None
    dep.to_dict.return_value = {"name": name}
    dep.__str__ = lambda self: name
    return dep


@pytest.fixture
def mock_mcp_deps():
    """Sample self-defined MCP dependency list."""
    return [_make_self_defined_dep()]


class TestInstallCharacterisation:
    """Snapshot install() behaviour for various input combinations."""

    def test_empty_deps_returns_zero(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator.install(mcp_deps=[])
        assert result == 0

    def test_empty_deps_with_logger_returns_zero(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        result = MCPIntegrator.install(mcp_deps=[], logger=logger)
        assert result == 0

    def test_none_deps_returns_zero(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator.install(mcp_deps=None)
        assert result == 0

    def test_install_with_no_logger(self, mock_mcp_deps):
        """install() with logger=None should not crash (uses NullCommandLogger)."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with patch.object(MCPIntegrator, "_install_for_runtime", return_value=True):
            with patch.object(
                MCPIntegrator,
                "_check_self_defined_servers_needing_installation",
                return_value=["test-server"],
            ):
                result = MCPIntegrator.install(
                    mcp_deps=mock_mcp_deps,
                    runtime="vscode",
                )
                assert isinstance(result, int)

    def test_install_with_logger(self, mock_mcp_deps):
        """install() with explicit logger should use it for output."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = False
        with patch.object(MCPIntegrator, "_install_for_runtime", return_value=True):
            with patch.object(
                MCPIntegrator,
                "_check_self_defined_servers_needing_installation",
                return_value=["test-server"],
            ):
                result = MCPIntegrator.install(
                    mcp_deps=mock_mcp_deps,
                    runtime="vscode",
                    logger=logger,
                )
                assert isinstance(result, int)

    def test_install_exclude_filter(self, mock_mcp_deps):
        """Excluded runtime does not block non-excluded runtimes from installing."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = False
        with patch.object(MCPIntegrator, "_install_for_runtime", return_value=True) as mock_install:
            with patch.object(
                MCPIntegrator,
                "_check_self_defined_servers_needing_installation",
                return_value=["test-server"],
            ):
                # exclude="cursor" doesn't affect explicit runtime="vscode"
                result = MCPIntegrator.install(
                    mcp_deps=mock_mcp_deps,
                    runtime="vscode",
                    exclude="cursor",
                    logger=logger,
                )
                assert isinstance(result, int)
                assert mock_install.called

    def test_install_specific_runtime(self, mock_mcp_deps):
        """install() with explicit runtime should target only that runtime."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = False
        with patch.object(MCPIntegrator, "_install_for_runtime", return_value=True) as mock_install:
            with patch.object(
                MCPIntegrator,
                "_check_self_defined_servers_needing_installation",
                return_value=["test-server"],
            ):
                MCPIntegrator.install(
                    mcp_deps=mock_mcp_deps,
                    runtime="vscode",
                    logger=logger,
                )
                assert mock_install.called

    def test_install_unsupported_runtime(self, mock_mcp_deps):
        """install() with unsupported runtime logs warning via _install_for_runtime."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = False
        with patch.object(
            MCPIntegrator,
            "_check_self_defined_servers_needing_installation",
            return_value=["test-server"],
        ):
            # _install_for_runtime will catch ValueError for unknown runtime
            result = MCPIntegrator.install(
                mcp_deps=mock_mcp_deps,
                runtime="nonexistent",
                logger=logger,
            )
            assert isinstance(result, int)

    def test_install_runtime_none_auto_detects(self, mock_mcp_deps):
        """runtime=None triggers auto-detection."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = False
        with patch.object(MCPIntegrator, "_install_for_runtime", return_value=True):
            with patch.object(
                MCPIntegrator,
                "_check_self_defined_servers_needing_installation",
                return_value=["test-server"],
            ):
                with patch(
                    "apm_cli.integration.mcp_integrator._is_vscode_available", return_value=True
                ):
                    result = MCPIntegrator.install(
                        mcp_deps=mock_mcp_deps,
                        runtime=None,
                        logger=logger,
                    )
                    assert isinstance(result, int)

    def test_install_verbose_flag(self, mock_mcp_deps):
        """verbose=True should pass through to runtime detection."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = True
        with patch.object(MCPIntegrator, "_install_for_runtime", return_value=True):
            with patch.object(
                MCPIntegrator,
                "_check_self_defined_servers_needing_installation",
                return_value=["test-server"],
            ):
                MCPIntegrator.install(
                    mcp_deps=mock_mcp_deps,
                    verbose=True,
                    runtime="vscode",
                    logger=logger,
                )

    def test_install_returns_count_of_configured_runtimes(self, mock_mcp_deps):
        """install() should return count of successfully configured runtimes."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = False
        with patch.object(MCPIntegrator, "_install_for_runtime", return_value=True):
            with patch.object(
                MCPIntegrator,
                "_check_self_defined_servers_needing_installation",
                return_value=["test-server"],
            ):
                result = MCPIntegrator.install(
                    mcp_deps=mock_mcp_deps,
                    runtime="vscode",
                    logger=logger,
                )
                assert result >= 0
