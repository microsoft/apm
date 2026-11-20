"""Unit tests for CursorClientAdapter and its MCP integrator wiring (pure logic cases)."""

import unittest

import pytest

from apm_cli.adapters.client.cursor import CursorClientAdapter
from apm_cli.factory import ClientFactory

pytestmark = pytest.mark.unit


class TestCursorClientFactory(unittest.TestCase):
    """Factory registration for the cursor runtime."""

    def test_create_cursor_client(self):
        client = ClientFactory.create_client("cursor")
        self.assertIsInstance(client, CursorClientAdapter)

    def test_create_cursor_client_case_insensitive(self):
        client = ClientFactory.create_client("Cursor")
        self.assertIsInstance(client, CursorClientAdapter)


if __name__ == "__main__":
    unittest.main()
