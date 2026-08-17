"""Website-pinned Agent Plugins v1 discovery and runtime-value contracts."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from apm_cli.agent_plugins import validate_mcp_config_document
from apm_cli.bundle.local_bundle import detect_local_bundle
from apm_cli.install.local_bundle_handler import _parse_bundle_mcp_servers

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
_APM_REQUIREMENT = (
    "APM-PLUGIN-SC-1 unknown placeholder text cannot authorize target-native "
    "ambient credential expansion"
)


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
    return tuple(
        sorted(
            dependency.name
            for dependency in _parse_bundle_mcp_servers(
                plugin_root,
                agent_plugin=True,
            )
        )
    )


def test_discovery_accepts_only_exact_root_agent_plugin_paths(tmp_path: Path) -> None:
    """Exact root names are the only inputs (ss4.1.2, ss5.1, ss6.1, ss7.2.1)."""
    exact = _copy_portable_plugin(tmp_path, "exact")
    exact_info = detect_local_bundle(exact)
    assert exact_info is not None
    assert exact_info.package_id == "contract-plugin"

    nested_manifest = tmp_path / "nested-manifest"
    nested_manifest.mkdir()
    shutil.copytree(
        _PORTABLE_PLUGIN,
        nested_manifest / ".claude-plugin",
    )
    case_manifest = _copy_portable_plugin(tmp_path, "case-manifest")
    (case_manifest / "plugin.json").rename(case_manifest / "Plugin.json")
    assert detect_local_bundle(nested_manifest) is None
    with pytest.raises(ValueError, match="ambiguous metadata paths"):
        detect_local_bundle(case_manifest)

    alternate = _copy_portable_plugin(tmp_path, "alternate-mcp")
    (alternate / "mcp.json").rename(alternate / ".mcp.json")
    case_variant = _copy_portable_plugin(tmp_path, "case-mcp")
    (case_variant / "mcp.json").rename(case_variant / "MCP.JSON")
    nested = _copy_portable_plugin(tmp_path, "nested-mcp")
    (nested / "nested").mkdir()
    (nested / "mcp.json").rename(nested / "nested" / "mcp.json")

    actual = {
        "mcp.json": _server_names(exact),
        ".mcp.json": _server_names(alternate),
        "MCP.JSON": _server_names(case_variant),
        "nested/mcp.json": _server_names(nested),
    }
    assert actual == {
        "mcp.json": ("contract-remote", "contract-stdio"),
        ".mcp.json": (),
        "MCP.JSON": (),
        "nested/mcp.json": (),
    }, f"{_CLAUSES['components']}; {_CLAUSES['mcp-loading']}"


def test_only_plugin_root_and_data_have_runtime_variable_semantics(tmp_path: Path) -> None:
    """Only two variables expand in args/env/cwd; URL/headers stay literal (ss7.2.1, ss9.2)."""
    plugin_root = _copy_portable_plugin(tmp_path, "variables")
    runtime_root = tmp_path / "retained" / "contract-plugin"
    data_root = tmp_path / "data" / "contract-plugin"
    dependencies = {
        dependency.name: dependency
        for dependency in _parse_bundle_mcp_servers(
            plugin_root,
            data_root=data_root,
            agent_plugin=True,
            runtime_root=runtime_root,
        )
    }
    stdio = dependencies["contract-stdio"]
    remote = dependencies["contract-remote"]

    assert list(stdio.args) == [
        f"{runtime_root}/bin/tool",
        f"{data_root}/state",
        "${UNKNOWN_VAR}",
    ]
    assert stdio.env["ROOT_REF"] == f"{runtime_root}/config"
    assert stdio.env["DATA_REF"] == f"{data_root}/cache"
    assert stdio.env["UNKNOWN_REF"] == "${UNKNOWN_VAR}"
    assert stdio.cwd == str(runtime_root)
    assert remote.url == "https://example.invalid/${PLUGIN_ROOT}/mcp"
    assert remote.headers == {
        "X-Plugin-Data": "${PLUGIN_DATA}",
        "X-Unknown": "${UNKNOWN_VAR}",
    }

    # Unknown placeholders remain literal under ss9.2. APM separately refuses
    # to project credential-shaped literals into targets that expand them.
    unknown_secret_reference = validate_mcp_config_document(
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {
                "ambient-secret": {
                    "type": "stdio",
                    "command": "printf",
                    "env": {"API_TOKEN": "${GITHUB_TOKEN}"},
                }
            },
        }
    )
    assert unknown_secret_reference.is_valid is False, (
        f"{_APM_REQUIREMENT}; {_CLAUSES['variables']} keeps ${{GITHUB_TOKEN}} literal"
    )
