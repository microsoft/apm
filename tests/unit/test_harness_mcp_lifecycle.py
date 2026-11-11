"""Third-party native MCP cleanup must retain foreign and edited connections."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from apm_cli.adapters.client.gemini import GeminiClientAdapter
from apm_cli.core.scope import InstallScope
from apm_cli.install.errors import RequiredIntegrationError
from apm_cli.install.mcp.ownership import mcp_native_hash
from apm_cli.integration.mcp_integrator import MCPIntegrator


class ExampleClient(GeminiClientAdapter):
    target_name = "example"

    def get_config_path(self):
        return str(self.project_root / ".example" / "settings.json")

    def remove_managed_servers(self, names: set[str]) -> set[str]:
        config = self.get_current_config()
        servers = config[self.mcp_servers_key]
        removed = names.intersection(servers)
        for name in removed:
            del servers[name]
        from pathlib import Path

        Path(self.get_config_path()).write_text(json.dumps(config), encoding="utf-8")
        return removed


@pytest.mark.parametrize("edited", [False, True])
def test_external_cleanup_requires_unchanged_owned_native_content(tmp_path, monkeypatch, edited):
    import apm_cli.harnesses.registry as registry_module
    from apm_cli import factory

    registry = SimpleNamespace(
        mcp_clients={"example": ExampleClient}, external_mcp_clients=frozenset({"example"})
    )
    monkeypatch.setattr(factory, "get_harness_registry", lambda: registry)
    monkeypatch.setattr(registry_module, "get_harness_registry", lambda: registry)
    path = tmp_path / ".example" / "settings.json"
    path.parent.mkdir()
    baseline = {"command": "fixture-server", "args": ["--readonly"]}
    native = {**baseline, "args": ["--user-edit"]} if edited else baseline
    config = {
        "mcpServers": {"tool": native, "foreign": {"command": "user-server"}},
        "theme": "user",
    }
    original = json.dumps(config).encode()
    path.write_bytes(original)
    kwargs = dict(
        runtime="example",
        project_root=tmp_path,
        scope=InstallScope.PROJECT,
        stored_configs={"vendor/tool": {"name": "vendor/tool"}},
        native_hashes={"example": {"vendor/tool": mcp_native_hash(baseline)}},
    )
    if edited:
        with pytest.raises(RequiredIntegrationError, match="Preserved MCP connection"):
            MCPIntegrator.remove_stale({"vendor/tool"}, **kwargs)
        assert path.read_bytes() == original
    else:
        MCPIntegrator.remove_stale({"vendor/tool"}, **kwargs)
        assert json.loads(path.read_text()) == {
            "mcpServers": {"foreign": {"command": "user-server"}},
            "theme": "user",
        }
