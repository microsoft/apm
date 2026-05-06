"""Tests for MCP overlay functionality: MCPDependency model, self-defined server
info building, overlay application, and install flow integration."""

from unittest.mock import MagicMock, patch  # noqa: F401

import pytest

from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.apm_package import MCPDependency


# ---------------------------------------------------------------------------
# MCPDependency Model
# ---------------------------------------------------------------------------
class TestMCPDependencyModel:
    def test_from_string(self):
        dep = MCPDependency.from_string("io.github.github/github-mcp-server")
        assert dep.name == "io.github.github/github-mcp-server"
        assert dep.transport is None
        assert dep.env is None
        assert dep.args is None
        assert dep.version is None
        assert dep.package is None
        assert dep.headers is None
        assert dep.tools is None
        assert dep.url is None
        assert dep.command is None
        assert dep.is_registry_resolved is True
        assert dep.is_self_defined is False

    def test_from_dict_minimal(self):
        dep = MCPDependency.from_dict({"name": "my-server"})
        assert dep.name == "my-server"
        assert dep.transport is None
        assert dep.env is None

    def test_from_dict_full_overlay(self):
        dep = MCPDependency.from_dict(
            {
                "name": "full-server",
                "transport": "stdio",
                "env": {"KEY": "value"},
                "args": ["--flag"],
                "version": "1.2.3",
                "package": "npm",
                "headers": {"X-Auth": "token"},
                "tools": ["read", "write"],
            }
        )
        assert dep.name == "full-server"
        assert dep.transport == "stdio"
        assert dep.env == {"KEY": "value"}
        assert dep.args == ["--flag"]
        assert dep.version == "1.2.3"
        assert dep.package == "npm"
        assert dep.headers == {"X-Auth": "token"}
        assert dep.tools == ["read", "write"]

    def test_from_dict_self_defined_http(self):
        dep = MCPDependency.from_dict(
            {
                "name": "acme-kb",
                "registry": False,
                "transport": "http",
                "url": "http://localhost:8080",
            }
        )
        assert dep.is_self_defined is True
        assert dep.is_registry_resolved is False
        assert dep.transport == "http"
        assert dep.url == "http://localhost:8080"

    def test_from_dict_self_defined_stdio(self):
        dep = MCPDependency.from_dict(
            {
                "name": "my-local",
                "registry": False,
                "transport": "stdio",
                "command": "my-mcp-server",
            }
        )
        assert dep.is_self_defined is True
        assert dep.transport == "stdio"
        assert dep.command == "my-mcp-server"

    def test_from_dict_legacy_type_mapped_to_transport(self):
        dep = MCPDependency.from_dict({"name": "x", "type": "stdio"})
        assert dep.transport == "stdio"

    def test_validate_self_defined_missing_transport(self):
        with pytest.raises(ValueError, match="requires 'transport'"):
            MCPDependency.from_dict({"name": "x", "registry": False})

    def test_validate_self_defined_http_missing_url(self):
        with pytest.raises(ValueError, match="requires 'url'"):
            MCPDependency.from_dict(
                {
                    "name": "x",
                    "registry": False,
                    "transport": "http",
                }
            )

    def test_validate_self_defined_stdio_missing_command(self):
        with pytest.raises(ValueError, match="requires 'command'"):
            MCPDependency.from_dict(
                {
                    "name": "x",
                    "registry": False,
                    "transport": "stdio",
                }
            )

    def test_validate_self_defined_stdio_shell_string_command_rejected(self):
        """Reject command containing whitespace when args is empty (the lirantal trap, #122).

        Per schema, 'command' is the binary path and 'args' is the list of arguments.
        APM does not whitespace-split 'command'. Silently accepting "npx pkg-name" would
        either mis-execute or rely on downstream shell-splitting.
        """
        with pytest.raises(ValueError, match="must be a single binary path"):
            MCPDependency.from_dict(
                {
                    "name": "lirantal-trap",
                    "registry": False,
                    "transport": "stdio",
                    "command": "npx -y mcp-server-nodejs-api-docs",
                }
            )

    def test_validate_stdio_shell_string_error_includes_fix_it(self):
        """Error message must include the corrected command/args shape."""
        try:
            MCPDependency.from_dict(
                {
                    "name": "fix-it",
                    "registry": False,
                    "transport": "stdio",
                    "command": "npx -y some-pkg",
                }
            )
        except ValueError as e:
            msg = str(e)
            assert "command: npx" in msg
            assert '"-y"' in msg
            assert '"some-pkg"' in msg
        else:
            pytest.fail("expected ValueError")

    def test_validate_stdio_command_with_whitespace_but_args_present_ok(self):
        """If args is explicitly provided, do not second-guess the command shape.

        A path with spaces is unusual but legal (e.g. /opt/My App/bin/server) and the
        author has taken responsibility for shape by providing args.
        """
        dep = MCPDependency.from_dict(
            {
                "name": "spaced-path",
                "registry": False,
                "transport": "stdio",
                "command": "/opt/My App/server",
                "args": ["--port", "3000"],
            }
        )
        assert dep.command == "/opt/My App/server"
        assert dep.args == ["--port", "3000"]

    def test_validate_stdio_single_token_command_ok(self):
        """The canonical shape (command=binary, args=list) must keep working."""
        dep = MCPDependency.from_dict(
            {
                "name": "canonical",
                "registry": False,
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "mcp-server-nodejs-api-docs"],
            }
        )
        assert dep.command == "npx"
        assert dep.args == ["-y", "mcp-server-nodejs-api-docs"]

    def test_validate_stdio_command_with_tabs_also_rejected(self):
        """Whitespace check covers tabs, not just spaces."""
        with pytest.raises(ValueError, match="must be a single binary path"):
            MCPDependency.from_dict(
                {
                    "name": "tabbed",
                    "registry": False,
                    "transport": "stdio",
                    "command": "npx\t-y\tpkg",
                }
            )

    def test_validate_stdio_tab_split_fix_it_suggestion_correct(self):
        """Fix-it suggestion must split on any whitespace, not just U+0020.

        Regression for PR #809 review: ``.partition(' ')`` produced
        ``command: 'npx\\t-y\\tpkg', args: []`` for tab-separated input,
        which is itself invalid. Replaced with ``split(maxsplit=1)``.
        """
        with pytest.raises(ValueError) as exc:
            MCPDependency.from_dict(
                {
                    "name": "tab-fixit",
                    "registry": False,
                    "transport": "stdio",
                    "command": "npx\t-y\tpkg",
                }
            )
        msg = str(exc.value)
        assert "command: npx" in msg, msg
        assert 'args: ["-y", "pkg"]' in msg, msg

    def test_validate_stdio_error_does_not_leak_full_command(self):
        """Credential-leak guard: the conversational ``Got:`` line must not echo
        the raw command verbatim.

        Regression for PR #809 panel review (M1): ``Got: command={self.command!r}``
        leaked tokens like ``--token=ghp_...`` to stderr / CI logs in a redundant
        framing. The ``Got:`` line must only echo the first token plus a count.

        Note: the structured ``Did you mean:`` suggestion still surfaces the
        original tokens because that is the user's literal copy-paste recovery
        path -- the user already has the secret in their YAML. The redundant
        verbatim ``Got:`` echo was the gratuitous leak surface.
        """
        with pytest.raises(ValueError) as exc:
            MCPDependency.from_dict(
                {
                    "name": "leaky",
                    "registry": False,
                    "transport": "stdio",
                    "command": "npx --token=ghp_SUPERSECRETTOKENVALUE mcp-server",
                }
            )
        msg = str(exc.value)
        assert "command='npx' (2 additional args)" in msg, msg
        # The ``Got:`` framing must not contain the raw token.
        got_segment = msg.split("Fix:")[0]
        assert "ghp_SUPERSECRETTOKENVALUE" not in got_segment, (
            f"`Got:` framing leaked credential token: {got_segment}"
        )

    def test_validate_stdio_explicit_empty_args_with_spaced_path_ok(self):
        """``args: []`` is a deliberate 'no extra args' signal and must be accepted.

        Regression for PR #809 panel review (M2): the predicate ``not self.args``
        treated ``[]`` as falsy and rejected legitimate input like a path with
        spaces paired with no arguments. Predicate is now ``self.args is None``.
        """
        dep = MCPDependency.from_dict(
            {
                "name": "spaced-path-no-args",
                "registry": False,
                "transport": "stdio",
                "command": "/opt/My App/server",
                "args": [],
            }
        )
        assert dep.command == "/opt/My App/server"
        assert dep.args == []

    def test_validate_stdio_whitespace_only_command_clear_error(self):
        """Whitespace-only command must produce a clear, non-degenerate error.

        Regression for PR #809 panel review (S1): ``command: "  "`` previously
        produced ``Did you mean: command: , args: []`` (nonsensical). Now emits
        a dedicated empty/whitespace-only message.
        """
        with pytest.raises(ValueError, match="empty or whitespace-only"):
            MCPDependency.from_dict(
                {
                    "name": "blank-cmd",
                    "registry": False,
                    "transport": "stdio",
                    "command": "   ",
                }
            )

    def test_validate_stdio_error_uses_multiline_cargo_style_format(self):
        """Error must render as multi-line Cargo-style for terminal scannability.

        Regression for PR #809 panel review (cli-log + devx follow-up): the
        original 350-char single-line error defeated the newspaper test and
        terminal URL detection. Now uses field/rule/got/fix/see structure.
        """
        with pytest.raises(ValueError) as exc:
            MCPDependency.from_dict(
                {
                    "name": "multiline",
                    "registry": False,
                    "transport": "stdio",
                    "command": "npx -y pkg",
                }
            )
        msg = str(exc.value)
        # Each labeled line on its own row, in this order.
        lines = msg.split("\n")
        assert len(lines) >= 6, f"expected multi-line format, got: {msg}"
        assert lines[0].startswith("'command' contains whitespace"), lines[0]
        assert any(line.lstrip().startswith("Rule:") for line in lines), msg
        assert any(line.lstrip().startswith("Got:") for line in lines), msg
        assert any(line.lstrip().startswith("Fix:") for line in lines), msg
        assert any(line.lstrip().startswith("See:") for line in lines), msg
        # URL must sit on its own line for terminal click-through.
        url_lines = [l for l in lines if "https://" in l]  # noqa: E741
        assert len(url_lines) == 1 and url_lines[0].count(" ") <= 4, url_lines

    def test_repr_redacts_command_to_avoid_leaking_credentials(self):
        """``repr(dep)`` must not leak full command (which may carry tokens).

        Regression for PR #809 panel review (sec MEDIUM follow-up): pre-existing
        ``__repr__`` echoed ``command={self.command!r}`` verbatim while
        carefully redacting ``env`` and ``headers``. Now shows first token only.
        """
        dep = MCPDependency(
            name="leaky-repr",
            registry=False,
            transport="stdio",
            command="npx --token=ghp_REPRSECRETTOKEN mcp-server",
            args=["--quiet"],
        )
        rep = repr(dep)
        assert "ghp_REPRSECRETTOKEN" not in rep, rep
        assert "command='npx'" in rep, rep

    def test_validate_stdio_non_string_command_rejected(self):
        """Non-string ``command`` (e.g. YAML list) must raise a clean ValueError.

        Regression for PR #809 panel review (S2): ``command: ["npx", "-y", "x"]``
        bypassed the whitespace check and crashed in ``validate_path_segments``
        with an unhandled ``AttributeError`` (``.replace`` on list).
        """
        with pytest.raises(ValueError, match="'command' must be a string"):
            MCPDependency.from_dict(
                {
                    "name": "list-cmd",
                    "registry": False,
                    "transport": "stdio",
                    "command": ["npx", "-y", "evil"],
                }
            )

    def test_to_dict_roundtrip(self):
        dep = MCPDependency(
            name="rt-server",
            transport="sse",
            env={"A": "1"},
            args={"org": "my-org"},
            version="2.0.0",
            package="npm",
            headers={"X-H": "v"},
            tools=["tool1"],
            url="http://example.com",
            command="cmd",
        )
        d = dep.to_dict()
        assert d["name"] == "rt-server"
        assert d["transport"] == "sse"
        assert d["env"] == {"A": "1"}
        assert d["args"] == {"org": "my-org"}
        assert d["version"] == "2.0.0"
        assert d["package"] == "npm"
        assert d["headers"] == {"X-H": "v"}
        assert d["tools"] == ["tool1"]
        assert d["url"] == "http://example.com"
        assert d["command"] == "cmd"

        dep2 = MCPDependency.from_dict(d)
        assert dep2.name == dep.name
        assert dep2.transport == dep.transport
        assert dep2.env == dep.env

    def test_to_dict_excludes_none_fields(self):
        dep = MCPDependency.from_string("simple-server")
        d = dep.to_dict()
        assert d == {"name": "simple-server"}

    def test_args_accepts_list(self):
        dep = MCPDependency.from_dict({"name": "x", "args": ["--port", "8080"]})
        assert dep.args == ["--port", "8080"]
        assert isinstance(dep.args, list)

    def test_args_accepts_dict(self):
        dep = MCPDependency.from_dict({"name": "x", "args": {"org": "my-org"}})
        assert dep.args == {"org": "my-org"}
        assert isinstance(dep.args, dict)

    # -- __str__ / __repr__ --------------------------------------------------

    def test_str_with_transport(self):
        dep = MCPDependency(name="my-srv", transport="stdio")
        assert str(dep) == "my-srv (stdio)"

    def test_str_without_transport(self):
        dep = MCPDependency(name="my-srv")
        assert str(dep) == "my-srv"

    def test_repr_does_not_leak_env(self):
        dep = MCPDependency(
            name="leaky",
            transport="stdio",
            env={"SECRET": "s3cret"},
            headers={"Authorization": "Bearer token"},
        )
        r = repr(dep)
        assert "s3cret" not in r
        assert "Bearer" not in r
        assert "***" in r
        assert "env=" in r
        assert "headers=" in r
        assert r.startswith("MCPDependency(")
        assert "name='leaky'" in r
        assert "transport='stdio'" in r

    # -- transport validation ------------------------------------------------

    def test_validate_invalid_transport_rejected(self):
        with pytest.raises(ValueError, match="unsupported transport"):
            MCPDependency.from_dict(
                {"name": "x", "registry": False, "transport": "foo", "command": "cmd"}
            )

    def test_validate_valid_transports_accepted(self):
        for t in ("stdio", "sse", "http", "streamable-http"):
            dep = MCPDependency(name="x", transport=t)
            # Should not raise for registry-resolved deps (no extra required fields)
            dep.validate()


# ---------------------------------------------------------------------------
# Universal hardening checks (strict=False AND strict=True)
# ---------------------------------------------------------------------------
class TestMCPDependencyHardening:
    # -- NAME allowlist regex -----------------------------------------------

    @pytest.mark.parametrize(
        "name",
        [
            "@scope/name",
            "name-dash",
            "name.dot",
            "name_under",
            "name123",
            "a",
            "org/repo",
            "io.github.github/github-mcp-server",
            "microsoft/azure-devops-mcp",
            "_corp-analytics",
            "_internal",
        ],
    )
    def test_name_regex_accepts_valid(self, name):
        MCPDependency.from_string(name)  # must not raise

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "-leading",
            ".leading",
            "a" * 129,
            "with space",
            "with\x00null",
            "with\nnewline",
            "with;semi",
            "with$dollar",
            "n\u00e4me",  # non-ASCII
        ],
    )
    def test_name_regex_rejects_invalid(self, name):
        with pytest.raises(ValueError):
            MCPDependency.from_string(name)

    # -- URL scheme allowlist -----------------------------------------------

    @pytest.mark.parametrize("url", ["http://x", "https://x"])
    def test_url_scheme_accepts_http_https(self, url):
        dep = MCPDependency(name="srv", url=url)
        dep.validate(strict=False)

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://x",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "gopher://x",
            "//x",  # scheme-less
        ],
    )
    def test_url_scheme_rejects_others(self, url):
        dep = MCPDependency(name="srv", url=url)
        with pytest.raises(ValueError, match="use http:// or https://"):
            dep.validate(strict=False)

    # -- Header CRLF rejection ----------------------------------------------

    def test_headers_normal_pass(self):
        dep = MCPDependency(name="srv", headers={"Authorization": "Bearer xyz"})
        dep.validate(strict=False)

    @pytest.mark.parametrize(
        "key,val",
        [
            ("X-Bad\rKey", "v"),
            ("X-Bad\nKey", "v"),
            ("X-OK", "val\rinjection"),
            ("X-OK", "val\ninjection"),
        ],
    )
    def test_headers_crlf_rejected(self, key, val):
        dep = MCPDependency(name="srv", headers={key: val})
        with pytest.raises(ValueError, match="control characters"):
            dep.validate(strict=False)

    # -- Command path-traversal check ---------------------------------------

    @pytest.mark.parametrize(
        "cmd", ["npx", "/usr/bin/node", "python3", "./bin/my-server", "./server"]
    )
    def test_command_safe_paths_pass(self, cmd):
        dep = MCPDependency(name="srv", command=cmd)
        dep.validate(strict=False)

    @pytest.mark.parametrize("cmd", ["../evil", "bin/../../../sbin/x", r"a\..\b"])
    def test_command_traversal_rejected(self, cmd):
        dep = MCPDependency(name="srv", command=cmd)
        with pytest.raises(ValueError, match=r"'\.\.' path segments"):
            dep.validate(strict=False)

    # -- from_string now validates ------------------------------------------

    def test_from_string_passes_for_valid_name(self):
        dep = MCPDependency.from_string("valid-name")
        assert dep.name == "valid-name"

    def test_from_string_fails_for_invalid_name(self):
        with pytest.raises(ValueError, match="Invalid MCP dependency name"):
            MCPDependency.from_string("bad name with space")

    # -- from_dict gating ---------------------------------------------------

    def test_from_dict_registry_runs_universal_only(self):
        # Registry-resolved (registry not False): strict=False only.
        # Valid name + valid url + no command should pass even though the
        # strict=True command-required check would normally fire.
        dep = MCPDependency.from_dict(
            {
                "name": "io.github.github/github-mcp-server",
                "url": "https://example.com",
            }
        )
        assert dep.name == "io.github.github/github-mcp-server"

    def test_from_dict_registry_rejects_universal_violations(self):
        with pytest.raises(ValueError, match="Invalid MCP dependency name"):
            MCPDependency.from_dict({"name": "bad name"})

    def test_from_dict_self_defined_runs_strict_checks(self):
        # registry=False with stdio transport but no command -> existing
        # strict=True check still fires.
        with pytest.raises(ValueError, match="requires 'command'"):
            MCPDependency.from_dict(
                {
                    "name": "x",
                    "registry": False,
                    "transport": "stdio",
                }
            )


# ---------------------------------------------------------------------------
# _build_self_defined_server_info
# ---------------------------------------------------------------------------
class TestBuildSelfDefinedServerInfo:
    def test_http_transport_builds_remote(self):
        dep = MCPDependency(
            name="http-srv",
            registry=False,
            transport="http",
            url="http://example.com",
        )
        result = MCPIntegrator._build_self_defined_info(dep)
        assert "remotes" in result
        assert len(result["remotes"]) == 1
        assert result["remotes"][0]["url"] == "http://example.com"
        assert result["remotes"][0]["transport_type"] == "http"
        assert "packages" not in result

    def test_sse_transport_builds_remote(self):
        dep = MCPDependency(
            name="sse-srv",
            registry=False,
            transport="sse",
            url="http://example.com/sse",
        )
        result = MCPIntegrator._build_self_defined_info(dep)
        assert "remotes" in result
        assert result["remotes"][0]["transport_type"] == "sse"
        assert result["remotes"][0]["url"] == "http://example.com/sse"

    def test_stdio_transport_builds_package(self):
        dep = MCPDependency(
            name="stdio-srv",
            registry=False,
            transport="stdio",
            command="my-cmd",
        )
        result = MCPIntegrator._build_self_defined_info(dep)
        assert "packages" in result
        assert len(result["packages"]) == 1
        assert result["packages"][0]["runtime_hint"] == "my-cmd"
        assert "remotes" not in result

    def test_http_with_headers(self):
        dep = MCPDependency(
            name="hdr-srv",
            registry=False,
            transport="http",
            url="http://example.com",
            headers={"Authorization": "Bearer token"},
        )
        result = MCPIntegrator._build_self_defined_info(dep)
        headers = result["remotes"][0]["headers"]
        assert len(headers) == 1
        assert headers[0] == {"name": "Authorization", "value": "Bearer token"}

    def test_stdio_with_env(self):
        dep = MCPDependency(
            name="env-srv",
            registry=False,
            transport="stdio",
            command="x",
            env={"KEY": "val"},
        )
        result = MCPIntegrator._build_self_defined_info(dep)
        env_vars = result["packages"][0]["environment_variables"]
        assert len(env_vars) == 1
        assert env_vars[0]["name"] == "KEY"

    def test_stdio_with_list_args(self):
        dep = MCPDependency(
            name="args-srv",
            registry=False,
            transport="stdio",
            command="npx",
            args=["-y", "pkg"],
        )
        result = MCPIntegrator._build_self_defined_info(dep)
        runtime_args = result["packages"][0]["runtime_arguments"]
        assert len(runtime_args) == 2
        assert runtime_args[0]["value_hint"] == "-y"
        assert runtime_args[1]["value_hint"] == "pkg"

    def test_tools_override_embedded(self):
        dep = MCPDependency(
            name="tools-srv",
            registry=False,
            transport="stdio",
            command="cmd",
            tools=["read", "write"],
        )
        result = MCPIntegrator._build_self_defined_info(dep)
        assert result["_apm_tools_override"] == ["read", "write"]

    def test_no_tools_no_key(self):
        dep = MCPDependency(
            name="no-tools",
            registry=False,
            transport="stdio",
            command="cmd",
        )
        result = MCPIntegrator._build_self_defined_info(dep)
        assert "_apm_tools_override" not in result


# ---------------------------------------------------------------------------
# _apply_mcp_overlay
# ---------------------------------------------------------------------------
class TestApplyMCPOverlay:
    def test_transport_stdio_removes_remotes(self):
        cache = {
            "srv": {
                "packages": [{"registry_name": "npm", "runtime_hint": "npx"}],
                "remotes": [{"url": "http://x", "transport_type": "http"}],
            }
        }
        dep = MCPDependency(name="srv", transport="stdio")
        MCPIntegrator._apply_overlay(cache, dep)
        assert "remotes" not in cache["srv"]
        assert "packages" in cache["srv"]

    def test_transport_http_removes_packages(self):
        cache = {
            "srv": {
                "packages": [{"registry_name": "npm", "runtime_hint": "npx"}],
                "remotes": [{"url": "http://x", "transport_type": "http"}],
            }
        }
        dep = MCPDependency(name="srv", transport="http")
        MCPIntegrator._apply_overlay(cache, dep)
        assert "packages" not in cache["srv"]
        assert "remotes" in cache["srv"]

    def test_package_type_filters(self):
        cache = {
            "srv": {
                "packages": [
                    {"registry_name": "npm", "runtime_hint": "npx"},
                    {"registry_name": "pypi", "runtime_hint": "pip"},
                ],
            }
        }
        dep = MCPDependency(name="srv", package="npm")
        MCPIntegrator._apply_overlay(cache, dep)
        assert len(cache["srv"]["packages"]) == 1
        assert cache["srv"]["packages"][0]["registry_name"] == "npm"

    def test_headers_merged_into_remotes(self):
        cache = {
            "srv": {
                "remotes": [{"url": "http://x", "headers": []}],
            }
        }
        dep = MCPDependency(name="srv", headers={"X-Custom": "val"})
        MCPIntegrator._apply_overlay(cache, dep)
        headers = cache["srv"]["remotes"][0]["headers"]
        assert len(headers) == 1
        assert headers[0] == {"name": "X-Custom", "value": "val"}

    def test_tools_embedded(self):
        cache = {"srv": {"packages": [{"registry_name": "npm"}]}}
        dep = MCPDependency(name="srv", tools=["repos"])
        MCPIntegrator._apply_overlay(cache, dep)
        assert cache["srv"]["_apm_tools_override"] == ["repos"]

    def test_no_overlay_no_change(self):
        original = {"packages": [{"registry_name": "npm", "runtime_hint": "npx"}]}
        cache = {"srv": original.copy()}
        dep = MCPDependency(name="srv")
        MCPIntegrator._apply_overlay(cache, dep)
        assert cache["srv"]["packages"] == original["packages"]

    def test_missing_server_info_noop(self):
        cache = {}
        dep = MCPDependency(name="nonexistent", transport="stdio")
        # Should not raise
        MCPIntegrator._apply_overlay(cache, dep)
        assert cache == {}

    def test_args_list_merged_into_packages(self):
        cache = {
            "srv": {
                "packages": [{"registry_name": "npm", "runtime_hint": "npx"}],
            }
        }
        dep = MCPDependency(name="srv", args=["--org", "acme"])
        MCPIntegrator._apply_overlay(cache, dep)
        rt_args = cache["srv"]["packages"][0]["runtime_arguments"]
        assert len(rt_args) == 2
        assert rt_args[0]["value_hint"] == "--org"
        assert rt_args[1]["value_hint"] == "acme"

    def test_args_dict_merged_into_packages(self):
        cache = {
            "srv": {
                "packages": [{"registry_name": "npm", "runtime_hint": "npx"}],
            }
        }
        dep = MCPDependency(name="srv", args={"org": "acme"})
        MCPIntegrator._apply_overlay(cache, dep)
        rt_args = cache["srv"]["packages"][0]["runtime_arguments"]
        assert len(rt_args) == 1
        assert rt_args[0]["value_hint"] == "--org=acme"

    def test_version_overlay_emits_warning(self):
        cache = {"srv": {"packages": [{"registry_name": "npm"}]}}
        dep = MCPDependency(name="srv", version="1.0.0")
        with pytest.warns(UserWarning, match=r"MCP overlay field 'version' on 'srv'.*ignored"):
            MCPIntegrator._apply_overlay(cache, dep)

    def test_custom_registry_overlay_emits_warning(self):
        cache = {"srv": {"packages": [{"registry_name": "npm"}]}}
        dep = MCPDependency(name="srv", registry="https://custom.registry.io")
        with pytest.warns(UserWarning, match=r"MCP overlay field 'registry' on 'srv'.*ignored"):
            MCPIntegrator._apply_overlay(cache, dep)

    def test_registry_false_no_warning(self):
        cache = {"srv": {"packages": [{"registry_name": "npm"}]}}
        dep = MCPDependency(name="srv", registry=False)
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            MCPIntegrator._apply_overlay(cache, dep)


# ---------------------------------------------------------------------------
# Install Flow Integration (with mocking)
# ---------------------------------------------------------------------------
class TestInstallMCPDepsWithOverlays:
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    def test_self_defined_deps_skip_registry_validation(self, _console, mock_install_runtime):
        dep = MCPDependency(
            name="my-local",
            registry=False,
            transport="stdio",
            command="my-cmd",
        )

        count = MCPIntegrator.install([dep], runtime="vscode")

        # Self-defined deps should NOT go through registry validation
        # (MCPServerOperations is never instantiated for self-defined-only lists)
        mock_install_runtime.assert_called_once()
        call_args = mock_install_runtime.call_args
        # First positional arg is runtime, second is dep list
        assert call_args[0][0] == "vscode"
        assert call_args[0][1] == ["my-local"]
        # Fourth positional arg is server_info_cache with synthetic info
        server_cache = call_args[0][3]
        assert "my-local" in server_cache
        assert "packages" in server_cache["my-local"]
        assert count == 1

    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    @patch("apm_cli.registry.operations.MCPServerOperations")
    def test_registry_deps_use_dep_names(self, mock_ops_cls, _console, mock_install_runtime):
        mock_ops = mock_ops_cls.return_value
        mock_ops.validate_servers_exist.return_value = (["io.github.github/github-mcp-server"], [])
        mock_ops.check_servers_needing_installation.return_value = [
            "io.github.github/github-mcp-server"
        ]
        mock_ops.batch_fetch_server_info.return_value = {"io.github.github/github-mcp-server": {}}
        mock_ops.collect_environment_variables.return_value = {}
        mock_ops.collect_runtime_variables.return_value = {}

        dep = MCPDependency.from_string("io.github.github/github-mcp-server")
        count = MCPIntegrator.install([dep], runtime="vscode")

        mock_ops.validate_servers_exist.assert_called_once_with(
            ["io.github.github/github-mcp-server"]
        )
        assert count == 1

    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.integration.mcp_integrator._get_console", return_value=None)
    @patch("apm_cli.registry.operations.MCPServerOperations")
    def test_mixed_deps_both_paths(self, mock_ops_cls, _console, mock_install_runtime):
        mock_ops = mock_ops_cls.return_value
        mock_ops.validate_servers_exist.return_value = (["io.github.github/github-mcp-server"], [])
        mock_ops.check_servers_needing_installation.return_value = [
            "io.github.github/github-mcp-server"
        ]
        mock_ops.batch_fetch_server_info.return_value = {"io.github.github/github-mcp-server": {}}
        mock_ops.collect_environment_variables.return_value = {}
        mock_ops.collect_runtime_variables.return_value = {}

        registry_dep = MCPDependency.from_string("io.github.github/github-mcp-server")
        self_defined_dep = MCPDependency(
            name="my-local",
            registry=False,
            transport="stdio",
            command="my-cmd",
        )

        count = MCPIntegrator.install([registry_dep, self_defined_dep], runtime="vscode")

        # Registry dep goes through validation
        mock_ops.validate_servers_exist.assert_called_once_with(
            ["io.github.github/github-mcp-server"]
        )
        # Both deps result in _install_for_runtime calls (1 registry + 1 self-defined)
        assert mock_install_runtime.call_count == 2
        assert count == 2
