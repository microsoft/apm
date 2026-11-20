"""Unit tests for the Agent Plugins v1 contract helpers (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from apm_cli.agent_plugins import (
    AGENT_PLUGINS_SITE_COMMIT,
    AGENT_PLUGINS_SPEC_COMMIT,
    AGENT_PLUGINS_SPEC_SHA256,
    AGENT_PLUGINS_VERSION,
    COM_MICROSOFT_APM_NAMESPACE,
    COM_MICROSOFT_APM_SCHEMA_VERSION,
    MCP_SCHEMA_ID,
    MCP_SCHEMA_SHA256,
    PLUGIN_SCHEMA_ID,
    PLUGIN_SCHEMA_SHA256,
    read_json_document,
)

pytestmark = pytest.mark.component

_SCHEMA_DIR = Path(__file__).parent.parent / "fixtures" / "schemas"
_PLUGIN_SCHEMA_PATH = _SCHEMA_DIR / "agent-plugins-v1.0.0-plugin.schema.json"
_MCP_SCHEMA_PATH = _SCHEMA_DIR / "agent-plugins-v1.0.0-mcp.schema.json"


@pytest.fixture(scope="module")
def plugin_schema() -> dict:
    return json.loads(_PLUGIN_SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def mcp_schema() -> dict:
    return json.loads(_MCP_SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate_against_schema(schema: dict, document: dict) -> None:
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    assert errors == [], "\n".join(
        f"{list(error.absolute_path)}: {error.message}" for error in errors
    )


class TestVendoredSchemas:
    """Vendored schemas must stay aligned with the official v1 documents."""

    def test_constants_match_v1_contract(self) -> None:
        assert AGENT_PLUGINS_VERSION == "1.0.0"
        assert AGENT_PLUGINS_SITE_COMMIT == "b946d6f331055fe83bc675f213e49b53d9371d20"
        assert AGENT_PLUGINS_SPEC_COMMIT == "b78a4f162d92c4b09ee205a11f59a6187926d947"
        assert (
            AGENT_PLUGINS_SPEC_SHA256
            == "367152c5f3d619f7d8bef05ce528b0ed810ad95cff72a2f40d85c0ef52b383d1"
        )
        assert (
            PLUGIN_SCHEMA_SHA256
            == "0a4aad95ce337878ad38802ebf0daa3fde76abe3f65400c86bcbb1ec0b3ab883"
        )
        assert (
            MCP_SCHEMA_SHA256 == "6539175bfcdf43085855183e86da40ea94b166547a72b47ae9a0a390516d3acb"
        )
        assert COM_MICROSOFT_APM_NAMESPACE == "com.microsoft.apm"
        assert COM_MICROSOFT_APM_SCHEMA_VERSION == "1"
        assert PLUGIN_SCHEMA_ID.endswith("/plugin.schema.json")
        assert MCP_SCHEMA_ID.endswith("/mcp.schema.json")

    def test_plugin_schema_fixture_integrity(self, plugin_schema: dict) -> None:
        assert plugin_schema["$id"] == PLUGIN_SCHEMA_ID
        assert plugin_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        _validate_against_schema(
            plugin_schema,
            {
                "$schema": PLUGIN_SCHEMA_ID,
                "name": "agent-plugin",
                "extensions": {"com.microsoft.apm": {"schemaVersion": "1"}},
            },
        )

    def test_mcp_schema_fixture_integrity(self, mcp_schema: dict) -> None:
        assert mcp_schema["$id"] == MCP_SCHEMA_ID
        assert mcp_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        _validate_against_schema(
            mcp_schema,
            {"$schema": MCP_SCHEMA_ID, "mcpServers": {}},
        )


class TestFileLoading:
    """File helpers use bounded local JSON reads."""

    def test_read_json_document_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        payload = {"$schema": PLUGIN_SCHEMA_ID, "name": "round-trip"}
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert read_json_document(path) == payload

    def test_read_json_document_rejects_symlink(self, tmp_path: Path) -> None:
        target = tmp_path / "target.json"
        target.write_text("{}", encoding="utf-8")
        link = tmp_path / "manifest.json"
        link.symlink_to(target)

        with pytest.raises(ValueError, match="must not be a symlink"):
            read_json_document(link)
