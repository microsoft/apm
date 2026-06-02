"""Unit tests for apm_cli.integration.mcp_integrator -- phase 3 w4.

Covers missing lines/branches in mcp_integrator.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _suppress_console(monkeypatch):
    monkeypatch.setattr("apm_cli.utils.console._get_console", lambda: None)


# ---------------------------------------------------------------------------
# _is_vscode_available
# ---------------------------------------------------------------------------


class TestIsVscodeAvailable:
    def test_returns_true_when_code_on_path(self, tmp_path):
        from apm_cli.integration.mcp_integrator import _is_vscode_available

        with patch("shutil.which", return_value="/usr/bin/code"):
            assert _is_vscode_available(tmp_path) is True

    def test_returns_true_when_vscode_dir_exists(self, tmp_path):
        from apm_cli.integration.mcp_integrator import _is_vscode_available

        (tmp_path / ".vscode").mkdir()
        with patch("shutil.which", return_value=None):
            assert _is_vscode_available(tmp_path) is True

    def test_returns_false_when_neither(self, tmp_path):
        from apm_cli.integration.mcp_integrator import _is_vscode_available

        with patch("shutil.which", return_value=None):
            assert _is_vscode_available(tmp_path) is False

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
        assert info["remotes"][0]["transport_type"] == "sse"

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


class TestRemoveStale:
    def test_noop_when_empty_stale_names(self, tmp_path):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        MCPIntegrator.remove_stale(set(), project_root=tmp_path, logger=logger)
        logger.progress.assert_not_called()

    def test_removes_from_vscode_mcp_json(self, tmp_path):
        import json

        from apm_cli.integration.mcp_integrator import MCPIntegrator

        vscode_dir = tmp_path / ".vscode"
        vscode_dir.mkdir()
        mcp_json = vscode_dir / "mcp.json"
        mcp_json.write_text(
            json.dumps({"servers": {"stale-server": {"cmd": "x"}, "keep": {"cmd": "y"}}}),
            encoding="utf-8",
        )

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["vscode"]):
            MCPIntegrator.remove_stale(
                {"stale-server"},
                project_root=tmp_path,
                logger=MagicMock(),
            )

        result = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert "stale-server" not in result["servers"]
        assert "keep" in result["servers"]

    def test_removes_from_opencode_json(self, tmp_path):
        import json

        from apm_cli.integration.mcp_integrator import MCPIntegrator

        opencode_dir = tmp_path / ".opencode"
        opencode_dir.mkdir()
        cfg = tmp_path / "opencode.json"
        cfg.write_text(
            json.dumps({"mcp": {"stale-srv": {}, "keep-srv": {}}}),
            encoding="utf-8",
        )

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["opencode"]):
            MCPIntegrator.remove_stale(
                {"stale-srv"},
                project_root=tmp_path,
                logger=MagicMock(),
            )

        result = json.loads(cfg.read_text(encoding="utf-8"))
        assert "stale-srv" not in result["mcp"]
        assert "keep-srv" in result["mcp"]

    def test_removes_from_gemini_settings(self, tmp_path):
        import json

        from apm_cli.integration.mcp_integrator import MCPIntegrator

        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        cfg = gemini_dir / "settings.json"
        cfg.write_text(
            json.dumps({"mcpServers": {"stale-gem": {}, "keep": {}}}),
            encoding="utf-8",
        )

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["gemini"]):
            MCPIntegrator.remove_stale(
                {"stale-gem"},
                project_root=tmp_path,
                logger=MagicMock(),
            )

        result = json.loads(cfg.read_text(encoding="utf-8"))
        assert "stale-gem" not in result["mcpServers"]

    def test_removes_from_claude_project_mcp_json(self, tmp_path):
        import json

        from apm_cli.integration.mcp_integrator import MCPIntegrator

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        mcp_json = tmp_path / ".mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"stale-claude": {}, "keep": {}}}),
            encoding="utf-8",
        )

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["claude"]):
            MCPIntegrator.remove_stale(
                {"stale-claude"},
                project_root=tmp_path,
                logger=MagicMock(),
            )

        result = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert "stale-claude" not in result["mcpServers"]

    def test_scope_unspecified_logs_progress_for_claude(self, tmp_path):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        logger = MagicMock()
        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["claude"]):
            MCPIntegrator.remove_stale(
                {"some-server"},
                project_root=tmp_path,
                logger=logger,
                scope=None,
                runtime="claude",
            )
        # logger.progress should have been called at least once with scope info
        calls = [str(c) for c in logger.progress.call_args_list]
        assert any("scope" in c.lower() for c in calls)

    def test_expand_stale_includes_short_name(self, tmp_path):
        import json

        from apm_cli.integration.mcp_integrator import MCPIntegrator

        vscode_dir = tmp_path / ".vscode"
        vscode_dir.mkdir()
        mcp_json = vscode_dir / "mcp.json"
        # Use the short name (last segment) as the config key
        mcp_json.write_text(
            json.dumps({"servers": {"github-mcp-server": {"cmd": "x"}}}),
            encoding="utf-8",
        )

        with patch("apm_cli.factory.ClientFactory.supported_clients", return_value=["vscode"]):
            # Pass the full reference
            MCPIntegrator.remove_stale(
                {"io.github.github/github-mcp-server"},
                project_root=tmp_path,
                logger=MagicMock(),
            )

        result = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert "github-mcp-server" not in result["servers"]


# ---------------------------------------------------------------------------
# MCPIntegrator.update_lockfile
# ---------------------------------------------------------------------------


class TestUpdateLockfile:
    def test_noop_when_lockfile_missing(self, tmp_path):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        lock_path = tmp_path / "apm.lock.yaml"
        # Does not exist -- should silently return
        MCPIntegrator.update_lockfile({"server-a"}, lock_path=lock_path)

    def test_updates_mcp_servers_in_lockfile(self, tmp_path):
        from apm_cli.deps.lockfile import LockFile
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        lock_path = tmp_path / "apm.lock.yaml"
        LockFile().save(lock_path)

        MCPIntegrator.update_lockfile({"server-a", "server-b"}, lock_path=lock_path)

        lockfile = LockFile.read(lock_path)
        assert lockfile is not None
        assert lockfile.mcp_servers == sorted({"server-a", "server-b"})

    def test_noop_when_lockfile_read_returns_none(self, tmp_path):
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        lock_path = tmp_path / "apm.lock.yaml"
        lock_path.write_text("", encoding="utf-8")

        with patch("apm_cli.deps.lockfile.LockFile.read", return_value=None):
            # Should not raise
            MCPIntegrator.update_lockfile({"srv"}, lock_path=lock_path)

    def test_mcp_configs_written_when_provided(self, tmp_path):
        from apm_cli.deps.lockfile import LockFile
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        lock_path = tmp_path / "apm.lock.yaml"
        LockFile().save(lock_path)

        configs = {"srv": {"key": "val"}}
        MCPIntegrator.update_lockfile({"srv"}, lock_path=lock_path, mcp_configs=configs)

        lockfile = LockFile.read(lock_path)
        assert lockfile is not None
        assert lockfile.mcp_configs == configs


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
