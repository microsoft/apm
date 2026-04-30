"""Characterisation tests for MCPIntegrator.remove_stale()."""

from pathlib import Path  # noqa: F401
from unittest.mock import MagicMock, patch  # noqa: F401

import pytest


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

    def test_remove_stale_empty_names(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator.remove_stale(stale_names=set())
        assert result is None

    def test_remove_stale_with_runtime(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator.remove_stale(
            stale_names=set(),
            runtime="vscode",
        )
        assert result is None

    def test_remove_stale_returns_none(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        logger.verbose = False
        result = MCPIntegrator.remove_stale(
            stale_names=set(),
            logger=logger,
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
