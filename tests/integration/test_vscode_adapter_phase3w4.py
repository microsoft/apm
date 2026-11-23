"""Integration tests for apm_cli.adapters.client.vscode -- phase3w4.

Covers missing lines/branches in VSCodeClientAdapter.
All tests are hermetic: filesystem uses tmp_path, registry calls are mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.adapters.client.vscode import VSCodeClientAdapter
from apm_cli.integration.mcp_integrator_install import run_mcp_install

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(tmp_path: Path) -> VSCodeClientAdapter:
    return VSCodeClientAdapter(project_root=tmp_path)


def _context7_optional_env_server_info() -> dict:
    """Return registry metadata with an unset optional token env var."""
    return {
        "name": "context7",
        "packages": [
            {
                "runtime_hint": "npx",
                "name": "@upstash/context7-mcp",
                "registry_name": "npm",
                "runtime_arguments": [],
                "package_arguments": [],
                "environment_variables": [
                    {
                        "name": "CONTEXT7_API_KEY",
                        "description": "Optional Context7 authorization token",
                        "required": False,
                    }
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# get_config_path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# update_config
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_current_config
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# configure_mcp_server
# ---------------------------------------------------------------------------


class TestConfigureMcpServer:
    def test_optional_env_vars_omitted_from_copilot_config_on_install(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CONTEXT7_API_KEY", raising=False)
        server_info = _context7_optional_env_server_info()
        logger = MagicMock()

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.registry.operations.MCPServerOperations.validate_servers_exist",
                return_value=(["context7"], []),
            ),
            patch(
                "apm_cli.registry.operations.MCPServerOperations.check_servers_needing_installation",
                return_value=["context7"],
            ),
            patch(
                "apm_cli.registry.operations.MCPServerOperations.batch_fetch_server_info",
                return_value={"context7": server_info},
            ),
        ):
            configured = run_mcp_install(
                ["context7"],
                runtime="copilot",
                explicit_target="copilot",
                project_root=tmp_path,
                logger=logger,
            )

        # Project MCP servers land in the workspace-root .mcp.json, never in
        # the retired .vscode/mcp.json (#3866).
        config = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
        server = config["mcpServers"]["context7"]

        assert configured == 1
        assert "CONTEXT7_API_KEY" not in server.get("env", {})
        assert "inputs" not in config
        assert not (tmp_path / ".vscode" / "mcp.json").exists()

    def test_user_vscode_mcp_json_untouched_by_copilot_install(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CONTEXT7_API_KEY", raising=False)
        vscode_dir = tmp_path / ".vscode"
        vscode_dir.mkdir()
        mcp_json = vscode_dir / "mcp.json"
        mcp_json.write_text(
            json.dumps(
                {
                    "servers": {
                        "context7": {
                            "type": "stdio",
                            "command": "npx",
                            "args": ["-y", "@upstash/context7-mcp"],
                            "env": {"CONTEXT7_API_KEY": "${env:CONTEXT7_API_KEY}"},
                        }
                    },
                    "inputs": [],
                }
            ),
            encoding="utf-8",
        )
        before = mcp_json.read_bytes()
        server_info = _context7_optional_env_server_info()
        logger = MagicMock()

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.registry.operations.MCPServerOperations.validate_servers_exist",
                return_value=(["context7"], []),
            ),
            patch(
                "apm_cli.registry.operations.MCPServerOperations.check_servers_needing_installation",
                return_value=["context7"],
            ),
            patch(
                "apm_cli.registry.operations.MCPServerOperations.batch_fetch_server_info",
                return_value={"context7": server_info},
            ),
            patch(
                "apm_cli.core.conflict_detector.MCPConflictDetector.check_server_exists",
                return_value=False,
            ),
        ):
            configured = run_mcp_install(
                ["context7"],
                runtime="copilot",
                explicit_target="copilot",
                project_root=tmp_path,
                logger=logger,
            )

        # A user-authored entry in the retired .vscode/mcp.json is never
        # touched; the install writes Copilot's .mcp.json instead (#3866).
        assert configured == 1
        assert mcp_json.read_bytes() == before
        server = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
            "context7"
        ]
        assert "CONTEXT7_API_KEY" not in server.get("env", {})


# ---------------------------------------------------------------------------
# _format_server_config
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _translate_env_vars_for_vscode
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _warn_on_legacy_angle_vars
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _extract_input_variables
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _extract_package_args
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _select_remote_with_url
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _select_best_package
# ---------------------------------------------------------------------------
