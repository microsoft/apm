"""Unit tests for MCPIntegrator.

Tests focus on pure-logic methods, ensuring the orchestration helpers
(deduplication, server-info building, drift detection, runtime detection,
lockfile update, and stale cleanup) behave correctly without requiring
live network calls or installed runtimes.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.integration.mcp_integrator import MCPIntegrator, _is_vscode_available
from apm_cli.models.dependency.mcp import MCPDependency


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dep(name, **kwargs) -> MCPDependency:
    """Convenience factory for MCPDependency."""
    return MCPDependency(name=name, **kwargs)


def _make_self_defined(name, transport="stdio", command=None, url=None, **kwargs):
    """Create a self-defined (registry: false) MCPDependency."""
    return MCPDependency(
        name=name,
        registry=False,
        transport=transport,
        command=command,
        url=url,
        **kwargs,
    )


# ===========================================================================
# _is_vscode_available
# ===========================================================================


class TestIsVscodeAvailable:
    def test_returns_true_when_code_on_path(self, tmp_path):
        with patch("apm_cli.integration.mcp_integrator.shutil.which", return_value="/usr/bin/code"), \
             patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path):
            assert _is_vscode_available() is True

    def test_returns_true_when_vscode_dir_exists(self, tmp_path):
        (tmp_path / ".vscode").mkdir()
        with patch("apm_cli.integration.mcp_integrator.shutil.which", return_value=None), \
             patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path):
            assert _is_vscode_available() is True

    def test_returns_false_when_neither_available(self, tmp_path):
        with patch("apm_cli.integration.mcp_integrator.shutil.which", return_value=None), \
             patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path):
            assert _is_vscode_available() is False

    def test_code_on_path_takes_precedence_over_missing_dir(self, tmp_path):
        # No .vscode dir, but 'code' is on PATH
        with patch("apm_cli.integration.mcp_integrator.shutil.which", return_value="/usr/bin/code"), \
             patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path):
            assert _is_vscode_available() is True


# ===========================================================================
# MCPIntegrator.deduplicate
# ===========================================================================


class TestDeduplicate:
    def test_empty_list(self):
        assert MCPIntegrator.deduplicate([]) == []

    def test_no_duplicates(self):
        deps = [_make_dep("a"), _make_dep("b"), _make_dep("c")]
        result = MCPIntegrator.deduplicate(deps)
        assert [d.name for d in result] == ["a", "b", "c"]

    def test_first_occurrence_wins(self):
        dep_a1 = _make_dep("server", transport="stdio")
        dep_a2 = _make_dep("server", transport="sse")
        result = MCPIntegrator.deduplicate([dep_a1, dep_a2])
        assert len(result) == 1
        assert result[0].transport == "stdio"

    def test_dedup_with_dict_entries(self):
        deps = [{"name": "foo"}, {"name": "foo"}, {"name": "bar"}]
        result = MCPIntegrator.deduplicate(deps)
        assert len(result) == 2
        assert result[0]["name"] == "foo"
        assert result[1]["name"] == "bar"

    def test_nameless_items_kept_by_value_inequality(self):
        # Nameless items that are not equal to each other are both kept.
        # MCPIntegrator.deduplicate() uses `dep not in result` for nameless
        # entries, so equality (not identity) governs dedup; two distinct
        # dicts with different contents are kept.
        dep1 = {"other": "x"}
        dep2 = {"other": "y"}
        result = MCPIntegrator.deduplicate([dep1, dep2])
        assert len(result) == 2

    def test_nameless_duplicate_reference_skipped(self):
        dep = {"other": "x"}
        result = MCPIntegrator.deduplicate([dep, dep])
        # Same object reference appears twice; deduplicate keeps only one
        assert len(result) == 1

    def test_mixed_string_and_object(self):
        # Strings fall through to `str(dep)` for the name key, so two equal
        # strings dedup by name like any other named entry, while a distinct
        # MCPDependency is preserved alongside.
        deps = ["alpha", _make_dep("beta"), "alpha"]
        result = MCPIntegrator.deduplicate(deps)
        assert len(result) == 2
        assert result[0] == "alpha"
        assert result[1].name == "beta"

    def test_preserves_order(self):
        names = ["z", "a", "m", "b"]
        deps = [_make_dep(n) for n in names]
        result = MCPIntegrator.deduplicate(deps)
        assert [d.name for d in result] == names


# ===========================================================================
# MCPIntegrator.get_server_names
# ===========================================================================


class TestGetServerNames:
    def test_empty(self):
        assert MCPIntegrator.get_server_names([]) == set()

    def test_dep_objects(self):
        deps = [_make_dep("alpha"), _make_dep("beta")]
        assert MCPIntegrator.get_server_names(deps) == {"alpha", "beta"}

    def test_plain_strings(self):
        assert MCPIntegrator.get_server_names(["foo", "bar"]) == {"foo", "bar"}

    def test_mixed(self):
        names = MCPIntegrator.get_server_names([_make_dep("obj"), "str_dep"])
        assert names == {"obj", "str_dep"}

    def test_deduplication_at_extraction(self):
        deps = [_make_dep("x"), _make_dep("x"), "x"]
        assert MCPIntegrator.get_server_names(deps) == {"x"}


# ===========================================================================
# MCPIntegrator.get_server_configs
# ===========================================================================


class TestGetServerConfigs:
    def test_empty(self):
        assert MCPIntegrator.get_server_configs([]) == {}

    def test_dep_object_serialized(self):
        dep = _make_dep("svc", transport="stdio")
        configs = MCPIntegrator.get_server_configs([dep])
        assert "svc" in configs
        assert configs["svc"]["name"] == "svc"
        assert configs["svc"]["transport"] == "stdio"

    def test_plain_string_fallback(self):
        configs = MCPIntegrator.get_server_configs(["plain-server"])
        assert configs == {"plain-server": {"name": "plain-server"}}

    def test_multiple_deps(self):
        deps = [_make_dep("a"), _make_dep("b")]
        configs = MCPIntegrator.get_server_configs(deps)
        assert set(configs.keys()) == {"a", "b"}


# ===========================================================================
# MCPIntegrator._append_drifted_to_install_list
# ===========================================================================


class TestAppendDrifted:
    def test_appends_sorted(self):
        install_list = []
        MCPIntegrator._append_drifted_to_install_list(install_list, {"z", "a", "m"})
        assert install_list == ["a", "m", "z"]

    def test_no_duplicates_with_existing(self):
        install_list = ["a", "b"]
        MCPIntegrator._append_drifted_to_install_list(install_list, {"b", "c"})
        assert install_list == ["a", "b", "c"]

    def test_empty_drifted(self):
        install_list = ["existing"]
        MCPIntegrator._append_drifted_to_install_list(install_list, set())
        assert install_list == ["existing"]

    def test_empty_install_list(self):
        install_list = []
        MCPIntegrator._append_drifted_to_install_list(install_list, {"only"})
        assert install_list == ["only"]


# ===========================================================================
# MCPIntegrator._detect_mcp_config_drift
# ===========================================================================


class TestDetectMcpConfigDrift:
    def test_no_drift_when_configs_match(self):
        dep = _make_dep("svc", transport="stdio")
        stored = {"svc": dep.to_dict()}
        drifted = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert drifted == set()

    def test_drift_detected_on_change(self):
        dep_original = _make_dep("svc", transport="stdio")
        dep_updated = _make_dep("svc", transport="sse")
        stored = {"svc": dep_original.to_dict()}
        drifted = MCPIntegrator._detect_mcp_config_drift([dep_updated], stored)
        assert drifted == {"svc"}

    def test_no_drift_for_new_server_not_in_stored(self):
        dep = _make_dep("new-svc")
        drifted = MCPIntegrator._detect_mcp_config_drift([dep], {})
        assert drifted == set()

    def test_skips_non_dep_items(self):
        # Plain strings without to_dict/name should be ignored
        drifted = MCPIntegrator._detect_mcp_config_drift(["raw-string"], {"raw-string": {}})
        assert drifted == set()

    def test_multiple_deps_partial_drift(self):
        dep_a = _make_dep("a", transport="stdio")
        dep_b = _make_dep("b", transport="stdio")
        stored = {
            "a": dep_a.to_dict(),
            "b": _make_dep("b", transport="sse").to_dict(),  # different transport
        }
        drifted = MCPIntegrator._detect_mcp_config_drift([dep_a, dep_b], stored)
        assert drifted == {"b"}


# ===========================================================================
# MCPIntegrator._detect_runtimes
# ===========================================================================


class TestDetectRuntimes:
    def test_empty_scripts(self):
        assert MCPIntegrator._detect_runtimes({}) == []

    def test_detects_copilot(self):
        runtimes = MCPIntegrator._detect_runtimes({"run": "apm run copilot"})
        assert "copilot" in runtimes

    def test_detects_codex(self):
        runtimes = MCPIntegrator._detect_runtimes({"deploy": "deploy with codex"})
        assert "codex" in runtimes

    def test_detects_llm(self):
        runtimes = MCPIntegrator._detect_runtimes({"run": "llm serve"})
        assert "llm" in runtimes

    def test_no_partial_word_match(self):
        # "copiloting" should not match "copilot" word boundary
        runtimes = MCPIntegrator._detect_runtimes({"run": "copiloting something"})
        assert "copilot" not in runtimes

    def test_detects_multiple_runtimes(self):
        scripts = {"step1": "run copilot", "step2": "run codex"}
        runtimes = MCPIntegrator._detect_runtimes(scripts)
        assert set(runtimes) >= {"copilot", "codex"}

    def test_no_false_positives(self):
        runtimes = MCPIntegrator._detect_runtimes({"run": "python main.py"})
        assert runtimes == []


# ===========================================================================
# MCPIntegrator._build_self_defined_info
# ===========================================================================


class TestBuildSelfDefinedInfo:
    def test_stdio_minimal(self):
        dep = _make_self_defined("my-tool", transport="stdio", command="my-tool")
        info = MCPIntegrator._build_self_defined_info(dep)
        assert info["name"] == "my-tool"
        assert "_raw_stdio" in info
        assert info["_raw_stdio"]["command"] == "my-tool"

    def test_stdio_with_args_and_env(self):
        dep = _make_self_defined(
            "cli-tool",
            transport="stdio",
            command="cli-tool",
            args=["--verbose", "--output=json"],
            env={"TOKEN": "secret"},
        )
        info = MCPIntegrator._build_self_defined_info(dep)
        assert info["_raw_stdio"]["args"] == ["--verbose", "--output=json"]
        assert info["_raw_stdio"]["env"] == {"TOKEN": "secret"}

    def test_http_transport_builds_remote(self):
        dep = _make_self_defined(
            "remote-svc", transport="http", url="https://example.com/mcp"
        )
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "remotes" in info
        assert info["remotes"][0]["url"] == "https://example.com/mcp"
        assert info["remotes"][0]["transport_type"] == "http"
        assert "packages" not in info

    def test_sse_transport_builds_remote(self):
        dep = _make_self_defined(
            "sse-svc", transport="sse", url="https://example.com/sse"
        )
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "remotes" in info

    def test_http_with_headers(self):
        dep = _make_self_defined(
            "headered-svc",
            transport="http",
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer tok"},
        )
        info = MCPIntegrator._build_self_defined_info(dep)
        assert info["remotes"][0]["headers"] == [{"name": "Authorization", "value": "Bearer tok"}]

    def test_stdio_no_command_uses_name(self):
        dep = _make_self_defined("auto-named", transport="stdio")
        info = MCPIntegrator._build_self_defined_info(dep)
        assert info["_raw_stdio"]["command"] == "auto-named"

    def test_tools_override_embedded(self):
        dep = _make_self_defined("tool-svc", transport="stdio", tools=["read", "write"])
        info = MCPIntegrator._build_self_defined_info(dep)
        assert info["_apm_tools_override"] == ["read", "write"]

    def test_stdio_env_vars_in_packages(self):
        dep = _make_self_defined(
            "pkg-svc",
            transport="stdio",
            command="pkg-svc",
            env={"KEY": "val"},
        )
        info = MCPIntegrator._build_self_defined_info(dep)
        # stdio deps without raw must emit a packages entry; assert presence
        # first so a regression that drops `packages` for stdio is caught
        # rather than silently passing.
        packages = info.get("packages", [])
        assert packages, "stdio dep must produce a non-empty packages list"
        env_vars = packages[0].get("environment_variables", [])
        assert any(e["name"] == "KEY" for e in env_vars)

    def test_no_tools_no_override_key(self):
        dep = _make_self_defined("simple", transport="stdio")
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "_apm_tools_override" not in info

    def test_list_args_in_packages(self):
        dep = _make_self_defined(
            "args-svc",
            transport="stdio",
            command="args-svc",
            args=["--arg1", "--arg2"],
        )
        info = MCPIntegrator._build_self_defined_info(dep)
        packages = info.get("packages", [])
        assert packages, "stdio dep with args must produce a non-empty packages list"
        rt_args = packages[0].get("runtime_arguments", [])
        hints = [a["value_hint"] for a in rt_args]
        assert "--arg1" in hints


# ===========================================================================
# MCPIntegrator._apply_overlay
# ===========================================================================


class TestApplyOverlay:
    def _base_cache_with_both(self, name):
        return {
            name: {
                "name": name,
                "packages": [{"runtime_hint": name, "runtime_arguments": [], "registry_name": "npm"}],
                "remotes": [{"transport_type": "sse", "url": "https://example.com"}],
            }
        }

    def test_stdio_transport_removes_remotes(self):
        cache = self._base_cache_with_both("svc")
        dep = _make_dep("svc", transport="stdio")
        MCPIntegrator._apply_overlay(cache, dep)
        assert "remotes" not in cache["svc"]
        assert "packages" in cache["svc"]

    def test_http_transport_removes_packages(self):
        cache = self._base_cache_with_both("svc")
        dep = _make_dep("svc", transport="http")
        MCPIntegrator._apply_overlay(cache, dep)
        assert "packages" not in cache["svc"]
        assert "remotes" in cache["svc"]

    def test_package_filter_by_registry(self):
        cache = {
            "svc": {
                "name": "svc",
                "packages": [
                    {"registry_name": "npm"},
                    {"registry_name": "pypi"},
                ],
            }
        }
        dep = _make_dep("svc", package="npm")
        MCPIntegrator._apply_overlay(cache, dep)
        assert len(cache["svc"]["packages"]) == 1
        assert cache["svc"]["packages"][0]["registry_name"] == "npm"

    def test_headers_appended_to_remotes(self):
        cache = {
            "svc": {
                "name": "svc",
                "remotes": [{"transport_type": "sse", "headers": []}],
            }
        }
        dep = _make_dep("svc", headers={"X-Token": "abc"})
        MCPIntegrator._apply_overlay(cache, dep)
        headers = cache["svc"]["remotes"][0]["headers"]
        assert {"name": "X-Token", "value": "abc"} in headers

    def test_tools_overlay_set(self):
        cache = {"svc": {"name": "svc"}}
        dep = _make_dep("svc", tools=["list", "get"])
        MCPIntegrator._apply_overlay(cache, dep)
        assert cache["svc"]["_apm_tools_override"] == ["list", "get"]

    def test_noop_for_unknown_server(self):
        cache = {}
        dep = _make_dep("ghost", transport="stdio")
        # Should not raise
        MCPIntegrator._apply_overlay(cache, dep)

    def test_list_args_appended_to_packages(self):
        cache = {
            "svc": {
                "name": "svc",
                "packages": [{"runtime_arguments": []}],
            }
        }
        dep = _make_dep("svc", args=["--extra"])
        MCPIntegrator._apply_overlay(cache, dep)
        rt_args = cache["svc"]["packages"][0]["runtime_arguments"]
        assert any(a.get("value_hint") == "--extra" for a in rt_args)

    def test_dict_args_appended_as_flags(self):
        cache = {
            "svc": {
                "name": "svc",
                "packages": [{"runtime_arguments": []}],
            }
        }
        dep = _make_dep("svc", args={"key": "value"})
        MCPIntegrator._apply_overlay(cache, dep)
        rt_args = cache["svc"]["packages"][0]["runtime_arguments"]
        assert any("--key=value" in a.get("value_hint", "") for a in rt_args)

    def test_version_overlay_emits_warning(self):
        cache = {"svc": {"name": "svc"}}
        dep = _make_dep("svc", version="1.2.3")
        with pytest.warns(UserWarning, match="version"):
            MCPIntegrator._apply_overlay(cache, dep)

    def test_registry_str_overlay_emits_warning(self):
        cache = {"svc": {"name": "svc"}}
        dep = _make_dep("svc", registry="https://custom.registry.example.com")
        with pytest.warns(UserWarning, match="registry"):
            MCPIntegrator._apply_overlay(cache, dep)


# ===========================================================================
# MCPIntegrator.update_lockfile
# ===========================================================================


class TestUpdateLockfile:
    def _write_minimal_lockfile(self, path: Path) -> None:
        content = "lockfile_version: '1'\ngenerated_at: '2026-01-01'\ndependencies: []\n"
        path.write_text(content, encoding="utf-8")

    def test_updates_mcp_servers_in_lockfile(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        self._write_minimal_lockfile(lock_path)

        MCPIntegrator.update_lockfile({"server-a", "server-b"}, lock_path=lock_path)

        from apm_cli.deps.lockfile import LockFile
        lf = LockFile.read(lock_path)
        assert set(lf.mcp_servers) == {"server-a", "server-b"}

    def test_updates_mcp_configs_when_provided(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        self._write_minimal_lockfile(lock_path)
        configs = {"server-a": {"name": "server-a", "transport": "stdio"}}

        MCPIntegrator.update_lockfile({"server-a"}, lock_path=lock_path, mcp_configs=configs)

        from apm_cli.deps.lockfile import LockFile
        lf = LockFile.read(lock_path)
        assert lf.mcp_configs == configs

    def test_noop_when_lockfile_missing(self, tmp_path):
        # Should not raise even if lockfile doesn't exist
        missing = tmp_path / "no_lock.yaml"
        MCPIntegrator.update_lockfile({"svc"}, lock_path=missing)  # no error

    def test_mcp_servers_sorted_in_lockfile(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        self._write_minimal_lockfile(lock_path)

        MCPIntegrator.update_lockfile({"z-svc", "a-svc", "m-svc"}, lock_path=lock_path)

        from apm_cli.deps.lockfile import LockFile
        lf = LockFile.read(lock_path)
        assert lf.mcp_servers == sorted(lf.mcp_servers)

    def test_empty_server_set_clears_mcp_servers(self, tmp_path):
        lock_path = tmp_path / "apm.lock.yaml"
        # Pre-populate
        self._write_minimal_lockfile(lock_path)
        MCPIntegrator.update_lockfile({"existing"}, lock_path=lock_path)

        MCPIntegrator.update_lockfile(set(), lock_path=lock_path)

        from apm_cli.deps.lockfile import LockFile
        lf = LockFile.read(lock_path)
        assert lf.mcp_servers == []


# ===========================================================================
# MCPIntegrator.remove_stale - vscode
# ===========================================================================


class TestRemoveStaleVscode:
    def _write_vscode_mcp(self, path: Path, servers: dict) -> None:
        path.mkdir(parents=True, exist_ok=True)
        mcp_json = path / "mcp.json"
        mcp_json.write_text(json.dumps({"servers": servers}), encoding="utf-8")

    def test_removes_stale_server_from_vscode(self, tmp_path):
        vscode_dir = tmp_path / ".vscode"
        self._write_vscode_mcp(vscode_dir, {"old-server": {}, "keep-server": {}})

        with patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path), \
             patch("pathlib.Path.home", return_value=tmp_path):
            MCPIntegrator.remove_stale({"old-server"}, runtime="vscode")

        remaining = json.loads((vscode_dir / "mcp.json").read_text())
        assert "old-server" not in remaining["servers"]
        assert "keep-server" in remaining["servers"]

    def test_short_name_matched_for_path_reference(self, tmp_path):
        vscode_dir = tmp_path / ".vscode"
        self._write_vscode_mcp(vscode_dir, {"github-mcp-server": {}, "keep": {}})

        with patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path), \
             patch("pathlib.Path.home", return_value=tmp_path):
            MCPIntegrator.remove_stale(
                {"io.github.github/github-mcp-server"}, runtime="vscode"
            )

        remaining = json.loads((vscode_dir / "mcp.json").read_text())
        assert "github-mcp-server" not in remaining["servers"]

    def test_empty_stale_set_is_noop(self, tmp_path):
        vscode_dir = tmp_path / ".vscode"
        self._write_vscode_mcp(vscode_dir, {"server": {}})
        original_mtime = (vscode_dir / "mcp.json").stat().st_mtime

        with patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path), \
             patch("pathlib.Path.home", return_value=tmp_path):
            MCPIntegrator.remove_stale(set(), runtime="vscode")

        # File unchanged when stale set is empty (early return)
        assert (vscode_dir / "mcp.json").stat().st_mtime == original_mtime

    def test_missing_vscode_mcp_json_is_noop(self, tmp_path):
        # No .vscode/mcp.json at all - should not raise
        with patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path), \
             patch("pathlib.Path.home", return_value=tmp_path):
            MCPIntegrator.remove_stale({"ghost"}, runtime="vscode")

    def test_target_restricted_to_requested_runtime(self, tmp_path):
        vscode_dir = tmp_path / ".vscode"
        self._write_vscode_mcp(vscode_dir, {"stale": {}})
        copilot_dir = tmp_path / ".copilot"
        copilot_dir.mkdir()
        copilot_mcp = copilot_dir / "mcp-config.json"
        copilot_mcp.write_text(json.dumps({"mcpServers": {"stale": {}}}), encoding="utf-8")

        with patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path), \
             patch("pathlib.Path.home", return_value=tmp_path):
            MCPIntegrator.remove_stale({"stale"}, runtime="vscode")

        # vscode cleaned; copilot untouched
        vscode_remaining = json.loads((vscode_dir / "mcp.json").read_text())
        assert "stale" not in vscode_remaining["servers"]
        copilot_remaining = json.loads(copilot_mcp.read_text())
        assert "stale" in copilot_remaining["mcpServers"]

    def test_removes_stale_server_from_vscode_with_explicit_project_root(self, tmp_path):
        nested = tmp_path / "nested-project"
        vscode_dir = nested / ".vscode"
        self._write_vscode_mcp(vscode_dir, {"old-server": {}, "keep-server": {}})

        with patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path), \
             patch("pathlib.Path.home", return_value=tmp_path):
            MCPIntegrator.remove_stale(
                {"old-server"},
                runtime="vscode",
                project_root=nested,
            )

        remaining = json.loads((vscode_dir / "mcp.json").read_text())
        assert "old-server" not in remaining["servers"]
        assert "keep-server" in remaining["servers"]


class TestInstallProjectRootDetection:
    @patch("apm_cli.registry.operations.MCPServerOperations")
    @patch("apm_cli.integration.mcp_integrator.MCPIntegrator._install_for_runtime")
    @patch("apm_cli.runtime.manager.RuntimeManager")
    @patch("apm_cli.integration.mcp_integrator.shutil.which", return_value=None)
    def test_install_uses_explicit_project_root_for_workspace_runtime_detection(
        self, _which, mock_mgr_cls, mock_install_rt, mock_ops_cls, tmp_path
    ):
        nested = tmp_path / "nested-project"
        (nested / ".cursor").mkdir(parents=True)
        (nested / ".opencode").mkdir()
        (nested / ".vscode").mkdir()

        mock_mgr = mock_mgr_cls.return_value
        mock_mgr.is_runtime_available.return_value = False
        mock_install_rt.return_value = True

        mock_ops = mock_ops_cls.return_value
        mock_ops.validate_servers_exist.return_value = (["test/server"], [])
        mock_ops.check_servers_needing_installation.return_value = ["test/server"]
        mock_ops.batch_fetch_server_info.return_value = {"test/server": {}}
        mock_ops.collect_environment_variables.return_value = {}
        mock_ops.collect_runtime_variables.return_value = {}

        with patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path):
            MCPIntegrator.install(
                mcp_deps=["test/server"],
                project_root=nested,
                apm_config={},
            )

        called_runtimes = {call.args[0] for call in mock_install_rt.call_args_list}
        assert "vscode" in called_runtimes
        assert "cursor" in called_runtimes
        assert "opencode" in called_runtimes


# ===========================================================================
# MCPIntegrator.remove_stale - copilot
# ===========================================================================


class TestRemoveStaleCopilot:
    def _write_copilot_mcp(self, home: Path, servers: dict) -> Path:
        copilot_dir = home / ".copilot"
        copilot_dir.mkdir(parents=True, exist_ok=True)
        cfg = copilot_dir / "mcp-config.json"
        cfg.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        return cfg

    def test_removes_stale_from_copilot(self, tmp_path):
        cfg = self._write_copilot_mcp(tmp_path, {"old": {}, "keep": {}})

        with patch("apm_cli.integration.mcp_integrator.Path.cwd", return_value=tmp_path), \
             patch("pathlib.Path.home", return_value=tmp_path):
            MCPIntegrator.remove_stale({"old"}, runtime="copilot")

        remaining = json.loads(cfg.read_text())
        assert "old" not in remaining["mcpServers"]
        assert "keep" in remaining["mcpServers"]


# ===========================================================================
# MCPIntegrator.collect_transitive - edge cases
# ===========================================================================


class TestCollectTransitive:
    def test_returns_empty_when_dir_missing(self, tmp_path):
        missing = tmp_path / "nonexistent"
        result = MCPIntegrator.collect_transitive(missing)
        assert result == []

    def test_returns_empty_when_dir_empty(self, tmp_path):
        result = MCPIntegrator.collect_transitive(tmp_path)
        assert result == []
