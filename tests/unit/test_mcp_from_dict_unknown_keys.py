"""Acceptance tests for warn-on-dropped-keys in MCPDependency.from_dict().

Addresses #1670 (warn-on-dropped-keys; passthrough escape-hatch tracked
separately, remains needs-design).

Coverage:
1. from_dict with unknown key -> warning naming the dropped key
2. from_dict with only known keys -> no warning
3. known-key parsing and resulting values are unchanged
4. robustness: non-string dict keys do not TypeError; non-ASCII output is escaped
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

    def test_unknown_key_warning_names_dropped_key(self):
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

    def test_unknown_key_not_stored_on_instance(self):
        """Unknown key must not appear as an attribute on the resulting instance."""
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
        assert not hasattr(dep, "oauth")

    def test_to_dict_round_trip_unaffected(self):
        """to_dict() round-trip is unaffected by the presence of unknown keys on input."""
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
        assert "oauth" not in result
        assert result["name"] == "slack"
        assert result["transport"] == "http"

    def test_missing_name_still_raises(self):
        """ValueError for missing 'name' is unchanged."""
        with pytest.raises(ValueError, match="name"):
            MCPDependency.from_dict({"oauth": "value"})
