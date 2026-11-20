"""Unit tests for apm_cli.integration.mcp_integrator_install (pure logic cases)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


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


# ---------------------------------------------------------------------------
# run_mcp_install -- single runtime (explicit --runtime)
# ---------------------------------------------------------------------------


def test_direct_registry_prevalidation_fails_closed():
    from apm_cli.integration.mcp_integrator_install import prevalidate_registry_dependencies

    dep = MagicMock()
    dep.name = "my-server"
    operations = MagicMock()

    def validate(_names, **kwargs):
        kwargs["server_info_cache"]["my-server"] = {"packages": []}
        return ["my-server"], []

    operations.validate_servers_exist.side_effect = validate

    with patch(
        "apm_cli.registry.operations.MCPServerOperations",
        return_value=operations,
    ):
        result = prevalidate_registry_dependencies(
            [dep],
            registry_url=None,
            verbose=False,
            logger=MagicMock(),
        )

    operations.validate_servers_exist.assert_called_once()
    assert operations.validate_servers_exist.call_args.kwargs["fail_closed"] is True
    assert result == {"my-server": {"packages": []}}


# ---------------------------------------------------------------------------
# run_mcp_install -- auto-detection with ImportError fallback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_mcp_install -- cursor/opencode/gemini/windsurf opt-in directory detection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_mcp_install -- no runtimes installed warnings
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# run_mcp_install -- scripts detection (verbose mode)
# ---------------------------------------------------------------------------
