"""Characterisation tests for MCPIntegrator.remove_stale() (pure logic cases)."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _suppress_console(monkeypatch):
    monkeypatch.setattr("apm_cli.utils.console._get_console", lambda: None)


class TestRemoveStaleCharacterisation:
    def test_remove_stale_no_logger(self):
        """remove_stale() with logger=None should not crash."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator.remove_stale(stale_names=set())
        assert result is None

    def test_remove_stale_with_logger(self):
        """remove_stale() with logger should use it."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = False
        result = MCPIntegrator.remove_stale(stale_names=set(), logger=logger)
        assert result is None

    def test_remove_stale_with_runtime(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator.remove_stale(
            stale_names=set(),
            runtime="vscode",
        )
        assert result is None

    def test_remove_stale_with_scope(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = False
        result = MCPIntegrator.remove_stale(
            stale_names=set(),
            logger=logger,
            scope=None,
        )
        assert result is None

    def test_remove_stale_verbose(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = True
        result = MCPIntegrator.remove_stale(
            stale_names=set(),
            logger=logger,
        )
        assert result is None

    def test_remove_stale_with_exclude(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = False
        result = MCPIntegrator.remove_stale(
            stale_names=set(),
            exclude="vscode",
            logger=logger,
        )
        assert result is None


class TestRemoveStaleIntelliJ:
    """Fixture-backed coverage for the JetBrains (intellij) stale-cleanup block."""

    def test_remove_stale_intellij_resolves_output_path_once(self):
        """Multiple removal messages reuse one validated config path."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        client = MagicMock()
        client.remove_managed_servers.return_value = {"first", "second"}
        client.get_config_path.return_value = "/home/user/.config/github-copilot/intellij/mcp.json"
        with (
            patch("apm_cli.factory.ClientFactory.create_client", return_value=client),
            patch("apm_cli.integration.mcp_integrator._rich_success"),
        ):
            MCPIntegrator.remove_stale(
                stale_names={"first", "second"},
                runtime="intellij",
                logger=MagicMock(),
            )

        client.get_config_path.assert_called_once_with()
