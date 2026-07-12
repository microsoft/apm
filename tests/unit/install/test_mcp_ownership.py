"""Tests for conservative legacy MCP ownership adoption."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apm_cli.install.mcp.ownership import adopt_legacy_mcp_target_servers


@pytest.mark.parametrize(
    ("native_config", "expected"),
    [
        ({"command": "echo", "args": ["managed"]}, {"codex": {"managed"}}),
        ({"command": "user-edited", "args": ["managed"]}, {}),
    ],
)
def test_legacy_adoption_requires_exact_native_baseline(
    tmp_path,
    native_config,
    expected,
) -> None:
    """User-edited native entries are never guessed to be APM-owned."""
    client = SimpleNamespace(
        mcp_servers_key="mcp_servers",
        get_current_config=lambda: {"mcp_servers": {"managed": native_config}},
        render_server_config=lambda _info: {
            "command": "echo",
            "args": ["managed"],
        },
    )
    stored = {
        "managed": {
            "name": "managed",
            "registry": False,
            "transport": "stdio",
            "command": "echo",
            "args": ["managed"],
        }
    }
    with (
        patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["codex"]),
        patch("apm_cli.factory.ClientFactory.create_client", return_value=client),
    ):
        adopted = adopt_legacy_mcp_target_servers(
            server_names={"managed"},
            stored_configs=stored,
            project_root=tmp_path,
            user_scope=False,
        )

    assert adopted == expected
