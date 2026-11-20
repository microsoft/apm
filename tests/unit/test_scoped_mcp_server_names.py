"""Regression tests for scoped MCP server config keys."""

from pathlib import Path

import pytest

from apm_cli.adapters.client.claude import ClaudeClientAdapter
from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.adapters.client.copilot import CopilotClientAdapter

pytestmark = pytest.mark.component


@pytest.fixture
def raw_stdio_servers() -> dict[str, dict]:
    """Return self-defined stdio server fixtures keyed by declared name."""
    return {
        "@playwright/mcp": {"_raw_stdio": {"command": "npx", "args": ["-y", "@playwright/mcp"]}},
        "@other/mcp": {"_raw_stdio": {"command": "npx", "args": ["-y", "@other/mcp"]}},
    }


@pytest.fixture
def self_defined_reverse_dns_servers() -> dict[str, dict]:
    """Return self-defined server fixtures with slashes in declared name."""
    return {
        "com.azure.dev/mcp-remote-azure-devops": {
            "name": "com.azure.dev/mcp-remote-azure-devops",
            "is_self_defined": True,
            "remotes": [{"transport_type": "http", "url": "https://mcp.dev.azure.com/my-org"}],
        },
        "io.github.owner/mcp-remote-azure-devops": {
            "name": "io.github.owner/mcp-remote-azure-devops",
            "is_self_defined": True,
            "remotes": [{"transport_type": "http", "url": "https://mcp.dev.github.com/my-org"}],
        },
    }


@pytest.mark.parametrize(
    "adapter_cls",
    [ClaudeClientAdapter, CodexClientAdapter, CopilotClientAdapter],
)
def test_scoped_server_name_is_preserved_as_config_key(adapter_cls, raw_stdio_servers):
    """Adapters must not truncate npm scoped package names to the basename."""
    captured_keys = []
    adapter = adapter_cls(project_root=Path.cwd(), user_scope=True)
    adapter.update_config = lambda updates, **_kwargs: captured_keys.extend(updates.keys()) or True

    result = adapter.configure_mcp_server("@playwright/mcp", server_info_cache=raw_stdio_servers)

    assert result is True
    assert captured_keys == ["@playwright/mcp"]


@pytest.mark.parametrize(
    "adapter_cls",
    [ClaudeClientAdapter, CodexClientAdapter, CopilotClientAdapter],
)
def test_two_scoped_server_names_do_not_collide(adapter_cls, raw_stdio_servers):
    """Distinct scoped packages with the same basename must keep distinct keys."""
    captured_keys = []
    adapter = adapter_cls(project_root=Path.cwd(), user_scope=True)
    adapter.update_config = lambda updates, **_kwargs: captured_keys.extend(updates.keys()) or True

    for server_ref in raw_stdio_servers:
        result = adapter.configure_mcp_server(server_ref, server_info_cache=raw_stdio_servers)
        assert result is True

    assert captured_keys == ["@playwright/mcp", "@other/mcp"]


@pytest.mark.parametrize(
    "adapter_cls",
    [ClaudeClientAdapter, CodexClientAdapter, CopilotClientAdapter],
)
def test_self_defined_reverse_dns_name_is_preserved_as_config_key(
    adapter_cls, self_defined_reverse_dns_servers
):
    """Adapters must preserve self-defined server names containing slashes."""
    captured_keys = []
    adapter = adapter_cls(project_root=Path.cwd(), user_scope=True)
    adapter.update_config = lambda updates, **_kwargs: captured_keys.extend(updates.keys()) or True

    result = adapter.configure_mcp_server(
        "com.azure.dev/mcp-remote-azure-devops",
        server_info_cache=self_defined_reverse_dns_servers,
    )

    assert result is True
    assert captured_keys == ["com.azure.dev/mcp-remote-azure-devops"]


@pytest.mark.parametrize(
    "adapter_cls",
    [ClaudeClientAdapter, CodexClientAdapter, CopilotClientAdapter],
)
def test_two_self_defined_names_with_same_final_segment_do_not_collide(
    adapter_cls, self_defined_reverse_dns_servers
):
    """Distinct self-defined servers with identical final segments must keep distinct keys."""
    captured_keys = []
    adapter = adapter_cls(project_root=Path.cwd(), user_scope=True)
    adapter.update_config = lambda updates, **_kwargs: captured_keys.extend(updates.keys()) or True

    for server_ref in self_defined_reverse_dns_servers:
        result = adapter.configure_mcp_server(
            server_ref, server_info_cache=self_defined_reverse_dns_servers
        )
        assert result is True

    assert captured_keys == [
        "com.azure.dev/mcp-remote-azure-devops",
        "io.github.owner/mcp-remote-azure-devops",
    ]


def test_static_guard_safe_installer_centralizes_naming_in_adapter():
    """SafeMCPInstaller must not duplicate self-defined config key resolution."""
    import ast

    installer_path = (
        Path(__file__).resolve().parents[2] / "src" / "apm_cli" / "core" / "safe_installer.py"
    )
    tree = ast.parse(installer_path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value in ("is_self_defined", "_raw_stdio"):
            pytest.fail(
                f"safe_installer.py references {node.value!r} at line {node.lineno}; "
                "MCP server config key determination must be centralized in "
                "MCPClientAdapter.determine_config_key."
            )
