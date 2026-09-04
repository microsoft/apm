"""Website-pinned Agent Plugins v1 discovery and runtime-value contracts."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from dataclasses import fields
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.agent_plugins import (
    AgentPluginComponents,
    NotAgentPluginError,
    load_agent_plugin,
)

pytestmark = [pytest.mark.integration, pytest.mark.component]

_FIXTURES = Path(__file__).parents[1] / "fixtures"
_AGENT_PLUGIN_FIXTURES = _FIXTURES / "agent_plugins"
_PORTABLE_PLUGIN = _AGENT_PLUGIN_FIXTURES / "portable"
_SCHEMA_FIXTURES = _FIXTURES / "schemas"

_CLAUSES = {
    "manifest": "Agent Plugins v1 ss4.1.2 and ss5.1",
    "components": "Agent Plugins v1 ss6.1 and ss7.2.1",
    "mcp-loading": "Agent Plugins v1 ss7.2.2(1)",
    "variables": "Agent Plugins v1 ss9.1-ss9.2",
}


@pytest.fixture(scope="module", autouse=True)
def _website_pinned_contract() -> None:
    """Bind every behavioral assertion to the website's immutable spec source."""
    pins = json.loads((_AGENT_PLUGIN_FIXTURES / "upstream-pins.json").read_text(encoding="ascii"))
    assert pins["site"]["commit"] == "b946d6f331055fe83bc675f213e49b53d9371d20"
    assert pins["site"]["specificationSource"] == {
        "repository": "https://github.com/agentplugins/agent-plugins-spec",
        "version": "1.0.0",
        "status": "working-draft",
        "commit": "b78a4f162d92c4b09ee205a11f59a6187926d947",
    }
    assert pins["spec"] == {
        "repository": "agentplugins/agent-plugins-spec",
        "commit": "b78a4f162d92c4b09ee205a11f59a6187926d947",
        "path": "spec/1.0.0.md",
        "sha256": "367152c5f3d619f7d8bef05ce528b0ed810ad95cff72a2f40d85c0ef52b383d1",
    }
    spec_bytes = gzip.decompress((_AGENT_PLUGIN_FIXTURES / "spec" / "1.0.0.md.gz").read_bytes())
    assert hashlib.sha256(spec_bytes).hexdigest() == pins["spec"]["sha256"]
    expected_hashes = {
        "plugin": (
            _SCHEMA_FIXTURES / "agent-plugins-v1.0.0-plugin.schema.json",
            "0a4aad95ce337878ad38802ebf0daa3fde76abe3f65400c86bcbb1ec0b3ab883",
        ),
        "mcp": (
            _SCHEMA_FIXTURES / "agent-plugins-v1.0.0-mcp.schema.json",
            "6539175bfcdf43085855183e86da40ea94b166547a72b47ae9a0a390516d3acb",
        ),
    }
    for name, (path, expected) in expected_hashes.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        assert pins["schemas"][name] == expected


def _copy_portable_plugin(root: Path, name: str) -> Path:
    destination = root / name
    shutil.copytree(_PORTABLE_PLUGIN, destination)
    return destination


def _server_names(plugin_root: Path) -> tuple[str, ...]:
    plugin = load_agent_plugin(plugin_root)
    return tuple(server.name for server in plugin.components.mcp_servers)


def _write_discovery_mcp(plugin_root: Path) -> None:
    (plugin_root / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": {
                    "exact-root": {
                        "type": "stdio",
                        "command": "printf",
                    }
                },
            }
        ),
        encoding="ascii",
    )


def test_discovery_accepts_only_exact_root_agent_plugin_paths(tmp_path: Path) -> None:
    """Exact root names are the only inputs (ss4.1.2, ss5.1, ss6.1, ss7.2.1)."""
    exact = _copy_portable_plugin(tmp_path, "exact")
    _write_discovery_mcp(exact)
    exact_plugin = load_agent_plugin(exact)
    assert exact_plugin.identity.name == "contract-plugin"

    nested_manifest = tmp_path / "nested-manifest"
    nested_manifest.mkdir()
    shutil.copytree(
        _PORTABLE_PLUGIN,
        nested_manifest / ".claude-plugin",
    )
    case_manifest = _copy_portable_plugin(tmp_path, "case-manifest")
    (case_manifest / "plugin.json").rename(case_manifest / "Plugin.json")
    with pytest.raises(NotAgentPluginError):
        load_agent_plugin(nested_manifest)
    with pytest.raises(NotAgentPluginError):
        load_agent_plugin(case_manifest)

    alternate = _copy_portable_plugin(tmp_path, "alternate-mcp")
    _write_discovery_mcp(alternate)
    (alternate / "mcp.json").rename(alternate / ".mcp.json")
    case_variant = _copy_portable_plugin(tmp_path, "case-mcp")
    _write_discovery_mcp(case_variant)
    (case_variant / "mcp.json").rename(case_variant / "MCP.JSON")
    nested = _copy_portable_plugin(tmp_path, "nested-mcp")
    _write_discovery_mcp(nested)
    (nested / "nested").mkdir()
    (nested / "mcp.json").rename(nested / "nested" / "mcp.json")

    actual = {
        "mcp.json": _server_names(exact),
        ".mcp.json": _server_names(alternate),
        "MCP.JSON": _server_names(case_variant),
        "nested/mcp.json": _server_names(nested),
    }
    assert actual == {
        "mcp.json": ("exact-root",),
        ".mcp.json": (),
        "MCP.JSON": (),
        "nested/mcp.json": (),
    }, f"{_CLAUSES['components']}; {_CLAUSES['mcp-loading']}"


def test_portable_core_has_exactly_skills_and_mcp_servers(tmp_path: Path) -> None:
    """Raw client component-like roots never expand the portable v1 vocabulary."""
    plugin_root = _copy_portable_plugin(tmp_path, "portable-core")
    for directory in ("agents", "commands", "hooks", "instructions", "extensions", "bin"):
        component = plugin_root / directory
        component.mkdir()
        (component / "payload.txt").write_text(directory, encoding="ascii")
    (plugin_root / "lsp.json").write_text(
        json.dumps(
            {
                "lspServers": {
                    "alternate": {
                        "command": "alternate",
                        "extensionToLanguage": {".alt": "alternate"},
                    }
                }
            }
        ),
        encoding="ascii",
    )

    plugin = load_agent_plugin(plugin_root)

    assert tuple(field.name for field in fields(AgentPluginComponents)) == (
        "skills",
        "mcp_servers",
    )
    assert tuple(skill.name for skill in plugin.components.skills) == ("contract-skill",)
    assert tuple(server.name for server in plugin.components.mcp_servers) == (
        "contract-remote",
        "contract-stdio",
    )
    ignored_paths = {
        diagnostic.path
        for diagnostic in plugin.diagnostics
        if diagnostic.code == "portable.component.ignored"
    }
    assert ignored_paths == {
        "agents",
        "commands",
        "extensions",
        "hooks",
        "instructions",
        "lsp.json",
    }


def test_loader_preserves_portable_runtime_expressions(tmp_path: Path) -> None:
    """IR preserves ss9.2 expressions; URL/header fields stay literal under ss7.2.1."""
    plugin_root = _copy_portable_plugin(tmp_path, "variables")
    plugin = load_agent_plugin(plugin_root)
    servers = {server.name: server for server in plugin.components.mcp_servers}
    assert tuple(servers) == ("contract-remote", "contract-stdio"), (
        f"{_CLAUSES['variables']}; {_CLAUSES['components']}; diagnostics={plugin.diagnostics!r}"
    )
    stdio = servers["contract-stdio"]
    remote = servers["contract-remote"]

    assert list(stdio.args) == [
        "${PLUGIN_ROOT}/bin/tool",
        "${PLUGIN_DATA}/state",
        "${UNKNOWN_VAR}",
    ]
    stdio_env = dict(stdio.env)
    assert stdio_env["ROOT_REF"] == "${PLUGIN_ROOT}/config"
    assert stdio_env["DATA_REF"] == "${PLUGIN_DATA}/cache"
    assert stdio_env["UNKNOWN_REF"] == "${UNKNOWN_VAR}"
    assert stdio.cwd == "${PLUGIN_ROOT}"

    parsed_url = urlparse(remote.url or "")
    assert parsed_url.scheme == "https"
    assert parsed_url.hostname == "example.invalid"
    assert parsed_url.path == "/${PLUGIN_ROOT}/mcp"
    assert parsed_url.params == ""
    assert parsed_url.query == ""
    assert parsed_url.fragment == ""
    assert dict(remote.headers) == {
        "X-Plugin-Data": "${PLUGIN_DATA}",
        "X-Unknown": "${UNKNOWN_VAR}",
    }
