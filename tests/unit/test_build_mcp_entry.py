"""Tests for the pure ``_build_mcp_entry`` builder.

Coverage focuses on the routing matrix (stdio vs remote vs registry) and
the round-trip through :class:`MCPDependency.from_dict` /
:meth:`from_string` for validation.
"""

import pytest

from apm_cli.commands.install import _build_mcp_entry
from apm_cli.models.dependency.mcp import MCPDependency


def _build(name="foo", **kw):
    defaults = dict(
        transport=None,
        url=None,
        env=None,
        headers=None,
        version=None,
        command_argv=(),
        registry_url=None,
    )
    defaults.update(kw)
    return _build_mcp_entry(name, **defaults)


class TestStdioShape:
    def test_stdio_command_only(self):
        entry, self_def = _build(command_argv=("npx", "-y", "server-foo"))
        assert self_def is True
        assert entry["name"] == "foo"
        assert entry["registry"] is False
        assert entry["transport"] == "stdio"
        assert entry["command"] == "npx"
        assert entry["args"] == ["-y", "server-foo"]
        assert "env" not in entry  # empty env omitted

    def test_stdio_command_with_env(self):
        entry, _ = _build(
            command_argv=("python", "server.py"),
            env={"FOO": "bar", "BAZ": "qux"},
        )
        assert entry["env"] == {"FOO": "bar", "BAZ": "qux"}

    def test_stdio_single_arg(self):
        entry, _ = _build(command_argv=("docker",))
        assert entry["command"] == "docker"
        assert "args" not in entry  # single command -> no args list

    def test_stdio_headers_ignored(self):
        # Headers passed in a stdio context are silently dropped (E13/E9
        # would catch this at the CLI layer; the builder is pure).
        entry, _ = _build(command_argv=("srv",), headers={"X-Auth": "tok"})
        assert "headers" not in entry


class TestRemoteShape:
    def test_remote_url_default_http(self):
        entry, self_def = _build(url="https://example.com/mcp")
        assert self_def is True
        assert entry["name"] == "foo"
        assert entry["registry"] is False
        assert entry["transport"] == "http"
        assert entry["url"] == "https://example.com/mcp"
        assert "headers" not in entry

    def test_remote_url_explicit_sse(self):
        entry, _ = _build(url="https://x/y", transport="sse")
        assert entry["transport"] == "sse"

    def test_remote_url_explicit_http(self):
        entry, _ = _build(url="http://x/y", transport="http")
        assert entry["transport"] == "http"

    def test_remote_with_headers(self):
        entry, _ = _build(
            url="https://x/y",
            headers={"X-Auth": "token", "Accept": "application/json"},
        )
        assert entry["headers"] == {"X-Auth": "token", "Accept": "application/json"}

    def test_remote_env_passed_through(self):
        # Builder is pure; CLI layer (E14) flags --env+--url. Builder
        # does NOT drop env -- but the CLI never passes it in a remote
        # call.  Verify builder shape: env is omitted because we did
        # not include the stdio routing condition.
        entry, _ = _build(url="https://x/y")
        assert "env" not in entry


class TestRegistryShape:
    def test_bare_string(self):
        entry, self_def = _build(name="io.github.x/y")
        assert self_def is False
        assert entry == "io.github.x/y"

    def test_with_version_overlay(self):
        entry, self_def = _build(name="srv", version="1.2.3")
        assert self_def is False
        assert entry == {"name": "srv", "version": "1.2.3"}

    def test_with_transport_overlay(self):
        entry, self_def = _build(name="srv", transport="stdio")
        assert self_def is False
        assert entry == {"name": "srv", "transport": "stdio"}

    def test_with_version_and_transport(self):
        entry, _ = _build(name="srv", version="2.0.0", transport="http")
        assert entry["name"] == "srv"
        assert entry["version"] == "2.0.0"
        assert entry["transport"] == "http"


class TestValidationRoundtrip:
    def test_valid_stdio_passes(self):
        entry, _ = _build(command_argv=("npx", "srv"))
        # Re-parse to confirm the entry is round-trippable.
        dep = MCPDependency.from_dict(entry)
        assert dep.name == "foo"

    def test_valid_remote_passes(self):
        entry, _ = _build(url="https://example.com/api")
        dep = MCPDependency.from_dict(entry)
        assert dep.url == "https://example.com/api"

    def test_invalid_name_rejected(self):
        with pytest.raises(ValueError, match="Invalid MCP dependency name"):
            _build(name="bad name with spaces", command_argv=("x",))

    def test_invalid_url_scheme_rejected(self):
        with pytest.raises(ValueError, match="use http:// or https://"):
            _build(url="file:///etc/passwd")

    def test_header_crlf_rejected(self):
        with pytest.raises(ValueError, match="control characters"):
            _build(url="https://x/y", headers={"X-A": "v\r\nInjected: 1"})

    def test_command_traversal_rejected(self):
        with pytest.raises(ValueError, match=r"'\.\.' path segments"):
            _build(command_argv=("../../../bin/sh",))

    def test_empty_name_rejected(self):
        with pytest.raises(ValueError):
            _build(name="", command_argv=("x",))


class TestExplicitTransportOverride:
    def test_explicit_transport_overrides_remote_inference(self):
        entry, _ = _build(url="https://x/y", transport="sse")
        assert entry["transport"] == "sse"


class TestRegistryUrlOverlay:
    """``registry_url`` (--registry CLI flag) is persisted to the entry's
    ``registry:`` field for reproducible installs across machines."""

    def test_registry_url_alone_promotes_to_dict(self):
        entry, self_def = _build(name="srv", registry_url="https://r.example.com")
        assert self_def is False
        assert entry == {"name": "srv", "registry": "https://r.example.com"}

    def test_registry_url_with_version(self):
        entry, _ = _build(name="srv", version="1.0.0", registry_url="https://r.example.com")
        assert entry == {
            "name": "srv",
            "version": "1.0.0",
            "registry": "https://r.example.com",
        }

    def test_registry_url_with_transport(self):
        entry, _ = _build(name="srv", transport="stdio", registry_url="https://r.example.com")
        assert entry == {
            "name": "srv",
            "transport": "stdio",
            "registry": "https://r.example.com",
        }

    def test_no_registry_url_keeps_bare_string(self):
        entry, _ = _build(name="srv")
        assert entry == "srv"
