"""Regression tests for optional MCP registry inputs."""

from __future__ import annotations

import json
from typing import Any

from apm_cli.adapters.client.base import MCPClientAdapter
from apm_cli.adapters.client.copilot import CopilotClientAdapter
from apm_cli.adapters.client.vscode import VSCodeClientAdapter
from apm_cli.registry.operations import MCPServerOperations


def _context7_server_info() -> dict[str, Any]:
    """Return registry metadata shaped like context7's optional token env."""
    return {
        "id": "context7-id",
        "name": "context7",
        "packages": [
            {
                "name": "@upstash/context7-mcp",
                "registry_name": "npm",
                "runtime_hint": "npx",
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


def test_collect_environment_variables_does_not_prompt_optional_without_value(
    monkeypatch,
) -> None:
    """Optional registry env vars are not force-prompted as required vars."""
    monkeypatch.delenv("CONTEXT7_API_KEY", raising=False)
    monkeypatch.delenv("REQUIRED_TOKEN", raising=False)
    operations = MCPServerOperations()
    prompted: dict[str, dict[str, Any]] = {}

    def fake_prompt(required_vars: dict[str, dict[str, Any]]) -> dict[str, str]:
        prompted.update(required_vars)
        return {"REQUIRED_TOKEN": "required-value"}

    monkeypatch.setattr(operations, "_prompt_for_environment_variables", fake_prompt)

    result = operations.collect_environment_variables(
        ["context7"],
        {
            "context7": {
                "name": "context7",
                "packages": [
                    {
                        "environment_variables": [
                            {
                                "name": "CONTEXT7_API_KEY",
                                "description": "Optional Context7 authorization token",
                                "required": False,
                            },
                            {
                                "name": "OPTIONAL_INPUT",
                                "description": "Optional input metadata variant",
                                "is_required": False,
                            },
                            {
                                "name": "REQUIRED_TOKEN",
                                "description": "Required token",
                                "required": True,
                            },
                        ]
                    }
                ],
            }
        },
    )

    assert prompted == {"REQUIRED_TOKEN": {"description": "Required token", "required": True}}
    assert result == {"REQUIRED_TOKEN": "required-value"}


def test_base_env_resolver_omits_optional_env_when_no_value_provided(monkeypatch) -> None:
    """Legacy adapters must not write empty optional env entries."""
    monkeypatch.delenv("OPTIONAL_TOKEN", raising=False)

    result = MCPClientAdapter._resolve_env_vars_with_prompting(
        [{"name": "OPTIONAL_TOKEN", "is_required": False}], {}, {}
    )

    assert result == {}


def test_copilot_omits_optional_env_placeholder_when_no_value_provided() -> None:
    """Copilot config must not create placeholders for unset optional env vars."""
    adapter = CopilotClientAdapter()

    config = adapter._format_server_config(
        _context7_server_info(), env_overrides={}, runtime_vars={}
    )

    assert "env" not in config


def test_vscode_omits_optional_env_inputs_when_no_value_provided(tmp_path) -> None:
    """VS Code config must not create prompt inputs for unset optional env vars."""
    adapter = VSCodeClientAdapter(project_root=tmp_path)

    assert adapter.configure_mcp_server(
        "context7",
        server_name="context7",
        server_info_cache={"context7": _context7_server_info()},
        env_overrides={},
    )

    config = json.loads((tmp_path / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
    server = config["servers"]["context7"]

    assert "env" not in server
    assert config.get("inputs", []) == []


def test_vscode_reinstall_preserves_user_edited_optional_env(tmp_path) -> None:
    """Reinstall must not overwrite an existing user-edited optional env field."""
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
    adapter = VSCodeClientAdapter(project_root=tmp_path)

    assert adapter.configure_mcp_server(
        "context7",
        server_name="context7",
        server_info_cache={"context7": _context7_server_info()},
        env_overrides={},
    )

    config = json.loads(mcp_json.read_text(encoding="utf-8"))
    server = config["servers"]["context7"]

    assert server["env"] == {"CONTEXT7_API_KEY": "${env:CONTEXT7_API_KEY}"}
    assert config.get("inputs", []) == []
