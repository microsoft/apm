"""Policy mcp.transport.allow judges the effective canonical MCP transport (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apm_cli.integration import mcp_transport_resolution as resolution
from apm_cli.integration.mcp_transport_resolution import write_transports
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.policy.schema import ApmPolicy, McpPolicy, McpTransportPolicy

pytestmark = pytest.mark.component

SSE_SERVER = {
    "name": "io.example/sse-server",
    "remotes": [{"type": "sse", "url": "https://sse.example/mcp"}],
}
HTTP_SERVER = {
    "name": "io.example/http-server",
    "remotes": [{"type": "streamable-http", "url": "https://http.example/mcp"}],
}


class _FakeClient:
    def __init__(self, registry_url, state):
        self.registry_url = registry_url or "https://registry.example"
        self._state = state

    def find_server_by_reference(self, name):
        self._state["calls"].append((self.registry_url, name))
        if self._state["unreachable"]:
            raise ConnectionError("boom")
        return self._state["servers"].get(name)


@pytest.fixture
def fake_registry():
    """Patch the registry operations with an in-memory registry."""
    state = {"servers": {}, "calls": [], "unreachable": False}

    def _client(registry_url):
        return _FakeClient(registry_url, state)

    resolution.clear_registry_info_memo()
    with patch.object(resolution, "_registry_client", _client):
        yield state
    resolution.clear_registry_info_memo()


def _policy(*allow: str) -> ApmPolicy:
    return ApmPolicy(mcp=McpPolicy(transport=McpTransportPolicy(allow=allow)))


def _transport_check(result):
    return next(check for check in result.checks if check.name == "mcp-transport")


class TestPolicyInstallParity:
    @pytest.mark.parametrize("dep_registry", [None, "https://corp.example/"])
    def test_install_writes_the_snapshot_policy_judged(
        self, fake_registry, tmp_path, monkeypatch, dep_registry
    ):
        """A registry that answers differently later cannot change what is written."""
        from unittest.mock import MagicMock

        from apm_cli.integration.mcp_integrator_install import (
            _registry_group_key,
            run_mcp_install,
        )
        from apm_cli.registry.client import SimpleRegistryClient

        monkeypatch.setenv("MCP_REGISTRY_URL", "https://default.example/")
        name = SSE_SERVER["name"]
        fake_registry["servers"][name] = SSE_SERVER
        dep = (
            MCPDependency.from_string(name)
            if dep_registry is None
            else MCPDependency.from_dict({"name": name, "registry": dep_registry})
        )
        real_url = SimpleRegistryClient(_registry_group_key(dep)).registry_url
        monkeypatch.setattr(
            resolution, "_registry_client", lambda url: _FakeClient(real_url, fake_registry)
        )
        assert resolution.resolve_effective_mcp_transports([dep]) == {name: frozenset({"sse"})}

        operations = MagicMock()
        operations.registry_client.registry_url = real_url
        operations.validate_servers_exist.side_effect = lambda names, **kwargs: (names, [])
        operations.check_servers_needing_installation.return_value = [name]
        operations.batch_fetch_server_info.return_value = {name: HTTP_SERVER}
        operations.collect_environment_variables.return_value = {}
        operations.collect_runtime_variables.return_value = {}
        written: dict = {}

        def _install(_runtime, _deps, _env, cache, _runtime_vars, **_kwargs):
            written.update(cache)
            return True

        with (
            patch("apm_cli.registry.operations.MCPServerOperations", return_value=operations),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                side_effect=lambda runtimes, **_kwargs: runtimes,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime",
                side_effect=_install,
            ),
        ):
            run_mcp_install(
                mcp_deps=[dep], runtime="copilot", project_root=tmp_path, logger=MagicMock()
            )

        assert write_transports(written[name]) == frozenset({"sse"})
        assert written[name]["remotes"][0]["url"] == SSE_SERVER["remotes"][0]["url"]
        operations.batch_fetch_server_info.assert_not_called()

    def test_audit_checks_dev_mcp_servers_like_install(self, fake_registry, tmp_path):
        from apm_cli.policy.policy_checks import run_policy_checks

        fake_registry["servers"][SSE_SERVER["name"]] = SSE_SERVER
        (tmp_path / "apm.yml").write_text(
            f"name: p\nversion: 1.0.0\ndevDependencies:\n  mcp:\n    - {SSE_SERVER['name']}\n",
            encoding="utf-8",
        )
        result = run_policy_checks(tmp_path, _policy("stdio", "streamable-http"), fail_fast=False)
        assert not _transport_check(result).passed
