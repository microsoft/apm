"""Policy mcp.transport.allow judges the effective canonical MCP transport.

Hermetic: the MCP registry is replaced by an in-memory fake.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apm_cli.integration import mcp_transport_resolution as resolution
from apm_cli.mcp_transport import (
    UnresolvedTransport,
    canonical_transport_name,
    server_write_transports,
)
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.policy.policy_checks import run_dependency_policy_checks
from apm_cli.policy.schema import ApmPolicy, McpPolicy, McpTransportPolicy

SSE_SERVER = {
    "name": "io.example/sse-server",
    "remotes": [{"type": "sse", "url": "https://sse.example/mcp"}],
}
HTTP_SERVER = {
    "name": "io.example/http-server",
    "remotes": [{"type": "streamable-http", "url": "https://http.example/mcp"}],
}
DUAL_SERVER = {
    "name": "io.example/dual-server",
    "packages": [{"registry_name": "npm", "name": "dual-server"}],
    "remotes": [{"type": "sse", "url": "https://dual.example/sse"}],
}


class _FakeClient:
    def __init__(self, registry_url, servers, calls, unreachable=False):
        self.registry_url = registry_url or "https://registry.example"
        self._servers = servers
        self._calls = calls
        self._unreachable = unreachable

    def find_server_by_reference(self, name):
        self._calls.append((self.registry_url, name))
        if self._unreachable:
            raise ConnectionError("boom")
        return self._servers.get(name)


@pytest.fixture
def fake_registry():
    """Patch the registry operations with an in-memory registry."""
    state = {"servers": {}, "calls": [], "unreachable": False}

    def _client(registry_url):
        return _FakeClient(registry_url, state["servers"], state["calls"], state["unreachable"])

    resolution.clear_registry_info_memo()
    with patch.object(resolution, "_registry_client", _client):
        yield state
    resolution.clear_registry_info_memo()


def _policy(*allow: str) -> ApmPolicy:
    return ApmPolicy(mcp=McpPolicy(transport=McpTransportPolicy(allow=allow)))


def _transport_check(result):
    return next(check for check in result.checks if check.name == "mcp-transport")


class TestOwnerProjection:
    def test_http_alias_is_streamable_http(self):
        assert canonical_transport_name("http") == "streamable-http"
        assert canonical_transport_name(" SSE ") == "sse"

    def test_package_and_remote_report_both(self):
        assert server_write_transports(DUAL_SERVER) == frozenset({"stdio", "sse"})

    def test_raw_stdio_is_stdio_only(self):
        info = {"_raw_stdio": {"command": "node"}, "remotes": SSE_SERVER["remotes"]}
        assert server_write_transports(info) == frozenset({"stdio"})


class TestRegistryRemoteTransport:
    def test_registry_sse_remote_blocked(self, fake_registry):
        """Regression (#3764): an undeclared registry SSE remote was never checked."""
        fake_registry["servers"][SSE_SERVER["name"]] = SSE_SERVER
        result = run_dependency_policy_checks(
            [],
            policy=_policy("stdio", "streamable-http"),
            mcp_deps=[MCPDependency.from_string(SSE_SERVER["name"])],
        )
        check = _transport_check(result)
        assert not check.passed
        assert check.details == [
            "io.example/sse-server: transport 'sse' not in allowed [stdio, streamable-http]"
        ]

    def test_registry_streamable_http_remote_passes(self, fake_registry):
        fake_registry["servers"][HTTP_SERVER["name"]] = HTTP_SERVER
        result = run_dependency_policy_checks(
            [],
            policy=_policy("stdio", "streamable-http"),
            mcp_deps=[MCPDependency.from_string(HTTP_SERVER["name"])],
        )
        assert _transport_check(result).passed

    def test_package_and_remote_both_checked(self, fake_registry):
        fake_registry["servers"][DUAL_SERVER["name"]] = DUAL_SERVER
        effective = resolution.resolve_effective_mcp_transports(
            [MCPDependency.from_string(DUAL_SERVER["name"])]
        )
        assert effective == {DUAL_SERVER["name"]: frozenset({"stdio", "sse"})}

    def test_no_lookup_without_transport_policy(self, fake_registry):
        run_dependency_policy_checks(
            [],
            policy=ApmPolicy(),
            mcp_deps=[MCPDependency.from_string(SSE_SERVER["name"])],
        )
        assert fake_registry["calls"] == []

    def test_lookup_is_memoized(self, fake_registry):
        fake_registry["servers"][SSE_SERVER["name"]] = SSE_SERVER
        deps = [MCPDependency.from_string(SSE_SERVER["name"])]
        resolution.resolve_effective_mcp_transports(deps)
        resolution.resolve_effective_mcp_transports(deps)
        assert len(fake_registry["calls"]) == 1

    def test_per_dependency_registry_is_queried(self, fake_registry):
        fake_registry["servers"][SSE_SERVER["name"]] = SSE_SERVER
        dep = MCPDependency.from_dict(
            {"name": SSE_SERVER["name"], "registry": "https://corp.example"}
        )
        resolution.resolve_effective_mcp_transports([dep])
        assert fake_registry["calls"] == [("https://corp.example", SSE_SERVER["name"])]


class TestDeclaredTransport:
    def test_declared_stdio_cannot_mask_remote_only_server(self, fake_registry):
        """A declaration narrows only when the server offers it; install writes the remote."""
        fake_registry["servers"][SSE_SERVER["name"]] = SSE_SERVER
        dep = MCPDependency.from_dict({"name": SSE_SERVER["name"], "transport": "stdio"})
        result = run_dependency_policy_checks([], policy=_policy("stdio"), mcp_deps=[dep])
        assert _transport_check(result).details == [
            "io.example/sse-server: transport 'sse' not in allowed [stdio]"
        ]

    def test_declared_transport_narrows_dual_server(self, fake_registry):
        fake_registry["servers"][DUAL_SERVER["name"]] = DUAL_SERVER
        dep = MCPDependency.from_dict({"name": DUAL_SERVER["name"], "transport": "stdio"})
        effective = resolution.resolve_effective_mcp_transports([dep])
        assert effective == {DUAL_SERVER["name"]: frozenset({"stdio"})}

    @pytest.mark.parametrize("declared", ["http", "streamable-http"])
    def test_declared_http_alias_used_when_registry_cannot_resolve(self, fake_registry, declared):
        dep = MCPDependency.from_dict({"name": "io.example/x", "transport": declared})
        result = run_dependency_policy_checks(
            [], policy=_policy("stdio", "streamable-http"), mcp_deps=[dep]
        )
        assert _transport_check(result).passed

    def test_declared_disallowed_transport_blocked(self, fake_registry):
        dep = MCPDependency.from_dict({"name": "io.example/x", "transport": "sse"})
        result = run_dependency_policy_checks([], policy=_policy("stdio"), mcp_deps=[dep])
        assert not _transport_check(result).passed

    def test_self_defined_resolves_offline(self, fake_registry):
        dep = MCPDependency.from_dict(
            {"name": "local", "registry": False, "transport": "stdio", "command": "node"}
        )
        effective = resolution.resolve_effective_mcp_transports([dep])
        assert effective == {"local": frozenset({"stdio"})}
        assert fake_registry["calls"] == []


class TestUnresolvedTransport:
    def test_not_found_is_actionable_violation(self, fake_registry):
        result = run_dependency_policy_checks(
            [],
            policy=_policy("stdio"),
            mcp_deps=[MCPDependency.from_string("io.example/missing")],
        )
        check = _transport_check(result)
        assert not check.passed
        detail = check.details[0]
        assert detail.startswith(
            "io.example/missing: effective transport unknown (server not found"
        )
        assert "declare `transport:`" in detail

    def test_unreachable_registry_reason(self, fake_registry):
        fake_registry["unreachable"] = True
        effective = resolution.resolve_effective_mcp_transports(
            [MCPDependency.from_string("io.example/any")]
        )
        value = effective["io.example/any"]
        assert isinstance(value, UnresolvedTransport)
        assert "unreachable" in value.reason


class TestPolicyInstallParity:
    def test_first_declaration_wins_like_install(self, fake_registry):
        fake_registry["servers"][DUAL_SERVER["name"]] = DUAL_SERVER
        production = MCPDependency.from_dict({"name": DUAL_SERVER["name"], "transport": "stdio"})
        development = MCPDependency.from_string(DUAL_SERVER["name"])
        result = run_dependency_policy_checks(
            [], policy=_policy("stdio"), mcp_deps=[production, development]
        )
        assert _transport_check(result).passed

    def test_install_writes_the_snapshot_policy_judged(self, fake_registry, tmp_path):
        """A registry that answers differently later cannot change what is written."""
        from unittest.mock import MagicMock

        from apm_cli.integration.mcp_integrator_install import run_mcp_install

        name = SSE_SERVER["name"]
        fake_registry["servers"][name] = SSE_SERVER
        dep = MCPDependency.from_string(name)
        assert resolution.resolve_effective_mcp_transports([dep]) == {name: frozenset({"sse"})}

        operations = MagicMock()
        operations.registry_client.registry_url = "https://registry.example"
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

        assert server_write_transports(written[name]) == frozenset({"sse"})
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
