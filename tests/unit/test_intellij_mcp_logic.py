"""Tests for the JetBrains Copilot MCP client adapter (pure logic cases)."""

import pytest

from apm_cli.adapters.client.intellij import (
    IntelliJClientAdapter,
)
from apm_cli.factory import ClientFactory

pytestmark = pytest.mark.unit


class TestIntelliJClientFactory:
    """Verify IntelliJClientAdapter is registered in ClientFactory."""

    def test_factory_creates_intellij_adapter(self):
        adapter = ClientFactory.create_client("intellij")
        assert isinstance(adapter, IntelliJClientAdapter)

    def test_supports_user_scope_is_true(self):
        adapter = ClientFactory.create_client("intellij")
        assert adapter.supports_user_scope is True

    def test_mcp_servers_key_is_servers(self):
        adapter = ClientFactory.create_client("intellij")
        assert adapter.mcp_servers_key == "servers"
