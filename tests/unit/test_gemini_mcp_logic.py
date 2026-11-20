"""Tests for the Gemini CLI MCP client adapter (pure logic cases)."""

import pytest

from apm_cli.adapters.client.gemini import GeminiClientAdapter
from apm_cli.factory import ClientFactory

pytestmark = pytest.mark.unit


class TestGeminiClientFactory:
    """Verify GeminiClientAdapter is registered in ClientFactory."""

    def test_factory_creates_gemini_adapter(self):
        adapter = ClientFactory.create_client("gemini")
        assert isinstance(adapter, GeminiClientAdapter)
