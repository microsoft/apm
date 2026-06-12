"""Integration: MCPIntegrator.install reaches run_mcp_install without delegate mocks.

``MCPIntegrator.install`` is a thin wrapper over ``run_mcp_install``; many
integration tests patch the method at the boundary and never execute the
extracted body. This module pins at least one path through the real delegate.
"""

from __future__ import annotations

import pytest

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.integration.mcp_integrator import MCPIntegrator

pytestmark = pytest.mark.integration


def test_install_delegate_empty_deps_executes_extracted_module(tmp_path, monkeypatch) -> None:
    """``install([])`` hits ``run_mcp_install`` early-return (no MCP deps)."""
    monkeypatch.chdir(tmp_path)
    assert MCPIntegrator.install([], logger=NullCommandLogger()) == 0
