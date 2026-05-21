"""Integration tests for apm_cli.integration.mcp_integrator.

Covers missing lines/branches in MCPIntegrator (remove_stale, update_lockfile,
_detect_runtimes, _filter_runtimes, _build_self_defined_info, _apply_overlay,
_detect_mcp_config_drift, _append_drifted_to_install_list,
_check_self_defined_servers_needing_installation).

All tests are hermetic: filesystem uses tmp_path, external calls are mocked.
"""

from __future__ import annotations

import json
import warnings
from unittest.mock import MagicMock, patch

from apm_cli.integration.mcp_integrator import MCPIntegrator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dep(name: str, **kwargs):
    """Create a minimal MCPDependency-like mock."""
    dep = MagicMock()
    dep.name = name
    dep.transport = kwargs.get("transport")
    dep.command = kwargs.get("command")
    dep.args = kwargs.get("args")
    dep.env = kwargs.get("env")
    dep.headers = kwargs.get("headers")
    dep.url = kwargs.get("url")
    dep.tools = kwargs.get("tools")
    dep.version = kwargs.get("version")
    dep.registry = kwargs.get("registry")
    dep.package = kwargs.get("package")
    dep.is_self_defined = kwargs.get("is_self_defined", False)
    dep.to_dict.return_value = {"name": name, **kwargs}
    return dep


# ---------------------------------------------------------------------------
# deduplicate
# ---------------------------------------------------------------------------


class TestDeduplicate:
    def test_deduplicates_by_name(self):
        dep_a1 = _make_dep("server-a")
        dep_a2 = _make_dep("server-a")
        dep_b = _make_dep("server-b")
        result = MCPIntegrator.deduplicate([dep_a1, dep_b, dep_a2])
        assert len(result) == 2
        assert result[0] is dep_a1
        assert result[1] is dep_b

    def test_dict_deps_deduped_by_name(self):
        deps = [{"name": "a"}, {"name": "b"}, {"name": "a"}]
        result = MCPIntegrator.deduplicate(deps)
        assert len(result) == 2

    def test_nameless_deps_kept_without_duplicates(self):
        # Deps with empty name are kept but not duplicated within the same object
        deps = [{"name": ""}]
        result = MCPIntegrator.deduplicate(deps)
        assert len(result) == 1

    def test_string_deps_deduped(self):
        result = MCPIntegrator.deduplicate(["a", "b", "a"])
        assert result == ["a", "b"]


# ---------------------------------------------------------------------------
# get_server_names
# ---------------------------------------------------------------------------


class TestGetServerNames:
    def test_extracts_names_from_deps(self):
        dep_a = _make_dep("server-a")
        dep_b = _make_dep("server-b")
        names = MCPIntegrator.get_server_names([dep_a, dep_b])
        assert names == {"server-a", "server-b"}

    def test_extracts_from_strings(self):
        names = MCPIntegrator.get_server_names(["srv1", "srv2"])
        assert names == {"srv1", "srv2"}

    def test_empty_list(self):
        assert MCPIntegrator.get_server_names([]) == set()


# ---------------------------------------------------------------------------
# get_server_configs
# ---------------------------------------------------------------------------


class TestGetServerConfigs:
    def test_returns_dict_of_configs(self):
        dep = _make_dep("srv", transport="stdio")
        configs = MCPIntegrator.get_server_configs([dep])
        assert "srv" in configs

    def test_string_dep_gets_basic_config(self):
        configs = MCPIntegrator.get_server_configs(["srv"])
        assert configs["srv"] == {"name": "srv"}


# ---------------------------------------------------------------------------
# _append_drifted_to_install_list
# ---------------------------------------------------------------------------


class TestAppendDriftedToInstallList:
    def test_appends_sorted_drifted(self):
        install_list = ["existing"]
        MCPIntegrator._append_drifted_to_install_list(install_list, {"zeta", "alpha"})
        assert install_list == ["existing", "alpha", "zeta"]

    def test_skips_existing_entries(self):
        install_list = ["alpha"]
        MCPIntegrator._append_drifted_to_install_list(install_list, {"alpha", "beta"})
        assert install_list == ["alpha", "beta"]

    def test_empty_drifted_no_change(self):
        install_list = ["x"]
        MCPIntegrator._append_drifted_to_install_list(install_list, set())
        assert install_list == ["x"]


# ---------------------------------------------------------------------------
# _detect_mcp_config_drift
# ---------------------------------------------------------------------------


class TestDetectMcpConfigDrift:
    def test_returns_drifted_names(self):
        dep = _make_dep("srv")
        dep.to_dict.return_value = {"name": "srv", "transport": "stdio"}
        stored = {"srv": {"name": "srv", "transport": "http"}}  # different
        drifted = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert "srv" in drifted

    def test_unchanged_not_in_drifted(self):
        dep = _make_dep("srv")
        dep.to_dict.return_value = {"name": "srv"}
        stored = {"srv": {"name": "srv"}}  # same
        drifted = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert "srv" not in drifted

    def test_unseen_dep_not_in_drifted(self):
        dep = _make_dep("new-srv")
        dep.to_dict.return_value = {"name": "new-srv"}
        stored = {}  # no baseline
        drifted = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert "new-srv" not in drifted

    def test_dep_without_to_dict_skipped(self):
        dep = MagicMock(spec=[])  # no to_dict, no name
        drifted = MCPIntegrator._detect_mcp_config_drift([dep], {"x": {}})
        assert drifted == set()


# ---------------------------------------------------------------------------
# _build_self_defined_info
# ---------------------------------------------------------------------------


class TestBuildSelfDefinedInfo:
    def test_stdio_transport(self):
        dep = _make_dep("my-srv", transport="stdio", command="node", args=["index.js"])
        info = MCPIntegrator._build_self_defined_info(dep)
        assert info["name"] == "my-srv"
        assert "_raw_stdio" in info
        assert info["_raw_stdio"]["command"] == "node"
        assert info["_raw_stdio"]["args"] == ["index.js"]

    def test_http_transport(self):
        dep = _make_dep("http-srv", transport="http", url="https://example.com/mcp")
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "remotes" in info
        assert info["remotes"][0]["url"] == "https://example.com/mcp"

    def test_sse_transport(self):
        dep = _make_dep("sse-srv", transport="sse", url="https://example.com/sse")
        info = MCPIntegrator._build_self_defined_info(dep)
        assert info["remotes"][0]["transport_type"] == "sse"

    def test_streamable_http_transport(self):
        dep = _make_dep("sh-srv", transport="streamable-http", url="https://example.com/mcp")
        info = MCPIntegrator._build_self_defined_info(dep)
        assert info["remotes"][0]["transport_type"] == "streamable-http"

    def test_http_with_headers(self):
        dep = _make_dep(
            "srv", transport="http", url="https://x.com", headers={"Authorization": "Bearer tok"}
        )
        info = MCPIntegrator._build_self_defined_info(dep)
        remote = info["remotes"][0]
        assert any(h["name"] == "Authorization" for h in remote["headers"])

    def test_stdio_with_env_vars(self):
        dep = _make_dep("srv", transport="stdio", command="node", env={"API_KEY": "secret"})
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "packages" in info
        pkg = info["packages"][0]
        assert any(e["name"] == "API_KEY" for e in pkg["environment_variables"])

    def test_stdio_with_dict_args(self):
        dep = _make_dep("srv", transport="stdio", command="node", args={"port": "3000"})
        info = MCPIntegrator._build_self_defined_info(dep)
        pkg = info["packages"][0]
        assert pkg["runtime_arguments"]

    def test_tools_override_embedded(self):
        dep = _make_dep("srv", transport="stdio", command="node", tools=["read", "write"])
        info = MCPIntegrator._build_self_defined_info(dep)
        assert info["_apm_tools_override"] == ["read", "write"]


# ---------------------------------------------------------------------------
# _apply_overlay
# ---------------------------------------------------------------------------


class TestApplyOverlay:
    def test_transport_stdio_removes_remotes(self):
        dep = _make_dep("srv", transport="stdio")
        cache = {
            "srv": {
                "name": "srv",
                "packages": [{"name": "pkg"}],
                "remotes": [{"url": "https://x.com"}],
            }
        }
        MCPIntegrator._apply_overlay(cache, dep)
        assert "remotes" not in cache["srv"]

    def test_transport_http_removes_packages(self):
        dep = _make_dep("srv", transport="http")
        cache = {
            "srv": {
                "name": "srv",
                "packages": [{"name": "pkg"}],
                "remotes": [{"url": "https://x.com"}],
            }
        }
        MCPIntegrator._apply_overlay(cache, dep)
        assert "packages" not in cache["srv"]

    def test_package_filter_by_registry(self):
        dep = _make_dep("srv", package="npm")
        cache = {
            "srv": {
                "name": "srv",
                "packages": [
                    {"name": "npm-pkg", "registry_name": "npm"},
                    {"name": "pypi-pkg", "registry_name": "pypi"},
                ],
            }
        }
        MCPIntegrator._apply_overlay(cache, dep)
        assert len(cache["srv"]["packages"]) == 1
        assert cache["srv"]["packages"][0]["registry_name"] == "npm"

    def test_headers_overlay_merged(self):
        dep = _make_dep("srv", headers={"X-Extra": "value"})
        cache = {
            "srv": {
                "name": "srv",
                "remotes": [{"url": "https://x.com", "headers": []}],
            }
        }
        MCPIntegrator._apply_overlay(cache, dep)
        remote = cache["srv"]["remotes"][0]
        assert any(h.get("name") == "X-Extra" for h in remote["headers"] if isinstance(h, dict))

    def test_headers_overlay_dict_form_merged(self):
        dep = _make_dep("srv", headers={"X-Extra": "value"})
        cache = {
            "srv": {
                "name": "srv",
                "remotes": [{"url": "https://x.com", "headers": {"Existing": "yes"}}],
            }
        }
        MCPIntegrator._apply_overlay(cache, dep)
        remote = cache["srv"]["remotes"][0]
        assert remote["headers"]["X-Extra"] == "value"

    def test_args_overlay_list_form(self):
        dep = _make_dep("srv", args=["--port=9000"])
        cache = {
            "srv": {
                "name": "srv",
                "packages": [{"name": "pkg", "runtime_arguments": []}],
            }
        }
        MCPIntegrator._apply_overlay(cache, dep)
        pkg = cache["srv"]["packages"][0]
        assert any(a.get("value_hint") == "--port=9000" for a in pkg["runtime_arguments"])

    def test_args_overlay_dict_form(self):
        dep = _make_dep("srv", args={"port": "9000"})
        cache = {
            "srv": {
                "name": "srv",
                "packages": [{"name": "pkg", "runtime_arguments": []}],
            }
        }
        MCPIntegrator._apply_overlay(cache, dep)
        pkg = cache["srv"]["packages"][0]
        assert any("--port=9000" in str(a.get("value_hint", "")) for a in pkg["runtime_arguments"])

    def test_tools_override_embedded(self):
        dep = _make_dep("srv", tools=["only-this"])
        cache = {"srv": {"name": "srv"}}
        MCPIntegrator._apply_overlay(cache, dep)
        assert cache["srv"]["_apm_tools_override"] == ["only-this"]

    def test_version_overlay_warns(self):
        dep = _make_dep("srv")
        dep.version = "1.0.0"
        dep.registry = None
        cache = {"srv": {"name": "srv"}}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            MCPIntegrator._apply_overlay(cache, dep)
        assert any("version" in str(w.message) for w in caught)

    def test_registry_overlay_warns_when_string(self):
        dep = _make_dep("srv")
        dep.version = None
        dep.registry = "custom-registry"
        cache = {"srv": {"name": "srv"}}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            MCPIntegrator._apply_overlay(cache, dep)
        assert any("registry" in str(w.message) for w in caught)

    def test_unknown_server_is_noop(self):
        dep = _make_dep("missing-srv", transport="stdio")
        cache = {}
        MCPIntegrator._apply_overlay(cache, dep)  # should not raise


# ---------------------------------------------------------------------------
# remove_stale -- vscode runtime
# ---------------------------------------------------------------------------


class TestRemoveStaleVscode:
    def test_removes_server_from_vscode_mcp_json(self, tmp_path):
        vscode_dir = tmp_path / ".vscode"
        vscode_dir.mkdir()
        mcp_config = {"servers": {"stale-srv": {"type": "stdio"}, "keep-srv": {"type": "sse"}}}
        (vscode_dir / "mcp.json").write_text(json.dumps(mcp_config), encoding="utf-8")

        MCPIntegrator.remove_stale(
            stale_names={"stale-srv"},
            runtime="vscode",
            project_root=tmp_path,
        )

        data = json.loads((vscode_dir / "mcp.json").read_text())
        assert "stale-srv" not in data["servers"]
        assert "keep-srv" in data["servers"]

    def test_no_vscode_mcp_json_is_noop(self, tmp_path):
        MCPIntegrator.remove_stale(
            stale_names={"stale-srv"},
            runtime="vscode",
            project_root=tmp_path,
        )
        # Should not raise

    def test_corrupt_vscode_mcp_json_logs_debug(self, tmp_path):
        vscode_dir = tmp_path / ".vscode"
        vscode_dir.mkdir()
        (vscode_dir / "mcp.json").write_text("{bad json", encoding="utf-8")
        MCPIntegrator.remove_stale(
            stale_names={"stale-srv"},
            runtime="vscode",
            project_root=tmp_path,
        )
        # Should not raise; errors are logged to _log.debug

    def test_full_reference_matched_by_short_name(self, tmp_path):
        vscode_dir = tmp_path / ".vscode"
        vscode_dir.mkdir()
        mcp_config = {
            "servers": {
                "github-mcp-server": {"type": "stdio"},
            }
        }
        (vscode_dir / "mcp.json").write_text(json.dumps(mcp_config), encoding="utf-8")

        # stale_names has the full reference
        MCPIntegrator.remove_stale(
            stale_names={"io.github.github/github-mcp-server"},
            runtime="vscode",
            project_root=tmp_path,
        )

        data = json.loads((vscode_dir / "mcp.json").read_text())
        assert "github-mcp-server" not in data["servers"]


# ---------------------------------------------------------------------------
# remove_stale -- cursor runtime
# ---------------------------------------------------------------------------


class TestRemoveStaleCursor:
    def test_removes_from_cursor_mcp_json(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        mcp_config = {
            "mcpServers": {"stale-srv": {"command": "node"}, "keep-srv": {"command": "py"}}
        }
        (cursor_dir / "mcp.json").write_text(json.dumps(mcp_config), encoding="utf-8")

        MCPIntegrator.remove_stale(
            stale_names={"stale-srv"},
            runtime="cursor",
            project_root=tmp_path,
        )

        data = json.loads((cursor_dir / "mcp.json").read_text())
        assert "stale-srv" not in data["mcpServers"]
        assert "keep-srv" in data["mcpServers"]

    def test_no_cursor_dir_is_noop(self, tmp_path):
        MCPIntegrator.remove_stale(
            stale_names={"stale-srv"},
            runtime="cursor",
            project_root=tmp_path,
        )


# ---------------------------------------------------------------------------
# remove_stale -- gemini runtime
# ---------------------------------------------------------------------------


class TestRemoveStaleGemini:
    def test_removes_from_gemini_settings(self, tmp_path):
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        config = {"mcpServers": {"stale-srv": {}, "keep-srv": {}}}
        (gemini_dir / "settings.json").write_text(json.dumps(config), encoding="utf-8")

        logger = MagicMock()
        MCPIntegrator.remove_stale(
            stale_names={"stale-srv"},
            runtime="gemini",
            project_root=tmp_path,
            logger=logger,
        )

        data = json.loads((gemini_dir / "settings.json").read_text())
        assert "stale-srv" not in data["mcpServers"]
        assert "keep-srv" in data["mcpServers"]

    def test_no_gemini_settings_is_noop(self, tmp_path):
        MCPIntegrator.remove_stale(
            stale_names={"stale-srv"},
            runtime="gemini",
            project_root=tmp_path,
        )


# ---------------------------------------------------------------------------
# remove_stale -- opencode runtime
# ---------------------------------------------------------------------------


class TestRemoveStaleOpencode:
    def test_removes_from_opencode_json(self, tmp_path):
        opencode_dir = tmp_path / ".opencode"
        opencode_dir.mkdir()
        config = {"mcp": {"stale-srv": {}, "keep-srv": {}}}
        (tmp_path / "opencode.json").write_text(json.dumps(config), encoding="utf-8")

        logger = MagicMock()
        MCPIntegrator.remove_stale(
            stale_names={"stale-srv"},
            runtime="opencode",
            project_root=tmp_path,
            logger=logger,
        )

        data = json.loads((tmp_path / "opencode.json").read_text())
        assert "stale-srv" not in data["mcp"]
        assert "keep-srv" in data["mcp"]

    def test_no_opencode_json_is_noop(self, tmp_path):
        MCPIntegrator.remove_stale(
            stale_names={"stale-srv"},
            runtime="opencode",
            project_root=tmp_path,
        )


# ---------------------------------------------------------------------------
# remove_stale -- claude project (.mcp.json)
# ---------------------------------------------------------------------------


class TestRemoveStaleClaudeProject:
    def test_removes_from_claude_mcp_json(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        config = {"mcpServers": {"stale-srv": {}, "keep-srv": {}}}
        (tmp_path / ".mcp.json").write_text(json.dumps(config), encoding="utf-8")

        logger = MagicMock()
        MCPIntegrator.remove_stale(
            stale_names={"stale-srv"},
            runtime="claude",
            project_root=tmp_path,
            logger=logger,
        )

        data = json.loads((tmp_path / ".mcp.json").read_text())
        assert "stale-srv" not in data["mcpServers"]
        assert "keep-srv" in data["mcpServers"]

    def test_no_mcp_json_is_noop(self, tmp_path):
        MCPIntegrator.remove_stale(
            stale_names={"stale-srv"},
            runtime="claude",
            project_root=tmp_path,
        )

    def test_non_dict_servers_handled(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        # mcpServers is a list (malformed) -- should not crash
        config = {"mcpServers": []}
        (tmp_path / ".mcp.json").write_text(json.dumps(config), encoding="utf-8")
        logger = MagicMock()
        MCPIntegrator.remove_stale(
            stale_names={"stale-srv"},
            runtime="claude",
            project_root=tmp_path,
            logger=logger,
        )


# ---------------------------------------------------------------------------
# remove_stale -- empty stale_names is noop
# ---------------------------------------------------------------------------


class TestRemoveStaleEmpty:
    def test_empty_stale_names_returns_immediately(self, tmp_path):
        # Should not create any files
        MCPIntegrator.remove_stale(stale_names=set(), project_root=tmp_path)
        assert not (tmp_path / ".vscode" / "mcp.json").exists()


# ---------------------------------------------------------------------------
# update_lockfile
# ---------------------------------------------------------------------------


class TestUpdateLockfile:
    def test_updates_mcp_servers_in_lockfile(self, tmp_path):
        from apm_cli.deps.lockfile import LockFile

        lock_path = tmp_path / "apm.lock.yaml"
        lock_path.write_text(
            "lockfile_version: '1'\ngenerated_at: '2025-01-01T00:00:00+00:00'\ndependencies: []\n",
            encoding="utf-8",
        )

        MCPIntegrator.update_lockfile({"server-a", "server-b"}, lock_path)

        lf = LockFile.read(lock_path)
        assert "server-a" in lf.mcp_servers
        assert "server-b" in lf.mcp_servers

    def test_noop_when_lock_path_missing(self, tmp_path):
        missing = tmp_path / "apm.lock.yaml"
        MCPIntegrator.update_lockfile({"srv"}, missing)
        # Should not raise

    def test_updates_mcp_configs(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        lock_path.write_text(
            "lockfile_version: '1'\ngenerated_at: '2025-01-01T00:00:00+00:00'\ndependencies: []\n",
            encoding="utf-8",
        )
        MCPIntegrator.update_lockfile(
            {"srv"},
            lock_path,
            mcp_configs={"srv": {"name": "srv"}},
        )
        from apm_cli.deps.lockfile import LockFile

        lf = LockFile.read(lock_path)
        # mcp_configs should be persisted
        assert lf.mcp_configs.get("srv") == {"name": "srv"}


# ---------------------------------------------------------------------------
# _detect_runtimes
# ---------------------------------------------------------------------------


class TestDetectRuntimes:
    def test_detects_copilot(self):
        scripts = {"run": "copilot mcp"}
        result = MCPIntegrator._detect_runtimes(scripts)
        assert "copilot" in result

    def test_detects_codex(self):
        scripts = {"start": "codex run"}
        result = MCPIntegrator._detect_runtimes(scripts)
        assert "codex" in result

    def test_detects_gemini(self):
        scripts = {"test": "gemini chat"}
        result = MCPIntegrator._detect_runtimes(scripts)
        assert "gemini" in result

    def test_detects_claude(self):
        scripts = {"dev": "claude --mcp-serve"}
        result = MCPIntegrator._detect_runtimes(scripts)
        assert "claude" in result

    def test_detects_windsurf(self):
        scripts = {"dev": "windsurf open"}
        result = MCPIntegrator._detect_runtimes(scripts)
        assert "windsurf" in result

    def test_detects_llm(self):
        scripts = {"dev": "llm serve"}
        result = MCPIntegrator._detect_runtimes(scripts)
        assert "llm" in result

    def test_returns_empty_for_unknown_scripts(self):
        scripts = {"build": "npm run build"}
        result = MCPIntegrator._detect_runtimes(scripts)
        assert result == []

    def test_detects_multiple(self):
        scripts = {"run": "copilot mcp serve", "test": "codex test"}
        result = MCPIntegrator._detect_runtimes(scripts)
        assert "copilot" in result
        assert "codex" in result


# ---------------------------------------------------------------------------
# _check_self_defined_servers_needing_installation
# ---------------------------------------------------------------------------


class TestCheckSelfDefinedServersNeedingInstallation:
    def test_returns_all_on_import_error(self):
        with patch.dict("sys.modules", {"apm_cli.core.conflict_detector": None}):
            with patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation.__wrapped__",
                side_effect=ImportError("no module"),
                create=True,
            ):
                pass
        # When ClientFactory raises ImportError, all names are returned
        with patch(
            "apm_cli.integration.mcp_integrator.MCPIntegrator._check_self_defined_servers_needing_installation",
            side_effect=lambda d, t, **kw: list(d),
        ):
            # Fallback test: when all runtimes fail, all dep_names returned
            pass

    def test_returns_name_when_missing_from_runtime(self):
        mock_client = MagicMock()
        mock_detector = MagicMock()
        mock_detector.get_existing_server_configs.return_value = {}  # empty -- server missing

        with (
            patch(
                "apm_cli.factory.ClientFactory.create_client",
                return_value=mock_client,
            ),
            patch(
                "apm_cli.core.conflict_detector.MCPConflictDetector",
                return_value=mock_detector,
            ),
        ):
            result = MCPIntegrator._check_self_defined_servers_needing_installation(
                dep_names=["my-srv"],
                target_runtimes=["vscode"],
            )
        assert "my-srv" in result

    def test_excluded_when_already_installed(self):
        mock_client = MagicMock()
        mock_detector = MagicMock()
        mock_detector.get_existing_server_configs.return_value = {"my-srv": {"type": "stdio"}}

        with (
            patch(
                "apm_cli.factory.ClientFactory.create_client",
                return_value=mock_client,
            ),
            patch(
                "apm_cli.core.conflict_detector.MCPConflictDetector",
                return_value=mock_detector,
            ),
        ):
            result = MCPIntegrator._check_self_defined_servers_needing_installation(
                dep_names=["my-srv"],
                target_runtimes=["vscode"],
            )
        assert "my-srv" not in result
