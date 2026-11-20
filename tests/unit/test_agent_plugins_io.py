"""Unit tests for the Agent Plugins v1 contract helpers (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from apm_cli.agent_plugins import (
    MCP_SCHEMA_ID,
    PLUGIN_SCHEMA_ID,
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
