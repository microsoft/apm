"""Acceptance tests for extra passthrough in MCPDependency.from_dict().

Addresses #1670 (unknown keys are preserved in 'extra' and round-tripped
through to_dict and into generated target manifests).

Coverage:
1. from_dict with unknown key -> warning naming the preserved key
2. from_dict with only known keys -> no warning
3. known-key parsing and resulting values are unchanged
4. robustness: non-string dict keys do not TypeError; non-ASCII output is escaped
5. extra round-trips through from_dict/to_dict
6. extra does not shadow known keys
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apm_cli.models.apm_package import MCPDependency

_WARN_PATH = "apm_cli.models.dependency.mcp._rich_warning"


class TestFromDictUnknownKeyWarning:
    """Acceptance criteria: warning fires when unknown keys are present."""

    def test_unknown_key_triggers_warning(self):
        """from_dict with an unknown key emits exactly one _rich_warning call."""
        with patch(_WARN_PATH) as mock_warn:
            MCPDependency.from_dict(
                {
                    "name": "slack",
                    "transport": "http",
                    "registry": False,
                    "url": "https://mcp.slack.com/mcp",
                    "oauth": {"clientId": "abc", "callbackPort": 3118},
                }
            )
        mock_warn.assert_called_once()

    def test_unknown_key_warning_names_preserved_key(self):
        """The warning message includes the name of the unknown key."""
        with patch(_WARN_PATH) as mock_warn:
            MCPDependency.from_dict(
                {
                    "name": "slack",
                    "transport": "http",
                    "registry": False,
                    "url": "https://mcp.slack.com/mcp",
                    "oauth": {"clientId": "abc", "callbackPort": 3118},
                }
            )
        msg = mock_warn.call_args[0][0]
        assert "oauth" in msg

    def test_multiple_unknown_keys_single_warning(self):
        """Multiple unknown keys produce ONE aggregated warning (not one per key)."""
        with patch(_WARN_PATH) as mock_warn:
            MCPDependency.from_dict(
                {
                    "name": "my-server",
                    "transport": "http",
                    "registry": False,
                    "url": "https://example.com/mcp",
                    "extra_a": "foo",
                    "extra_b": "bar",
                }
            )
        assert mock_warn.call_count == 1
        msg = mock_warn.call_args[0][0]
        assert "extra_a" in msg
        assert "extra_b" in msg

    def test_unknown_key_warning_names_dependency(self):
        """Warning message includes the dependency name for user context."""
        with patch(_WARN_PATH) as mock_warn:
            MCPDependency.from_dict(
                {
                    "name": "slack",
                    "transport": "http",
                    "registry": False,
                    "url": "https://mcp.slack.com/mcp",
                    "oauth": {},
                }
            )
        msg = mock_warn.call_args[0][0]
        assert "slack" in msg

    def test_unknown_key_warning_is_ascii_only(self):
        """Warning message must be printable ASCII (cp1252-safe)."""
        with patch(_WARN_PATH) as mock_warn:
            MCPDependency.from_dict(
                {
                    "name": "my-server",
                    "transport": "http",
                    "registry": False,
                    "url": "https://example.com/mcp",
                    "mystery": "value",
                }
            )
        msg = mock_warn.call_args[0][0]
        assert all(0x20 <= ord(c) <= 0x7E for c in msg), f"non-ASCII chars in: {msg!r}"

    def test_non_string_key_no_type_error(self):
        """from_dict with a non-string (integer) dict key must not raise TypeError."""
        with patch(_WARN_PATH) as mock_warn:
            dep = MCPDependency.from_dict(
                {
                    "name": "server",
                    123: "integer-key-value",
                }
            )
        assert dep.name == "server"
        mock_warn.assert_called_once()
        msg = mock_warn.call_args[0][0]
        assert "123" in msg

    def test_non_ascii_name_warning_is_ascii_only(self):
        """Warning message stays printable ASCII when dep name contains non-ASCII before validation."""
        with patch(_WARN_PATH) as mock_warn:
            with pytest.raises(ValueError):
                MCPDependency.from_dict(
                    {
                        "name": "caf\xe9-server",
                        "unknown_key": "val",
                    }
                )
        msg = mock_warn.call_args[0][0]
        assert all(0x20 <= ord(c) <= 0x7E for c in msg), f"non-ASCII chars in: {msg!r}"


class TestFromDictKnownKeysNoWarning:
    """Acceptance criteria: no warning when only known keys are present."""

    def test_minimal_dict_no_warning(self):
        """from_dict with only 'name' emits no warning."""
        with patch(_WARN_PATH) as mock_warn:
            MCPDependency.from_dict({"name": "my-server"})
        mock_warn.assert_not_called()

    def test_all_known_keys_no_warning(self):
        """from_dict with all known keys emits no warning."""
        with patch(_WARN_PATH) as mock_warn:
            MCPDependency.from_dict(
                {
                    "name": "full-server",
                    "transport": "stdio",
                    "env": {"KEY": "val"},
                    "args": ["--flag"],
                    "version": "1.0.0",
                    "registry": False,
                    "package": "npm",
                    "headers": {"X-Auth": "tok"},
                    "tools": ["read"],
                    "command": "npx",
                }
            )
        mock_warn.assert_not_called()

    def test_legacy_type_key_no_warning(self):
        """The legacy 'type' key (alias for 'transport') is known and must not warn."""
        with patch(_WARN_PATH) as mock_warn:
            MCPDependency.from_dict({"name": "legacy-server", "type": "stdio"})
        mock_warn.assert_not_called()

    def test_registry_resolved_server_no_warning(self):
        """A registry-resolved server dict with only known keys emits no warning."""
        with patch(_WARN_PATH) as mock_warn:
            MCPDependency.from_dict(
                {
                    "name": "io.github.github/github-mcp-server",
                    "version": "1.2.3",
                    "env": {"GITHUB_TOKEN": "tok"},
                }
            )
        mock_warn.assert_not_called()


class TestFromDictKnownKeyParsingUnchanged:
    """Acceptance criteria: known-key parsing and resulting values are unchanged."""

    def test_known_keys_parsed_correctly_with_unknown_present(self):
        """Unknown key must not corrupt known-key values."""
        with patch(_WARN_PATH):
            dep = MCPDependency.from_dict(
                {
                    "name": "slack",
                    "transport": "http",
                    "registry": False,
                    "url": "https://mcp.slack.com/mcp",
                    "env": {"TOKEN": "tok"},
                    "oauth": {"clientId": "abc"},
                }
            )
        assert dep.name == "slack"
        assert dep.transport == "http"
        assert dep.registry is False
        assert dep.url == "https://mcp.slack.com/mcp"
        assert dep.env == {"TOKEN": "tok"}

    def test_unknown_key_stored_in_extra(self):
        """Unknown key must appear in the 'extra' dict on the resulting instance."""
        with patch(_WARN_PATH):
            dep = MCPDependency.from_dict(
                {
                    "name": "slack",
                    "transport": "http",
                    "registry": False,
                    "url": "https://mcp.slack.com/mcp",
                    "oauth": {"clientId": "abc"},
                }
            )
        assert dep.extra == {"oauth": {"clientId": "abc"}}

    def test_to_dict_round_trips_extra_keys(self):
        """to_dict() includes extra keys at the top level."""
        with patch(_WARN_PATH):
            dep = MCPDependency.from_dict(
                {
                    "name": "slack",
                    "transport": "http",
                    "registry": False,
                    "url": "https://mcp.slack.com/mcp",
                    "oauth": {"clientId": "abc"},
                }
            )
        result = dep.to_dict()
        assert result["oauth"] == {"clientId": "abc"}
        assert result["name"] == "slack"
        assert result["transport"] == "http"

    def test_missing_name_still_raises(self):
        """ValueError for missing 'name' is unchanged."""
        with pytest.raises(ValueError, match="name"):
            MCPDependency.from_dict({"oauth": "value"})


class TestExtraPassthrough:
    """Acceptance criteria for the extra passthrough mechanism."""

    def test_extra_does_not_shadow_known_keys(self):
        """Extra keys cannot override known keys in to_dict output."""
        dep = MCPDependency(
            name="test",
            transport="stdio",
            extra={"transport": "http", "name": "evil"},
        )
        result = dep.to_dict()
        assert result["transport"] == "stdio"
        assert result["name"] == "test"

    def test_extra_none_when_no_unknown_keys(self):
        """extra is None when from_dict receives only known keys."""
        with patch(_WARN_PATH) as mock_warn:
            dep = MCPDependency.from_dict({"name": "server", "transport": "stdio"})
        mock_warn.assert_not_called()
        assert dep.extra is None

    def test_extra_round_trip_multiple_keys(self):
        """Multiple extra keys round-trip through from_dict/to_dict."""
        with patch(_WARN_PATH):
            dep = MCPDependency.from_dict(
                {
                    "name": "slack",
                    "transport": "http",
                    "registry": False,
                    "url": "https://mcp.slack.com/mcp",
                    "oauth": {"clientId": "abc", "callbackPort": 3118},
                    "customSetting": "value",
                }
            )
        result = dep.to_dict()
        assert result["oauth"] == {"clientId": "abc", "callbackPort": 3118}
        assert result["customSetting"] == "value"

    def test_extra_in_repr(self):
        """__repr__ includes extra key count when extra is present."""
        dep = MCPDependency(name="test", extra={"oauth": {}, "custom": "val"})
        assert "extra=<2 key(s)>" in repr(dep)

    def test_no_extra_in_repr_when_none(self):
        """__repr__ does not include extra when it is None."""
        dep = MCPDependency(name="test")
        assert "extra" not in repr(dep)


class TestBuildSelfDefinedInfoExtra:
    """_build_self_defined_info passes extra to adapters."""

    def test_extra_flows_to_server_info(self):
        """_build_self_defined_info includes _extra when dep has extra."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = MCPDependency(
            name="slack",
            transport="http",
            registry=False,
            url="https://mcp.slack.com/mcp",
            extra={"oauth": {"clientId": "abc"}},
        )
        info = MCPIntegrator._build_self_defined_info(dep)
        assert info["_extra"] == {"oauth": {"clientId": "abc"}}

    def test_no_extra_when_dep_has_none(self):
        """_build_self_defined_info omits _extra when dep.extra is None."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = MCPDependency(
            name="slack",
            transport="http",
            registry=False,
            url="https://mcp.slack.com/mcp",
        )
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "_extra" not in info


class TestAdapterMergeExtra:
    """_merge_extra correctly merges extra keys into adapter config."""

    def test_merge_extra_adds_keys(self):
        """Extra keys are added to the config dict."""
        from apm_cli.adapters.client.base import MCPClientAdapter

        config = {"type": "http", "url": "https://example.com"}
        server_info = {"_extra": {"oauth": {"clientId": "abc"}}}
        MCPClientAdapter._merge_extra(config, server_info)
        assert config["oauth"] == {"clientId": "abc"}

    def test_merge_extra_does_not_shadow(self):
        """Extra keys do not override existing config keys."""
        from apm_cli.adapters.client.base import MCPClientAdapter

        config = {"type": "http", "url": "https://example.com"}
        server_info = {"_extra": {"type": "stdio", "oauth": {"clientId": "abc"}}}
        MCPClientAdapter._merge_extra(config, server_info)
        assert config["type"] == "http"
        assert config["oauth"] == {"clientId": "abc"}

    def test_merge_extra_noop_when_absent(self):
        """No-op when server_info has no _extra."""
        from apm_cli.adapters.client.base import MCPClientAdapter

        config = {"type": "http"}
        server_info = {"name": "test"}
        MCPClientAdapter._merge_extra(config, server_info)
        assert config == {"type": "http"}


class TestExplicitExtraBlock:
    """Explicit 'extra:' YAML key merges into the extra dict."""

    def test_explicit_extra_block_captured(self):
        """An explicit 'extra:' dict merges into dep.extra."""
        with patch(_WARN_PATH) as mock_warn:
            dep = MCPDependency.from_dict(
                {
                    "name": "server",
                    "transport": "http",
                    "extra": {"oauth": {"clientId": "abc"}},
                }
            )
        mock_warn.assert_not_called()
        assert dep.extra == {"oauth": {"clientId": "abc"}}

    def test_explicit_extra_merged_with_unknown_keys(self):
        """Explicit 'extra:' and unknown top-level keys both land in extra."""
        with patch(_WARN_PATH):
            dep = MCPDependency.from_dict(
                {
                    "name": "server",
                    "transport": "http",
                    "extra": {"oauth": {"clientId": "abc"}},
                    "customSetting": "value",
                }
            )
        assert dep.extra["oauth"] == {"clientId": "abc"}
        assert dep.extra["customSetting"] == "value"

    def test_explicit_extra_not_nested(self):
        """The explicit 'extra:' key itself does not appear nested inside extra."""
        with patch(_WARN_PATH) as mock_warn:
            dep = MCPDependency.from_dict(
                {
                    "name": "server",
                    "extra": {"key": "val"},
                }
            )
        mock_warn.assert_not_called()
        assert "extra" not in dep.extra
        assert dep.extra == {"key": "val"}


class TestApplyOverlayExtra:
    """_apply_overlay propagates dep.extra for registry-resolved deps."""

    def test_overlay_propagates_extra(self):
        """_apply_overlay sets _extra on cached server_info when dep has extra."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        cache = {"my-server": {"name": "my-server", "packages": []}}
        dep = MCPDependency(
            name="my-server",
            extra={"oauth": {"clientId": "abc"}},
        )
        MCPIntegrator._apply_overlay(cache, dep)
        assert cache["my-server"]["_extra"] == {"oauth": {"clientId": "abc"}}

    def test_overlay_no_extra_when_none(self):
        """_apply_overlay does not set _extra when dep.extra is None."""
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        cache = {"my-server": {"name": "my-server", "packages": []}}
        dep = MCPDependency(name="my-server")
        MCPIntegrator._apply_overlay(cache, dep)
        assert "_extra" not in cache["my-server"]


class TestAdapterRealPathExtraRender:
    """End-to-end: extra reaches the FINAL rendered config via a real adapter path.

    These drive the actual ``_format_server_config`` render (not the isolated
    ``_merge_extra`` helper) so the per-adapter call sites are not mutation-blind.
    Covers stdio + remote on two adapters (Codex, Copilot).
    """

    def test_codex_stdio_renders_extra(self, tmp_path):
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "slack",
            "_raw_stdio": {"command": "my-cmd", "args": ["--flag"], "env": {}},
            "_extra": {"oauth": {"clientId": "abc", "callbackPort": 3118}},
        }
        cfg = adapter._format_server_config(server_info)
        assert cfg["oauth"] == {"clientId": "abc", "callbackPort": 3118}

    def test_codex_remote_renders_extra(self, tmp_path):
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "slack",
            "id": "uuid-1",
            "remotes": [{"transport_type": "streamable-http", "url": "https://mcp.slack.com/mcp"}],
            "packages": [],
            "_extra": {"oauth": {"clientId": "abc", "callbackPort": 3118}},
        }
        cfg = adapter._format_server_config(server_info)
        assert cfg["url"] == "https://mcp.slack.com/mcp"
        assert cfg["oauth"] == {"clientId": "abc", "callbackPort": 3118}

    def test_copilot_stdio_renders_extra(self):
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter()
        server_info = {
            "name": "slack",
            "_raw_stdio": {"command": "my-cmd", "args": ["--flag"], "env": {}},
            "_extra": {"oauth": {"clientId": "abc"}},
        }
        cfg = adapter._format_server_config(server_info)
        assert cfg["oauth"] == {"clientId": "abc"}

    def test_copilot_remote_renders_extra(self):
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter()
        server_info = {
            "name": "slack",
            "id": "uuid-2",
            "remotes": [{"transport_type": "http", "url": "https://mcp.slack.com/mcp"}],
            "_extra": {"oauth": {"clientId": "abc"}},
        }
        cfg = adapter._format_server_config(server_info)
        assert cfg["url"] == "https://mcp.slack.com/mcp"
        assert cfg["oauth"] == {"clientId": "abc"}


class TestAdapterRealPathShadowGuard:
    """End-to-end: extra cannot shadow/redirect adapter-set fields on a real path."""

    def test_codex_remote_extra_cannot_redirect_url(self, tmp_path):
        """A denylisted ``url`` in extra must not overwrite the real remote URL."""
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "slack",
            "id": "uuid-1",
            "remotes": [{"transport_type": "streamable-http", "url": "https://mcp.slack.com/mcp"}],
            "packages": [],
            "_extra": {"url": "https://evil.example.com/mcp", "oauth": {"clientId": "abc"}},
        }
        cfg = adapter._format_server_config(server_info)
        assert cfg["url"] == "https://mcp.slack.com/mcp"
        assert cfg["oauth"] == {"clientId": "abc"}

    def test_codex_remote_extra_cannot_inject_http_headers(self, tmp_path):
        """``http_headers`` is a denylisted harness alias and must not be injectable."""
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "slack",
            "id": "uuid-1",
            "remotes": [{"transport_type": "streamable-http", "url": "https://mcp.slack.com/mcp"}],
            "packages": [],
            "_extra": {"http_headers": {"Authorization": "Bearer evil"}},
        }
        cfg = adapter._format_server_config(server_info)
        assert "http_headers" not in cfg

    def test_copilot_stdio_extra_cannot_redirect_command(self):
        """A denylisted ``command`` in extra must not overwrite the real command."""
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter()
        server_info = {
            "name": "slack",
            "_raw_stdio": {"command": "real-cmd", "args": [], "env": {}},
            "_extra": {"command": "/bin/evil", "oauth": {"clientId": "abc"}},
        }
        cfg = adapter._format_server_config(server_info)
        assert cfg["command"] == "real-cmd"
        assert cfg["oauth"] == {"clientId": "abc"}

    def test_codex_remote_extra_cannot_inject_command_on_empty_path(self, tmp_path):
        """The denylist is unconditional: ``command`` is blocked even on the remote
        path that never pre-sets ``command`` (the ``if k not in config`` guard would
        otherwise let it through)."""
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "slack",
            "id": "uuid-1",
            "remotes": [{"transport_type": "streamable-http", "url": "https://mcp.slack.com/mcp"}],
            "packages": [],
            "_extra": {"command": "/bin/evil", "oauth": {"clientId": "abc"}},
        }
        cfg = adapter._format_server_config(server_info)
        assert "command" not in cfg
        assert cfg["oauth"] == {"clientId": "abc"}


class TestExtraReservedKeyDenylist:
    """Reserved modeled-field names cannot pass through 'extra' at either layer."""

    def test_explicit_extra_reserved_key_stripped(self):
        """An explicit 'extra:' block carrying a modeled-field name drops it."""
        with patch(_WARN_PATH) as mock_warn:
            dep = MCPDependency.from_dict(
                {
                    "name": "server",
                    "transport": "http",
                    "registry": False,
                    "url": "https://example.com/mcp",
                    "extra": {"command": "/bin/evil", "oauth": {"clientId": "abc"}},
                }
            )
        assert dep.extra == {"oauth": {"clientId": "abc"}}
        msg = mock_warn.call_args[0][0]
        assert "command" in msg

    def test_explicit_extra_reserved_alias_type_stripped(self):
        """The legacy alias 'type' is reserved and stripped from an explicit extra block."""
        with patch(_WARN_PATH):
            dep = MCPDependency.from_dict(
                {
                    "name": "server",
                    "transport": "http",
                    "registry": False,
                    "url": "https://example.com/mcp",
                    "extra": {"type": "stdio", "oauth": {}},
                }
            )
        assert "type" not in dep.extra
        assert dep.extra == {"oauth": {}}

    def test_merge_extra_blocks_reserved_keys_unconditionally(self):
        """_merge_extra drops modeled-field names even when absent from config."""
        from apm_cli.adapters.client.base import MCPClientAdapter

        config = {"url": "https://example.com", "id": "uuid"}
        server_info = {
            "_extra": {
                "command": "/bin/evil",
                "env": {"X": "1"},
                "http_headers": {"Authorization": "Bearer evil"},
                "oauth": {"clientId": "abc"},
            }
        }
        MCPClientAdapter._merge_extra(config, server_info)
        assert "command" not in config
        assert "env" not in config
        assert "http_headers" not in config
        assert config["oauth"] == {"clientId": "abc"}


class TestResolvedByKeyReserved:
    """resolved_by is install-time provenance, never a manifest-settable key.

    Reserving it keeps the "provenance never leaks into mcp_configs" invariant
    airtight: a manifest key named resolved_by is ignored, not passed through
    ``extra`` into the serialized config (#2081).
    """

    def test_manifest_resolved_by_not_serialized(self):
        with patch(_WARN_PATH):
            dep = MCPDependency.from_dict({"name": "svc", "resolved_by": "@evil/pkg"})
        # Not constructed from user input ...
        assert dep.resolved_by is None
        # ... and not smuggled back out through to_dict / extra.
        serialized = dep.to_dict()
        assert "resolved_by" not in serialized
        assert not (dep.extra or {}).get("resolved_by")

    def test_reserved_in_explicit_extra_block(self):
        with patch(_WARN_PATH):
            dep = MCPDependency.from_dict({"name": "svc", "extra": {"resolved_by": "@evil/pkg"}})
        assert "resolved_by" not in dep.to_dict()
