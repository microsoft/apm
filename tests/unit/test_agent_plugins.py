"""Unit tests for the Agent Plugins v1 contract helpers."""

from __future__ import annotations

import pytest

from apm_cli.agent_plugins import (
    MCP_SCHEMA_ID,
    PLUGIN_SCHEMA_ID,
    supports_mcp_schema_id,
    supports_plugin_schema_id,
    validate_mcp_config_document,
    validate_plugin_manifest_document,
)

pytestmark = pytest.mark.unit


class TestLocalPluginManifestValidation:
    """Local plugin manifest validation matches the Agent Plugins rules."""

    def test_supported_and_unsupported_schema_ids(self) -> None:
        assert supports_plugin_schema_id(PLUGIN_SCHEMA_ID) is True
        assert (
            supports_plugin_schema_id("https://agent-plugins.org/schemas/0.9.0/plugin.schema.json")
            is False
        )

    def test_strict_name_rules(self) -> None:
        result = validate_plugin_manifest_document(
            {
                "$schema": PLUGIN_SCHEMA_ID,
                "name": "good.plugin-1",
                "extensions": {"com.microsoft.apm": {"schemaVersion": "1"}},
            }
        )
        assert result.is_valid is True
        assert result.normalized is not None
        assert result.normalized["name"] == "good.plugin-1"

    @pytest.mark.parametrize(
        "name",
        ["Bad-Name", "-start", "end-", "too.many..dots", "has--double", ""],
    )
    def test_invalid_plugin_names_fail(self, name: str) -> None:
        result = validate_plugin_manifest_document(
            {
                "$schema": PLUGIN_SCHEMA_ID,
                "name": name,
                "extensions": {"com.microsoft.apm": {"schemaVersion": "1"}},
            }
        )
        assert result.is_valid is False
        assert result.errors

    def test_unknown_top_level_fields_are_warned_and_ignored(self) -> None:
        result = validate_plugin_manifest_document(
            {
                "$schema": PLUGIN_SCHEMA_ID,
                "name": "plugin-name",
                "mystery": "ignored",
                "extensions": {"com.microsoft.apm": {"schemaVersion": "1"}},
            }
        )
        assert result.is_valid is True
        assert any("mystery" in warning for warning in result.warnings)
        assert result.normalized is not None
        assert "mystery" not in result.normalized

    def test_non_object_extensions_are_ignored_with_warning(self) -> None:
        result = validate_plugin_manifest_document(
            {
                "$schema": PLUGIN_SCHEMA_ID,
                "name": "plugin-name",
                "extensions": "ignore me",
            }
        )
        assert result.is_valid is True
        assert result.normalized == {"$schema": PLUGIN_SCHEMA_ID, "name": "plugin-name"}
        assert any("extensions field ignored" in warning for warning in result.warnings)

    def test_supported_extension_block_is_optional(self) -> None:
        result = validate_plugin_manifest_document(
            {"$schema": PLUGIN_SCHEMA_ID, "name": "plugin-name"}
        )
        assert result.is_valid is True

    def test_ordinary_type_violations_are_fatal(self) -> None:
        result = validate_plugin_manifest_document(
            {"$schema": PLUGIN_SCHEMA_ID, "name": "plugin-name", "keywords": ["ok", 1]}
        )
        assert result.is_valid is False
        assert any("keywords" in error for error in result.errors)


class TestLocalMcpValidation:
    """Local MCP validation keeps top-level and server-level failure boundaries."""

    def test_supported_and_unsupported_schema_ids(self) -> None:
        assert supports_mcp_schema_id(MCP_SCHEMA_ID) is True
        assert (
            supports_mcp_schema_id("https://agent-plugins.org/schemas/0.9.0/mcp.schema.json")
            is False
        )

    def test_top_level_validation_rejects_unknown_fields(self) -> None:
        result = validate_mcp_config_document(
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {},
                "unexpected": True,
            }
        )
        assert result.is_valid is False
        assert any("unexpected" in error for error in result.errors)

    @pytest.mark.parametrize(
        "server_kind, invalid_server_name, expected_present_names",
        [
            (
                "stdio",
                "remote-bad",
                ("stdio-ok", "sse-ok"),
            ),
            (
                "streamable-http",
                "remote-bad",
                ("stdio-ok", "sse-ok"),
            ),
            (
                "sse",
                "sse-bad",
                ("stdio-ok", "remote-ok"),
            ),
        ],
    )
    def test_per_server_isolation(
        self,
        server_kind: str,
        invalid_server_name: str,
        expected_present_names: tuple[str, ...],
    ) -> None:
        if server_kind == "stdio":
            document = {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {
                    "stdio-ok": {"type": "stdio", "command": "./bin/tool", "cwd": "./data"},
                    invalid_server_name: {
                        "type": "streamable-http",
                        "url": "https://example.com:bad/mcp",
                    },
                    "sse-ok": {"type": "sse", "url": "https://example.com/sse"},
                },
            }
        elif server_kind == "streamable-http":
            document = {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {
                    "stdio-ok": {"type": "stdio", "command": "./bin/tool"},
                    invalid_server_name: {
                        "type": "streamable-http",
                        "url": "https://example.com:bad/mcp",
                    },
                    "sse-ok": {"type": "sse", "url": "https://example.com/sse"},
                },
            }
        else:
            document = {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {
                    "stdio-ok": {"type": "stdio", "command": "./bin/tool"},
                    "remote-ok": {"type": "streamable-http", "url": "https://example.com/mcp"},
                    invalid_server_name: {"type": "sse", "url": "https://${UNKNOWN_HOST}/mcp"},
                },
            }

        result = validate_mcp_config_document(document)
        assert result.is_valid is False
        assert result.normalized is not None
        for name in expected_present_names:
            assert name in result.normalized["mcpServers"]
        assert invalid_server_name not in result.normalized["mcpServers"]
        assert any("url" in error or "command" in error for error in result.errors)

    def test_stdio_env_and_headers_are_strict(self) -> None:
        result = validate_mcp_config_document(
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {
                    "tool": {
                        "type": "stdio",
                        "command": "./bin/tool",
                        "env": {"PLUGIN_ROOT": "nope"},
                    }
                },
            }
        )
        assert result.is_valid is False
        assert any("reserved name" in error for error in result.errors)

    def test_remote_url_rejects_userinfo_and_fragment(self) -> None:
        url = "http://user:literal@example.com:8080/${UNKNOWN_VAR}#fragment"
        result = validate_mcp_config_document(
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {
                    "tool": {
                        "type": "streamable-http",
                        "url": url,
                    }
                },
            }
        )
        assert result.is_valid is False
        assert any("url" in error for error in result.errors)

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/${UNKNOWN_VAR}?token=${GITHUB_TOKEN}",
            "https://example.com/${_X}/${X1}?encoded=hello%20world",
            "https://example.com./mcp",
            "http://localhost:8080/${UNKNOWN_VAR}",
            "http://127.0.0.1:8080/${UNKNOWN_VAR}",
            "http://[::1]:8080/${UNKNOWN_VAR}",
        ],
    )
    def test_remote_url_accepts_spec_endpoints_and_preserves_literals(self, url: str) -> None:
        result = validate_mcp_config_document(
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {
                    "tool": {
                        "type": "streamable-http",
                        "url": url,
                    }
                },
            }
        )

        assert result.is_valid is True
        assert result.normalized is not None
        assert result.normalized["mcpServers"]["tool"]["url"] == url

    @pytest.mark.parametrize(
        "url",
        [
            "https://${UNKNOWN_HOST}/mcp",
            "https://example.com:${UNKNOWN_PORT}/mcp",
            "https://example.com:bad/mcp",
            "https://example.com:0/mcp",
            "https://${TOKEN}@example.com/mcp",
            "https://example.com:/mcp",
            "https://user name@example.com/mcp",
            "https://example.com/\x00",
            "https://example.com/\x01",
            "https://example.com/\x7f",
            "https://exa\nmple.com/mcp",
            "https://user:literal@example.com/mcp",
            "https://example.com/mcp#fragment",
            "http://example.com/mcp",
            "http://192.0.2.1/mcp",
            "http://[2001:db8::1]/mcp",
            "https://example.com/%",
            "https://example.com/%2",
            "https://example.com/%GG",
            "https://example.com\\mcp",
            "https://example.com/path\\segment",
            "https://example.com/mcp?value=left\\right",
            "https://example.com/${X",
            "https://example.com/${}",
            "https://example.com/${1X}",
            "https://example.com/${X-Y}",
            "https://example.com/stray}",
            "http://[::1%eth0]/",
            "http://[::1%25eth0]/",
            "https://[::1%ab]/",
            "https://[::1%25eth0]/",
            "https://[2001:db8::1%25eth0]/",
            "https://example.com../",
            "https://example.com/\u0080",
            "https://example.com/\u009f",
        ],
    )
    def test_remote_url_rejects_placeholder_or_invalid_authority(self, url: str) -> None:
        result = validate_mcp_config_document(
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {
                    "tool": {
                        "type": "streamable-http",
                        "url": url,
                    }
                },
            }
        )

        assert result.is_valid is False
        assert any("url" in error for error in result.errors)

    @pytest.mark.parametrize("rooted", [False, True])
    def test_remote_url_accepts_maximum_dns_hostname_length(self, rooted: bool) -> None:
        host = ".".join(["a" * 63] * 3 + ["a" * 61])
        if rooted:
            host += "."
        url = f"https://{host}/mcp"

        result = validate_mcp_config_document(
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {
                    "tool": {
                        "type": "streamable-http",
                        "url": url,
                    }
                },
            }
        )

        assert len(host) == (254 if rooted else 253)
        assert result.is_valid is True
        assert result.normalized is not None
        assert result.normalized["mcpServers"]["tool"]["url"] == url

    @pytest.mark.parametrize(
        ("labels", "expected_length"),
        [
            (["a" * 63] * 3 + ["a" * 62], 254),
            (["a"] * 128, 255),
        ],
    )
    def test_remote_url_rejects_dns_hostname_over_total_length_limit(
        self,
        labels: list[str],
        expected_length: int,
    ) -> None:
        host = ".".join(labels)
        result = validate_mcp_config_document(
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {
                    "tool": {
                        "type": "streamable-http",
                        "url": f"https://{host}/mcp",
                    }
                },
            }
        )

        assert len(host) == expected_length
        assert result.is_valid is False
        assert any("url" in error for error in result.errors)

    @pytest.mark.parametrize(
        "args",
        [
            ["--token=literal"],
            ["--api-key", "literal"],
            ["Authorization: Bearer literal"],
        ],
    )
    def test_stdio_args_preserve_literals_without_enforcing_security_policy(
        self,
        args: list[str],
    ) -> None:
        result = validate_mcp_config_document(
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {"tool": {"type": "stdio", "command": "tool", "args": args}},
            }
        )

        assert result.is_valid is True
        assert result.normalized is not None
        assert result.normalized["mcpServers"]["tool"]["args"] == args
