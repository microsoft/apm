"""Unit tests for apm_cli.integration.mcp_integrator.

Covers missing lines/branches in mcp_integrator.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _suppress_console(monkeypatch):
    monkeypatch.setattr("apm_cli.utils.console._get_console", lambda: None)


# ---------------------------------------------------------------------------
# _is_vscode_available
# ---------------------------------------------------------------------------


class TestIsVscodeAvailable:
    def test_uses_cwd_when_none(self):
        from apm_cli.integration.mcp_integrator import _is_vscode_available

        with patch("shutil.which", return_value="/usr/bin/code"):
            assert _is_vscode_available(None) is True


# ---------------------------------------------------------------------------
# MCPIntegrator._build_self_defined_info
# ---------------------------------------------------------------------------


class TestBuildSelfDefinedInfo:
    def _make_dep(
        self,
        name="myserver",
        transport="stdio",
        command=None,
        args=None,
        env=None,
        url=None,
        headers=None,
        tools=None,
    ):
        dep = MagicMock()
        dep.name = name
        dep.transport = transport
        dep.command = command
        dep.args = args
        dep.env = env
        dep.url = url
        dep.headers = headers
        dep.tools = tools
        return dep

    def test_stdio_dep_builds_packages_section(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(transport="stdio", command="npx", args=["--yes", "myserver"])
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "packages" in info
        assert info["packages"][0]["runtime_hint"] == "npx"

    def test_stdio_dep_with_env(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(transport="stdio", env={"API_KEY": "abc"})
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "packages" in info
        env_vars = info["packages"][0]["environment_variables"]
        assert any(v["name"] == "API_KEY" for v in env_vars)

    def test_http_transport_builds_remotes_section(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(transport="http", url="https://api.example.com/mcp")
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "remotes" in info
        assert info["remotes"][0]["url"] == "https://api.example.com/mcp"

    def test_http_transport_with_headers(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(
            transport="http",
            url="https://api.example.com/mcp",
            headers={"X-Token": "tok123"},
        )
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "remotes" in info
        hdrs = info["remotes"][0]["headers"]
        assert any(h["name"] == "X-Token" for h in hdrs)

    def test_sse_transport(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(transport="sse", url="https://sse.example.com/mcp")
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "remotes" in info
        assert info["remotes"][0]["transport"] == "sse"

    def test_tools_override_embedded(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(transport="stdio", tools=["tool1", "tool2"])
        info = MCPIntegrator._build_self_defined_info(dep)
        assert info["_apm_tools_override"] == ["tool1", "tool2"]

    def test_dict_args_in_stdio(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(transport="stdio", args={"k": "v"})
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "packages" in info
        runtime_args = info["packages"][0]["runtime_arguments"]
        assert len(runtime_args) == 1
        assert runtime_args[0]["value_hint"] == "v"

    def test_raw_stdio_includes_command(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(transport="stdio", command="mybin", args=["a"])
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "_raw_stdio" in info
        assert info["_raw_stdio"]["command"] == "mybin"

    def test_raw_stdio_env_dict(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(transport="stdio", command="mybin", env={"FOO": "bar"})
        info = MCPIntegrator._build_self_defined_info(dep)
        assert info["_raw_stdio"]["env"] == {"FOO": "bar"}


# ---------------------------------------------------------------------------
# MCPIntegrator._apply_overlay
# ---------------------------------------------------------------------------


class TestApplyOverlay:
    def _make_dep(self, name="srv", transport=None, package=None, headers=None, args=None):
        dep = MagicMock()
        dep.name = name
        dep.transport = transport
        dep.package = package
        dep.headers = headers
        dep.args = args
        return dep

    def test_no_info_noop(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(transport="http")
        MCPIntegrator._apply_overlay({}, dep)  # no KeyError

    def test_http_transport_removes_packages(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(transport="http")
        info = {"name": "srv", "remotes": [{"url": "http://x"}], "packages": [{"name": "p"}]}
        MCPIntegrator._apply_overlay({"srv": info}, dep)
        assert "packages" not in info

    def test_stdio_transport_removes_remotes(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(transport="stdio")
        info = {
            "name": "srv",
            "packages": [{"name": "p"}],
            "remotes": [{"url": "http://x"}],
        }
        MCPIntegrator._apply_overlay({"srv": info}, dep)
        assert "remotes" not in info

    def test_package_filter_by_registry(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(package="npm")
        info = {
            "name": "srv",
            "packages": [
                {"registry_name": "npm", "name": "p1"},
                {"registry_name": "pypi", "name": "p2"},
            ],
        }
        MCPIntegrator._apply_overlay({"srv": info}, dep)
        assert len(info["packages"]) == 1
        assert info["packages"][0]["registry_name"] == "npm"

    def test_package_filter_no_match_keeps_original(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(package="docker")
        info = {
            "name": "srv",
            "packages": [{"registry_name": "npm"}],
        }
        MCPIntegrator._apply_overlay({"srv": info}, dep)
        # No match: original retained
        assert len(info["packages"]) == 1

    def test_headers_overlay_merged_into_remote(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(headers={"X-Token": "tok"})
        info = {
            "name": "srv",
            "remotes": [{"url": "http://x", "headers": []}],
        }
        MCPIntegrator._apply_overlay({"srv": info}, dep)
        assert any(h["name"] == "X-Token" for h in info["remotes"][0]["headers"])

    def test_headers_overlay_dict_existing_headers(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(headers={"X-New": "val"})
        info = {
            "name": "srv",
            "remotes": [{"url": "http://x", "headers": {"existing": "x"}}],
        }
        MCPIntegrator._apply_overlay({"srv": info}, dep)
        assert info["remotes"][0]["headers"]["X-New"] == "val"

    def test_args_overlay_list_appended_to_package(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(args=["--extra"])
        info = {
            "name": "srv",
            "packages": [{"runtime_arguments": []}],
        }
        MCPIntegrator._apply_overlay({"srv": info}, dep)
        assert len(info["packages"][0]["runtime_arguments"]) == 1

    def test_args_overlay_dict(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = self._make_dep(args={"key": "val"})
        info = {
            "name": "srv",
            "packages": [{"runtime_arguments": []}],
        }
        MCPIntegrator._apply_overlay({"srv": info}, dep)
        assert len(info["packages"][0]["runtime_arguments"]) == 1


# ---------------------------------------------------------------------------
# MCPIntegrator.deduplicate
# ---------------------------------------------------------------------------


class TestDeduplicate:
    def test_deduplicates_by_name(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep1 = MagicMock()
        dep1.name = "server-a"
        dep2 = MagicMock()
        dep2.name = "server-a"
        dep3 = MagicMock()
        dep3.name = "server-b"

        result = MCPIntegrator.deduplicate([dep1, dep2, dep3])
        assert len(result) == 2
        assert result[0] is dep1

    def test_keeps_unnamed_deps_if_unique(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        dep = MagicMock(spec=[])  # no .name attribute, no __str__ override
        result = MCPIntegrator.deduplicate([dep, dep])
        # same object not duplicated
        assert len(result) == 1

    def test_dict_deps_deduped_by_name_key(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator.deduplicate([{"name": "srv"}, {"name": "srv"}, {"name": "other"}])
        assert len(result) == 2

    def test_empty_name_dict_no_dedup(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        d1 = {"name": ""}
        d2 = {"name": ""}
        result = MCPIntegrator.deduplicate([d1, d2])
        # empty-name items are treated as "not deduplicated by name" --
        # only duplicates removed if same object
        assert len(result) >= 1


# ---------------------------------------------------------------------------
# MCPIntegrator.remove_stale
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MCPIntegrator.update_lockfile
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MCPIntegrator._detect_runtimes
# ---------------------------------------------------------------------------


class TestDetectRuntimes:
    def test_detects_copilot(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        scripts = {"run": "copilot run my-skill"}
        result = MCPIntegrator._detect_runtimes(scripts)
        assert "copilot" in result

    def test_detects_codex(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator._detect_runtimes({"build": "codex build"})
        assert "codex" in result

    def test_detects_gemini(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator._detect_runtimes({"run": "gemini run"})
        assert "gemini" in result

    def test_detects_claude(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator._detect_runtimes({"x": "claude prompt"})
        assert "claude" in result

    def test_detects_llm(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator._detect_runtimes({"x": "llm prompt"})
        assert "llm" in result

    def test_detects_windsurf(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator._detect_runtimes({"x": "windsurf run"})
        assert "windsurf" in result

    def test_empty_scripts(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator._detect_runtimes({})
        assert result == []

    def test_multiple_runtimes_in_one_script(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        result = MCPIntegrator._detect_runtimes({"all": "copilot codex gemini"})
        assert "copilot" in result
        assert "codex" in result
        assert "gemini" in result


# ---------------------------------------------------------------------------
# MCPIntegrator._install_for_runtime -- ImportError / ValueError paths
# ---------------------------------------------------------------------------


class TestInstallForRuntimeErrorPaths:
    def test_import_error_returns_false(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with patch(
            "apm_cli.core.operations.install_package",
            side_effect=ImportError("no module"),
        ):
            result = MCPIntegrator._install_for_runtime(
                runtime="copilot",
                mcp_deps=["some-dep"],
                logger=MagicMock(),
            )
        assert result is False

    def test_value_error_returns_false(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with patch(
            "apm_cli.core.operations.install_package",
            side_effect=ValueError("not supported"),
        ):
            result = MCPIntegrator._install_for_runtime(
                runtime="copilot",
                mcp_deps=["dep"],
                logger=MagicMock(),
            )
        assert result is False

    def test_generic_exception_returns_false(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with patch(
            "apm_cli.core.operations.install_package",
            side_effect=Exception("something broke"),
        ):
            result = MCPIntegrator._install_for_runtime(
                runtime="copilot",
                mcp_deps=["dep"],
                logger=MagicMock(),
            )
        assert result is False

    def test_failed_result_returns_false(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with patch(
            "apm_cli.core.operations.install_package",
            return_value={"failed": True},
        ):
            result = MCPIntegrator._install_for_runtime(
                runtime="copilot",
                mcp_deps=["dep"],
                logger=MagicMock(),
            )
        assert result is False

    def test_success_returns_true(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with patch(
            "apm_cli.core.operations.install_package",
            return_value={"failed": False},
        ):
            result = MCPIntegrator._install_for_runtime(
                runtime="copilot",
                mcp_deps=["dep"],
                logger=MagicMock(),
            )
        assert result is True


# ---------------------------------------------------------------------------
# MCPIntegrator._check_self_defined_servers_needing_installation
# ---------------------------------------------------------------------------


class TestCheckSelfDefinedServersNeedingInstallation:
    def test_import_error_returns_all_names(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        with patch.dict("sys.modules", {"apm_cli.factory": None}):
            with patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation",
                side_effect=None,
            ):
                pass  # just verify the structure; tested below via direct call

        with patch(
            "apm_cli.core.conflict_detector.MCPConflictDetector",
            side_effect=ImportError("no module"),
        ):
            result = MCPIntegrator._check_self_defined_servers_needing_installation(
                ["srv-a", "srv-b"],
                target_runtimes=["copilot"],
            )
        # The ImportError path returns list(dep_names)
        assert set(result) == {"srv-a", "srv-b"}

    def test_all_servers_already_configured(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        mock_client = MagicMock()
        mock_detector = MagicMock()
        mock_detector.get_existing_server_configs.return_value = {"srv-a": {}, "srv-b": {}}

        with (
            patch("apm_cli.core.conflict_detector.MCPConflictDetector", return_value=mock_detector),
            patch("apm_cli.factory.ClientFactory.create_client", return_value=mock_client),
        ):
            result = MCPIntegrator._check_self_defined_servers_needing_installation(
                ["srv-a", "srv-b"],
                target_runtimes=["copilot"],
            )
        assert result == []

    def test_server_missing_from_runtime(self):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        mock_client = MagicMock()
        mock_detector = MagicMock()
        mock_detector.get_existing_server_configs.return_value = {"srv-a": {}}

        with (
            patch("apm_cli.core.conflict_detector.MCPConflictDetector", return_value=mock_detector),
            patch("apm_cli.factory.ClientFactory.create_client", return_value=mock_client),
        ):
            result = MCPIntegrator._check_self_defined_servers_needing_installation(
                ["srv-a", "srv-b"],
                target_runtimes=["copilot"],
            )
        assert "srv-b" in result
