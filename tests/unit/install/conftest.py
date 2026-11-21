"""Local conftest for install.sh safety tests.

The root tests/conftest.py autouses fixtures that import apm_cli
(primitive-coverage check, discovery cache reset). Those are valid for
the Python CLI test suite but irrelevant to a shell-script regression
test -- and forcing apm_cli on PATH here would couple the safety tests
to a Python dependency chain they don't exercise.

We override the two autouse fixtures with no-ops so this test file can
run in any environment that has bash + pytest, no Python build needed.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True, scope="session")
def _validate_primitive_coverage():
    """No-op: shell-script safety tests don't exercise the dispatch table."""


@pytest.fixture(autouse=True)
def _isolate_discovery_state():
    """No-op: shell-script safety tests don't touch discovery state."""
    yield


class _StdioMcpRegistryClient:
    registry_url = "https://registry.example"

    def __init__(self, servers: dict):
        self._servers = servers

    def find_server_by_reference(self, name):
        return self._servers.get(
            name, {"name": name, "packages": [{"registry_name": "npm", "name": name}]}
        )


@pytest.fixture
def stdio_mcp_registry(monkeypatch):
    """Serve every registry MCP server as a stdio package unless overridden.

    Org policy ``mcp.transport.allow`` resolves registry servers; tests add
    entries to the returned dict to serve other shapes.
    """
    from apm_cli.integration import mcp_transport_resolution

    servers: dict = {}
    mcp_transport_resolution.clear_registry_info_memo()
    monkeypatch.setattr(
        mcp_transport_resolution,
        "_registry_client",
        lambda registry_url: _StdioMcpRegistryClient(servers),
    )
    yield servers
    mcp_transport_resolution.clear_registry_info_memo()
