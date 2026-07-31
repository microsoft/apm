"""Unit tests for the MCP flag-conflict matrix (E1-E15).

Covers ``apm_cli.install.mcp.conflicts.validate_mcp_conflicts`` and the
``MCP_REQUIRED_FLAGS`` constant.  All tests are pure-Python with no I/O.
"""

from __future__ import annotations

import click
import pytest

from apm_cli.install.mcp.conflicts import (
    MCP_REQUIRED_FLAGS,
    validate_mcp_conflicts,
)
from apm_cli.install.mcp.spec import MCPRequestSpec

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _call(**overrides) -> None:
    """Call validate_mcp_conflicts with sensible defaults, allowing overrides."""
    spec_keys = {"mcp_name", "transport", "url", "mcp_version", "command_argv", "registry_url"}
    spec_defaults: dict = dict(
        mcp_name="my-server",
        transport=None,
        url=None,
        mcp_version=None,
        command_argv=None,
        registry_url=None,
    )
    other_defaults: dict = dict(
        packages=[],
        pre_dash_packages=[],
        env={},
        headers={},
        global_=False,
        only=None,
        update=False,
        any_transport_flag=False,
    )
    spec_kwargs = {k: overrides.pop(k, spec_defaults[k]) for k in spec_keys}
    other_defaults.update(overrides)
    validate_mcp_conflicts(spec=MCPRequestSpec(**spec_kwargs), **other_defaults)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestValidCallNoErrors:
    def test_all_defaults_no_error(self) -> None:
        """A call with all defaults should succeed without raising."""
        _call()

    def test_mcp_name_with_transport_stdio_no_url(self) -> None:
        """stdio transport without --url is valid."""
        _call(transport="stdio")

    def test_mcp_name_with_command_argv(self) -> None:
        """A stdio command alongside mcp_name (no url) is valid."""
        _call(command_argv=["npx", "my-server"])

    def test_remote_transport_with_url(self) -> None:
        """Remote transport with url (no command_argv) is valid."""
        _call(transport="http", url="https://example.com/mcp")

    def test_headers_with_url(self) -> None:
        """--header with --url is valid."""
        _call(url="https://example.com/mcp", headers={"X-Key": "value"})

    def test_registry_url_alone(self) -> None:
        """--registry without url or command_argv is valid."""
        _call(registry_url="https://registry.example.com")


# ---------------------------------------------------------------------------
# E10 - flags require --mcp
# ---------------------------------------------------------------------------


class TestE10FlagsRequireMcp:
    """Each MCP-specific flag must error when mcp_name is None."""

    def test_transport_without_mcp(self) -> None:
        with pytest.raises(click.UsageError, match=r"--transport requires --mcp"):
            _call(mcp_name=None, transport="stdio")

    def test_url_without_mcp(self) -> None:
        with pytest.raises(click.UsageError, match=r"--url requires --mcp"):
            _call(mcp_name=None, url="https://example.com")

    def test_env_without_mcp(self) -> None:
        with pytest.raises(click.UsageError, match=r"--env requires --mcp"):
            _call(mcp_name=None, env={"KEY": "val"})

    def test_header_without_mcp(self) -> None:
        with pytest.raises(click.UsageError, match=r"--header requires --mcp"):
            _call(mcp_name=None, headers={"X-Foo": "bar"})

    def test_mcp_version_without_mcp(self) -> None:
        with pytest.raises(click.UsageError, match=r"--mcp-version requires --mcp"):
            _call(mcp_name=None, mcp_version="1.2.3")

    def test_registry_without_mcp(self) -> None:
        with pytest.raises(click.UsageError, match=r"--registry requires --mcp"):
            _call(mcp_name=None, registry_url="https://registry.example.com")

    def test_command_argv_without_mcp_allowed(self) -> None:
        """Legacy install behaviour: post-`--` command without --mcp is allowed."""
        _call(mcp_name=None, command_argv=["npx", "server"])  # must not raise

    def test_no_flags_without_mcp_allowed(self) -> None:
        """No flags set with mcp_name=None is a plain install, no error."""
        _call(mcp_name=None)


# ---------------------------------------------------------------------------
# E7 - empty mcp_name
# ---------------------------------------------------------------------------


class TestE7EmptyMcpName:
    def test_empty_string_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"MCP name cannot be empty"):
            _call(mcp_name="")


# ---------------------------------------------------------------------------
# E8 - mcp_name starts with '-'
# ---------------------------------------------------------------------------


class TestE8McpNameStartsWithDash:
    def test_leading_dash_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"MCP name cannot start with"):
            _call(mcp_name="-bad-name")

    def test_double_dash_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"MCP name cannot start with"):
            _call(mcp_name="--flag")

    def test_valid_name_no_error(self) -> None:
        _call(mcp_name="good-name")


# ---------------------------------------------------------------------------
# E1 - positional packages mixed with --mcp
# ---------------------------------------------------------------------------


class TestE1PositionalPackagesMixedWithMcp:
    def test_pre_dash_packages_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"cannot mix --mcp with positional packages"):
            _call(pre_dash_packages=["some-pkg"])

    def test_multiple_pre_dash_packages_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"cannot mix --mcp with positional packages"):
            _call(pre_dash_packages=["pkg-a", "pkg-b"])

    def test_empty_pre_dash_packages_ok(self) -> None:
        _call(pre_dash_packages=[])


# ---------------------------------------------------------------------------
# E2 - --global not supported for MCP entries
# ---------------------------------------------------------------------------


class TestE2GlobalNotSupportedForMcp:
    def test_global_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"--global is not supported"):
            _call(global_=True)

    def test_not_global_ok(self) -> None:
        _call(global_=False)


# ---------------------------------------------------------------------------
# E3 - --only apm conflicts with --mcp
# ---------------------------------------------------------------------------


class TestE3OnlyApmConflictsWithMcp:
    def test_only_apm_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"cannot use --only apm with --mcp"):
            _call(only="apm")

    def test_only_other_value_ok(self) -> None:
        _call(only="mcp")

    def test_only_none_ok(self) -> None:
        _call(only=None)


# ---------------------------------------------------------------------------
# E4 - transport selection flags don't apply to MCP
# ---------------------------------------------------------------------------


class TestE4TransportSelectionFlags:
    def test_use_ssh_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"transport selection flags"):
            _call(any_transport_flag=True)

    def test_use_https_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"transport selection flags"):
            _call(any_transport_flag=True)

    def test_allow_protocol_fallback_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"transport selection flags"):
            _call(any_transport_flag=True)

    def test_none_set_ok(self) -> None:
        _call(any_transport_flag=False)


# ---------------------------------------------------------------------------
# E5 - --update is for refreshing
# ---------------------------------------------------------------------------


class TestE5UpdateFlag:
    def test_update_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"apm update"):
            _call(update=True)

    def test_no_update_ok(self) -> None:
        _call(update=False)


# ---------------------------------------------------------------------------
# E9 - --header requires --url
# ---------------------------------------------------------------------------


class TestE9HeaderRequiresUrl:
    def test_header_without_url_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"--header requires --url"):
            _call(headers={"X-Key": "value"}, url=None)

    def test_header_with_url_ok(self) -> None:
        _call(headers={"X-Key": "value"}, url="https://example.com")

    def test_empty_headers_no_url_ok(self) -> None:
        _call(headers={}, url=None)


# ---------------------------------------------------------------------------
# E11 - --url with stdio command
# ---------------------------------------------------------------------------


class TestE11UrlWithStdioCommand:
    def test_url_and_command_argv_raises(self) -> None:
        with pytest.raises(
            click.UsageError, match=r"cannot specify both --url and a stdio command"
        ):
            _call(url="https://example.com", command_argv=["npx", "server"])

    def test_url_without_command_ok(self) -> None:
        _call(url="https://example.com", command_argv=None)

    def test_command_without_url_ok(self) -> None:
        _call(url=None, command_argv=["npx", "server"])


# ---------------------------------------------------------------------------
# E12 - --transport stdio with --url
# ---------------------------------------------------------------------------


class TestE12StdioTransportWithUrl:
    def test_stdio_with_url_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"stdio transport doesn't accept --url"):
            _call(transport="stdio", url="https://example.com")

    def test_stdio_without_url_ok(self) -> None:
        _call(transport="stdio", url=None)

    def test_http_with_url_ok(self) -> None:
        _call(transport="http", url="https://example.com")


# ---------------------------------------------------------------------------
# E13 - remote transports with stdio command
# ---------------------------------------------------------------------------


class TestE13RemoteTransportWithStdioCommand:
    @pytest.mark.parametrize("transport", ["http", "sse", "streamable-http"])
    def test_remote_transport_with_command_raises(self, transport: str) -> None:
        with pytest.raises(click.UsageError, match=r"remote transports don't accept stdio command"):
            _call(transport=transport, command_argv=["node", "server.js"])

    def test_stdio_transport_with_command_ok(self) -> None:
        _call(transport="stdio", command_argv=["node", "server.js"])

    def test_remote_transport_without_command_ok(self) -> None:
        _call(transport="http", url="https://example.com")


# ---------------------------------------------------------------------------
# E14 - --env with --url and no command
# ---------------------------------------------------------------------------


class TestE14EnvWithUrlNoCommand:
    def test_env_with_url_no_command_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"--env applies to stdio MCPs"):
            _call(env={"MY_VAR": "value"}, url="https://example.com", command_argv=None)

    def test_env_with_command_ok(self) -> None:
        """--env is fine when there is a stdio command."""
        _call(env={"MY_VAR": "value"}, url=None, command_argv=["npx", "server"])

    def test_env_without_url_or_command_ok(self) -> None:
        """--env alone (registry-resolved MCP) is fine."""
        _call(env={"MY_VAR": "value"}, url=None, command_argv=None)

    def test_empty_env_with_url_ok(self) -> None:
        _call(env={}, url="https://example.com", command_argv=None)


# ---------------------------------------------------------------------------
# E15 - --registry only applies to registry-resolved entries
# ---------------------------------------------------------------------------


class TestE15RegistryOnlyForRegistryEntries:
    def test_registry_with_url_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"--registry only applies"):
            _call(registry_url="https://reg.example.com", url="https://example.com")

    def test_registry_with_command_argv_raises(self) -> None:
        with pytest.raises(click.UsageError, match=r"--registry only applies"):
            _call(registry_url="https://reg.example.com", command_argv=["node", "s.js"])

    def test_registry_without_url_or_command_ok(self) -> None:
        _call(registry_url="https://reg.example.com", url=None, command_argv=None)


# ---------------------------------------------------------------------------
# MCP_REQUIRED_FLAGS constant
# ---------------------------------------------------------------------------


class TestMcpRequiredFlagsConstant:
    def test_is_tuple(self) -> None:
        assert isinstance(MCP_REQUIRED_FLAGS, tuple)

    def test_contains_expected_pairs(self) -> None:
        expected: list[tuple[str, str]] = [
            ("transport", "--transport"),
            ("url", "--url"),
            ("env", "--env"),
            ("header", "--header"),
            ("mcp_version", "--mcp-version"),
        ]
        for pair in expected:
            assert pair in MCP_REQUIRED_FLAGS, f"{pair} not found in MCP_REQUIRED_FLAGS"

    def test_all_pairs_are_two_element_tuples(self) -> None:
        for item in MCP_REQUIRED_FLAGS:
            assert len(item) == 2, f"Expected 2-element tuple, got {item!r}"
