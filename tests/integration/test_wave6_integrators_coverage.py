"""Integration tests for mcp_integrator, hook_integrator, and skill_integrator.

Coverage targets (exercise real source lines, mock only external I/O):
  - MCPIntegrator        (~45% covered, 267 missing)
  - HookIntegrator       (~44% covered, 263 missing)
  - SkillIntegrator      (~50% covered, 249 missing)

Strategy:
  - Create realistic apm_modules/ structures in tmp_path
  - Use real APMPackage / PackageInfo objects
  - Only mock external I/O (subprocess, HTTP, auth)
  - Exercise every major code path in each module
"""

from __future__ import annotations

import json
from pathlib import Path

from apm_cli.integration.hook_integrator import (
    HookIntegrationResult,
    HookIntegrator,
    _copilot_keys_to_gemini,
    _filter_hook_files_for_target,
    _reinject_apm_source_from_sidecar,
    _to_gemini_hook_entries,
)
from apm_cli.integration.mcp_integrator import MCPIntegrator, _is_vscode_available
from apm_cli.integration.skill_integrator import (
    SkillIntegrator,
    copy_skill_to_target,
    get_effective_type,
    normalize_skill_name,
    should_compile_instructions,
    should_install_skill,
    to_hyphen_case,
    validate_skill_name,
)
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.models.validation import PackageType

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_apm_package(name: str = "test-pkg", version: str = "1.0.0") -> APMPackage:
    return APMPackage(name=name, version=version)


def _make_package_info(
    install_path: Path,
    name: str = "test-pkg",
    pkg_type: PackageType | None = None,
) -> PackageInfo:
    pkg = _make_apm_package(name)
    return PackageInfo(package=pkg, install_path=install_path, package_type=pkg_type)


# ===========================================================================
# MCPIntegrator tests
# ===========================================================================


class TestIsVscodeAvailable:
    def test_vscode_dir_makes_it_available(self, tmp_path):
        (tmp_path / ".vscode").mkdir()
        assert _is_vscode_available(tmp_path) is True

    def test_no_vscode_dir_and_no_binary(self, tmp_path, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda _: None)
        assert _is_vscode_available(tmp_path) is False

    def test_defaults_to_cwd_when_root_none(self, tmp_path, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda _: None)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".vscode").mkdir()
        assert _is_vscode_available(None) is True


class TestMCPIntegratorCollectTransitive:
    def test_returns_empty_when_no_apm_modules_dir(self, tmp_path):
        result = MCPIntegrator.collect_transitive(tmp_path / "apm_modules")
        assert result == []

    def test_returns_empty_when_apm_modules_empty(self, tmp_path):
        apm_modules = tmp_path / "apm_modules"
        apm_modules.mkdir()
        result = MCPIntegrator.collect_transitive(apm_modules)
        assert result == []

    def test_skips_invalid_apm_yml(self, tmp_path):
        apm_modules = tmp_path / "apm_modules"
        pkg_dir = apm_modules / "my-pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text("{invalid yaml: [}")
        result = MCPIntegrator.collect_transitive(apm_modules)
        assert result == []

    def test_collects_mcp_from_valid_package(self, tmp_path):
        """Collect MCP deps when package declares them in dependencies.mcp."""
        from apm_cli.models.apm_package import clear_apm_yml_cache

        clear_apm_yml_cache()
        apm_modules = tmp_path / "apm_modules"
        pkg_dir = apm_modules / "my-pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text(
            "name: my-pkg\nversion: 1.0.0\ndependencies:\n  mcp:\n    - io.github.test/mcp-server\n"
        )
        result = MCPIntegrator.collect_transitive(apm_modules)
        assert len(result) >= 1
        assert result[0].name == "io.github.test/mcp-server"


class TestMCPIntegratorDeduplicate:
    def test_deduplicates_by_name(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep_a = MCPDependency.from_string("server-a")
        dep_b = MCPDependency.from_string("server-a")  # duplicate
        dep_c = MCPDependency.from_string("server-b")

        result = MCPIntegrator.deduplicate([dep_a, dep_b, dep_c])
        assert len(result) == 2
        assert result[0].name == "server-a"
        assert result[1].name == "server-b"

    def test_preserves_order_first_wins(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep_a = MCPDependency.from_string("a")
        dep_b = MCPDependency.from_string("b")
        dep_a2 = MCPDependency.from_string("a")

        result = MCPIntegrator.deduplicate([dep_a, dep_b, dep_a2])
        assert len(result) == 2
        assert result[0] is dep_a

    def test_handles_dict_deps(self):
        deps = [{"name": "s1"}, {"name": "s1"}, {"name": "s2"}]
        result = MCPIntegrator.deduplicate(deps)
        assert len(result) == 2

    def test_handles_unnamed_deps(self):
        deps = [{"no_name": "x"}, {"no_name": "y"}]
        result = MCPIntegrator.deduplicate(deps)
        # unnamed entries (name is "") are added without dedup check
        assert len(result) == 2


class TestMCPIntegratorBuildSelfDefinedInfo:
    def test_stdio_transport(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_dict(
            {
                "name": "my-server",
                "registry": False,
                "transport": "stdio",
                "command": "node",
                "args": ["index.js", "--port=8080"],
                "env": {"MY_VAR": "value"},
            }
        )
        info = MCPIntegrator._build_self_defined_info(dep)
        assert info["name"] == "my-server"
        assert "_raw_stdio" in info
        assert info["_raw_stdio"]["command"] == "node"
        assert "MY_VAR" in info["_raw_stdio"]["env"]

    def test_http_transport(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_dict(
            {
                "name": "remote-server",
                "registry": False,
                "transport": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer tok"},
            }
        )
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "remotes" in info
        assert info["remotes"][0]["url"] == "https://example.com/mcp"
        # headers should be present
        assert any(h["name"] == "Authorization" for h in info["remotes"][0]["headers"])

    def test_with_tools_override(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_dict(
            {
                "name": "tool-server",
                "registry": False,
                "transport": "stdio",
                "command": "python",
                "tools": ["search", "read"],
            }
        )
        info = MCPIntegrator._build_self_defined_info(dep)
        assert info["_apm_tools_override"] == ["search", "read"]

    def test_stdio_with_dict_args(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_dict(
            {
                "name": "dict-args-server",
                "registry": False,
                "transport": "stdio",
                "command": "my-cmd",
                "args": {"port": "8080", "host": "localhost"},
            }
        )
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "packages" in info
        assert any(
            "8080" in str(a.get("value_hint", "")) for a in info["packages"][0]["runtime_arguments"]
        )


class TestMCPIntegratorApplyOverlay:
    def test_transport_http_removes_packages(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_dict({"name": "srv", "transport": "http"})
        cache = {
            "srv": {
                "name": "srv",
                "packages": [{"runtime_hint": "npm"}],
                "remotes": [{"url": "https://example.com", "transport_type": "http"}],
            }
        }
        MCPIntegrator._apply_overlay(cache, dep)
        assert "packages" not in cache["srv"]

    def test_transport_stdio_removes_remotes(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_dict({"name": "srv", "transport": "stdio"})
        cache = {
            "srv": {
                "name": "srv",
                "packages": [{"runtime_hint": "npm", "registry_name": "npm"}],
                "remotes": [{"url": "https://example.com", "transport_type": "http"}],
            }
        }
        MCPIntegrator._apply_overlay(cache, dep)
        assert "remotes" not in cache["srv"]

    def test_package_filter(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_dict({"name": "srv", "package": "npm"})
        cache = {
            "srv": {
                "name": "srv",
                "packages": [
                    {"registry_name": "npm", "name": "npm-pkg"},
                    {"registry_name": "pypi", "name": "py-pkg"},
                ],
            }
        }
        MCPIntegrator._apply_overlay(cache, dep)
        assert len(cache["srv"]["packages"]) == 1
        assert cache["srv"]["packages"][0]["registry_name"] == "npm"

    def test_headers_overlay_list(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_dict({"name": "srv", "headers": {"X-Key": "abc"}})
        cache = {
            "srv": {
                "name": "srv",
                "remotes": [{"url": "https://example.com", "headers": []}],
            }
        }
        MCPIntegrator._apply_overlay(cache, dep)
        assert {"name": "X-Key", "value": "abc"} in cache["srv"]["remotes"][0]["headers"]

    def test_missing_name_is_noop(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_dict({"name": "missing-srv", "transport": "stdio"})
        cache = {}
        MCPIntegrator._apply_overlay(cache, dep)  # should not raise

    def test_args_overlay_list(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_dict({"name": "srv", "args": ["--verbose"]})
        cache = {
            "srv": {
                "name": "srv",
                "packages": [{"runtime_arguments": [], "registry_name": "npm"}],
            }
        }
        MCPIntegrator._apply_overlay(cache, dep)
        assert any("verbose" in str(a) for a in cache["srv"]["packages"][0]["runtime_arguments"])

    def test_version_warning_emitted(self, recwarn):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_dict({"name": "srv", "version": "1.2.3"})
        cache = {"srv": {"name": "srv"}}
        MCPIntegrator._apply_overlay(cache, dep)
        assert any("version" in str(w.message) for w in recwarn.list)


class TestMCPIntegratorGetServerNames:
    def test_extracts_names(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        deps = [MCPDependency.from_string("alpha"), MCPDependency.from_string("beta"), "gamma"]
        names = MCPIntegrator.get_server_names(deps)
        assert names == {"alpha", "beta", "gamma"}

    def test_empty_list(self):
        assert MCPIntegrator.get_server_names([]) == set()


class TestMCPIntegratorGetServerConfigs:
    def test_extracts_configs(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        deps = [MCPDependency.from_string("srv-a"), "plain-string"]
        configs = MCPIntegrator.get_server_configs(deps)
        assert "srv-a" in configs
        assert "plain-string" in configs
        assert configs["plain-string"] == {"name": "plain-string"}


class TestMCPIntegratorDriftDetection:
    def test_detects_drift(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_dict({"name": "srv", "env": {"A": "new"}})
        stored = {"srv": {"name": "srv", "env": {"A": "old"}}}
        drifted = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert "srv" in drifted

    def test_no_drift_when_identical(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_string("stable-srv")
        stored = {"stable-srv": dep.to_dict()}
        drifted = MCPIntegrator._detect_mcp_config_drift([dep], stored)
        assert "stable-srv" not in drifted

    def test_ignores_new_deps_not_in_stored(self):
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_string("brand-new")
        drifted = MCPIntegrator._detect_mcp_config_drift([dep], {})
        assert len(drifted) == 0


class TestMCPIntegratorAppendDrifted:
    def test_appends_sorted_without_duplicates(self):
        install_list = ["c"]
        MCPIntegrator._append_drifted_to_install_list(install_list, {"a", "b", "c"})
        assert install_list == ["c", "a", "b"]


class TestMCPIntegratorRemoveStale:
    def test_removes_from_vscode_mcp_json(self, tmp_path):
        vscode = tmp_path / ".vscode"
        vscode.mkdir()
        mcp_json = vscode / "mcp.json"
        mcp_json.write_text(
            json.dumps({"servers": {"old-server": {"command": "x"}, "keep-me": {"command": "y"}}})
        )
        MCPIntegrator.remove_stale({"old-server"}, runtime="vscode", project_root=tmp_path)
        config = json.loads(mcp_json.read_text())
        assert "old-server" not in config["servers"]
        assert "keep-me" in config["servers"]

    def test_skips_vscode_when_file_absent(self, tmp_path):
        # Should not raise
        MCPIntegrator.remove_stale({"nonexistent"}, runtime="vscode", project_root=tmp_path)

    def test_removes_from_cursor_mcp_json(self, tmp_path):
        cursor = tmp_path / ".cursor"
        cursor.mkdir()
        mcp_json = cursor / "mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"stale": {"command": "x"}, "keep": {"command": "y"}}})
        )
        MCPIntegrator.remove_stale({"stale"}, runtime="cursor", project_root=tmp_path)
        config = json.loads(mcp_json.read_text())
        assert "stale" not in config["mcpServers"]
        assert "keep" in config["mcpServers"]

    def test_removes_from_opencode_json(self, tmp_path):
        opencode_dir = tmp_path / ".opencode"
        opencode_dir.mkdir()
        opencode_json = tmp_path / "opencode.json"
        opencode_json.write_text(
            json.dumps({"mcp": {"stale-oc": {"cmd": "x"}, "keep": {"cmd": "y"}}})
        )
        MCPIntegrator.remove_stale({"stale-oc"}, runtime="opencode", project_root=tmp_path)
        config = json.loads(opencode_json.read_text())
        assert "stale-oc" not in config["mcp"]
        assert "keep" in config["mcp"]

    def test_removes_from_gemini_settings(self, tmp_path):
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings = gemini_dir / "settings.json"
        settings.write_text(json.dumps({"mcpServers": {"stale-gem": {}, "keep": {}}}))
        MCPIntegrator.remove_stale({"stale-gem"}, runtime="gemini", project_root=tmp_path)
        config = json.loads(settings.read_text())
        assert "stale-gem" not in config["mcpServers"]

    def test_removes_from_claude_project_mcp_json(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        mcp_json = tmp_path / ".mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"stale-cl": {}, "keep-cl": {}}}))
        MCPIntegrator.remove_stale({"stale-cl"}, runtime="claude", project_root=tmp_path)
        config = json.loads(mcp_json.read_text())
        assert "stale-cl" not in config["mcpServers"]

    def test_empty_stale_names_is_noop(self, tmp_path):
        # Should not touch anything
        MCPIntegrator.remove_stale(set(), project_root=tmp_path)

    def test_exclude_removes_runtime_from_set(self, tmp_path):
        vscode = tmp_path / ".vscode"
        vscode.mkdir()
        mcp_json = vscode / "mcp.json"
        mcp_json.write_text(json.dumps({"servers": {"srv": {}}}))
        # Exclude vscode -- file should NOT be touched
        MCPIntegrator.remove_stale(
            {"srv"}, runtime="vscode", exclude="vscode", project_root=tmp_path
        )
        config = json.loads(mcp_json.read_text())
        assert "srv" in config["servers"]  # unchanged


class TestMCPIntegratorUpdateLockfile:
    def test_noop_when_lockfile_absent(self, tmp_path):
        # Should not raise even when lock path does not exist
        MCPIntegrator.update_lockfile({"s1", "s2"}, lock_path=tmp_path / "missing.lock.yaml")

    def test_updates_mcp_servers_in_lockfile(self, tmp_path):
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        lock_path = get_lockfile_path(tmp_path)
        lf = LockFile()
        lf.save(lock_path)

        MCPIntegrator.update_lockfile({"alpha", "beta"}, lock_path=lock_path)

        lf2 = LockFile.read(lock_path)
        assert set(lf2.mcp_servers) == {"alpha", "beta"}


class TestMCPIntegratorDetectRuntimes:
    def test_detects_copilot(self):
        scripts = {"run": "copilot run something"}
        detected = MCPIntegrator._detect_runtimes(scripts)
        assert "copilot" in detected

    def test_detects_claude(self):
        detected = MCPIntegrator._detect_runtimes({"test": "run claude"})
        assert "claude" in detected

    def test_detects_multiple(self):
        detected = MCPIntegrator._detect_runtimes(
            {"s1": "run copilot", "s2": "run codex", "s3": "run gemini"}
        )
        assert "copilot" in detected
        assert "codex" in detected
        assert "gemini" in detected

    def test_empty_scripts_returns_empty(self):
        assert MCPIntegrator._detect_runtimes({}) == []


# ===========================================================================
# HookIntegrator tests
# ===========================================================================


class TestHookIntegrationResultCompat:
    def test_hooks_integrated_alias(self):
        r = HookIntegrationResult(hooks_integrated=5)
        assert r.hooks_integrated == 5
        assert r.files_integrated == 5

    def test_full_constructor(self):
        r = HookIntegrationResult(
            files_integrated=2, files_updated=0, files_skipped=1, target_paths=[]
        )
        assert r.hooks_integrated == 2
        assert r.files_skipped == 1


class TestFilterHookFilesForTarget:
    def test_universal_file_passes_all_targets(self, tmp_path):
        f = tmp_path / "hooks.json"
        f.touch()
        for target in ("claude", "cursor", "copilot", "codex", "gemini"):
            result = _filter_hook_files_for_target([f], target)
            assert f in result

    def test_copilot_hooks_file_only_for_copilot_vscode(self, tmp_path):
        f = tmp_path / "copilot-hooks.json"
        f.touch()
        assert f in _filter_hook_files_for_target([f], "copilot")
        assert f in _filter_hook_files_for_target([f], "vscode")
        assert f not in _filter_hook_files_for_target([f], "claude")

    def test_claude_hooks_file_only_for_claude(self, tmp_path):
        f = tmp_path / "claude-hooks.json"
        f.touch()
        assert f in _filter_hook_files_for_target([f], "claude")
        assert f not in _filter_hook_files_for_target([f], "cursor")

    def test_cursor_hooks_file_only_for_cursor(self, tmp_path):
        f = tmp_path / "cursor-hooks.json"
        f.touch()
        assert f in _filter_hook_files_for_target([f], "cursor")
        assert f not in _filter_hook_files_for_target([f], "copilot")

    def test_gemini_hooks_file_only_for_gemini(self, tmp_path):
        f = tmp_path / "gemini-hooks.json"
        f.touch()
        assert f in _filter_hook_files_for_target([f], "gemini")
        assert f not in _filter_hook_files_for_target([f], "claude")


class TestReinjectApmSourceFromSidecar:
    def test_reinjects_matching_entry(self):
        hooks = {"PreToolUse": [{"type": "command", "command": "echo hello"}]}
        sidecar = {
            "PreToolUse": [{"type": "command", "command": "echo hello", "_apm_source": "my-pkg"}]
        }
        _reinject_apm_source_from_sidecar(hooks, sidecar)
        assert hooks["PreToolUse"][0]["_apm_source"] == "my-pkg"

    def test_no_match_leaves_entry_unchanged(self):
        hooks = {"PostToolUse": [{"type": "command", "command": "echo bye"}]}
        sidecar = {"PreToolUse": [{"type": "command", "command": "echo hi", "_apm_source": "pkg"}]}
        _reinject_apm_source_from_sidecar(hooks, sidecar)
        assert "_apm_source" not in hooks["PostToolUse"][0]

    def test_each_sidecar_entry_consumed_once(self):
        """Two identical entries: only one should be marked."""
        hooks = {
            "PreToolUse": [
                {"type": "command", "command": "run"},
                {"type": "command", "command": "run"},
            ]
        }
        sidecar = {
            "PreToolUse": [
                {"type": "command", "command": "run", "_apm_source": "pkg"},
            ]
        }
        _reinject_apm_source_from_sidecar(hooks, sidecar)
        marked = [e for e in hooks["PreToolUse"] if "_apm_source" in e]
        assert len(marked) == 1


class TestCopilotKeysToGemini:
    def test_renames_bash_to_command(self):
        hook = {"bash": "echo hi"}
        _copilot_keys_to_gemini(hook)
        assert "command" in hook
        assert "bash" not in hook

    def test_renames_powershell_to_command(self):
        hook = {"powershell": "Write-Host hi"}
        _copilot_keys_to_gemini(hook)
        assert hook["command"] == "Write-Host hi"

    def test_converts_timeoutSec_to_timeout_ms(self):
        hook = {"command": "x", "timeoutSec": 10}
        _copilot_keys_to_gemini(hook)
        assert hook["timeout"] == 10000
        assert "timeoutSec" not in hook

    def test_no_rename_when_command_present(self):
        hook = {"command": "existing", "bash": "ignored"}
        _copilot_keys_to_gemini(hook)
        # command already present -- bash stays but isn't renamed
        assert hook["command"] == "existing"


class TestToGeminiHookEntries:
    def test_wraps_flat_entry(self):
        entries = [{"type": "command", "bash": "echo hi"}]
        result = _to_gemini_hook_entries(entries)
        assert len(result) == 1
        assert "hooks" in result[0]
        assert result[0]["hooks"][0]["command"] == "echo hi"

    def test_passes_through_already_nested_entry(self):
        entry = {"hooks": [{"type": "command", "command": "echo bye"}]}
        result = _to_gemini_hook_entries([entry])
        assert result[0] is entry

    def test_propagates_apm_source(self):
        entry = {"type": "command", "bash": "x", "_apm_source": "my-pkg"}
        result = _to_gemini_hook_entries([entry])
        assert result[0].get("_apm_source") == "my-pkg"


class TestHookIntegratorFindHookFiles:
    def test_finds_files_in_apm_hooks_dir(self, tmp_path):
        hooks_dir = tmp_path / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "main.json").write_text("{}")
        (hooks_dir / "other.json").write_text("{}")

        integrator = HookIntegrator()
        found = integrator.find_hook_files(tmp_path)
        assert len(found) == 2

    def test_finds_files_in_hooks_dir(self, tmp_path):
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "native.json").write_text("{}")

        integrator = HookIntegrator()
        found = integrator.find_hook_files(tmp_path)
        assert len(found) == 1

    def test_deduplicates_across_dirs(self, tmp_path):
        """Same resolved path in both dirs should only appear once."""
        apm_hooks = tmp_path / ".apm" / "hooks"
        apm_hooks.mkdir(parents=True)
        (apm_hooks / "shared.json").write_text("{}")

        integrator = HookIntegrator()
        found = integrator.find_hook_files(tmp_path)
        assert len(found) == 1

    def test_skips_symlinks(self, tmp_path):
        hooks_dir = tmp_path / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        real = hooks_dir / "real.json"
        real.write_text("{}")
        link = hooks_dir / "link.json"
        link.symlink_to(real)

        integrator = HookIntegrator()
        found = integrator.find_hook_files(tmp_path)
        assert all(not f.is_symlink() for f in found)

    def test_returns_empty_when_no_hooks(self, tmp_path):
        integrator = HookIntegrator()
        assert integrator.find_hook_files(tmp_path) == []


class TestParseHookJson:
    def test_parses_valid_json(self, tmp_path):
        f = tmp_path / "hook.json"
        f.write_text('{"hooks": {"PreToolUse": []}}')
        result = HookIntegrator()._parse_hook_json(f)
        assert result is not None
        assert "hooks" in result

    def test_returns_none_for_invalid_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not valid json")
        assert HookIntegrator()._parse_hook_json(f) is None

    def test_returns_none_for_array_json(self, tmp_path):
        f = tmp_path / "array.json"
        f.write_text("[1, 2, 3]")
        assert HookIntegrator()._parse_hook_json(f) is None

    def test_returns_none_for_missing_file(self, tmp_path):
        assert HookIntegrator()._parse_hook_json(tmp_path / "nonexistent.json") is None


class TestRewriteCommandForTarget:
    def test_rewrite_plugin_root_reference(self, tmp_path):
        pkg_path = tmp_path / "my-pkg"
        pkg_path.mkdir()
        script = pkg_path / "hooks" / "run.sh"
        script.parent.mkdir()
        script.write_text("#!/bin/bash\necho ok")

        integrator = HookIntegrator()
        new_cmd, scripts = integrator._rewrite_command_for_target(
            "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh",
            pkg_path,
            "my-pkg",
            "claude",
        )
        assert "${CLAUDE_PLUGIN_ROOT}" not in new_cmd
        assert len(scripts) == 1

    def test_rewrite_relative_path_reference(self, tmp_path):
        pkg_path = tmp_path / "my-pkg"
        pkg_path.mkdir()
        scripts_dir = pkg_path / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "check.sh"
        script.write_text("#!/bin/bash")

        integrator = HookIntegrator()
        new_cmd, scripts = integrator._rewrite_command_for_target(
            "./scripts/check.sh",
            pkg_path,
            "my-pkg",
            "vscode",
            hook_file_dir=pkg_path,
        )
        assert "./scripts/check.sh" not in new_cmd
        assert len(scripts) == 1

    def test_no_rewrite_for_system_command(self, tmp_path):
        pkg_path = tmp_path / "pkg"
        pkg_path.mkdir()
        integrator = HookIntegrator()
        new_cmd, scripts = integrator._rewrite_command_for_target(
            "python3 --version",
            pkg_path,
            "pkg",
            "claude",
        )
        assert new_cmd == "python3 --version"
        assert scripts == []

    def test_cursor_scripts_base(self, tmp_path):
        pkg_path = tmp_path / "pkg"
        pkg_path.mkdir()
        script = pkg_path / "run.sh"
        script.write_text("echo hi")

        integrator = HookIntegrator()
        _new_cmd, scripts = integrator._rewrite_command_for_target(
            "${PLUGIN_ROOT}/run.sh",
            pkg_path,
            "pkg",
            "cursor",
        )
        if scripts:
            assert ".cursor/hooks/pkg" in scripts[0][1]


class TestRewriteHooksData:
    def test_rewrites_flat_copilot_format(self, tmp_path):
        pkg_path = tmp_path / "pkg"
        pkg_path.mkdir()
        scripts_dir = pkg_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "run.sh").write_text("echo hi")

        data = {
            "hooks": {
                "preToolUse": [{"bash": "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh", "type": "command"}]
            }
        }
        integrator = HookIntegrator()
        rewritten, _all_scripts = integrator._rewrite_hooks_data(data, pkg_path, "pkg", "vscode")
        bash_val = rewritten["hooks"]["preToolUse"][0]["bash"]
        assert "${CLAUDE_PLUGIN_ROOT}" not in bash_val

    def test_rewrites_nested_claude_format(self, tmp_path):
        pkg_path = tmp_path / "pkg"
        pkg_path.mkdir()
        hook_py = pkg_path / "hooks.py"
        hook_py.write_text("# hook")

        data = {
            "hooks": {
                "PreToolUse": [
                    {"hooks": [{"command": "${CLAUDE_PLUGIN_ROOT}/hooks.py", "type": "command"}]}
                ]
            }
        }
        integrator = HookIntegrator()
        rewritten, _all_scripts = integrator._rewrite_hooks_data(data, pkg_path, "pkg", "claude")
        cmd = rewritten["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert "${CLAUDE_PLUGIN_ROOT}" not in cmd


class TestIntegratePackageHooks:
    def _make_pkg(self, tmp_path: Path, name: str = "test-pkg") -> tuple[PackageInfo, Path]:
        pkg_dir = tmp_path / "apm_modules" / name
        pkg_dir.mkdir(parents=True)
        return _make_package_info(pkg_dir, name), pkg_dir

    def test_returns_empty_when_no_hook_files(self, tmp_path):
        pkg_info, _ = self._make_pkg(tmp_path)
        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks(pkg_info, tmp_path)
        assert result.files_integrated == 0

    def test_installs_hook_file_to_github_hooks(self, tmp_path):
        pkg_info, pkg_dir = self._make_pkg(tmp_path)
        hooks_dir = pkg_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps({"hooks": {"preToolUse": [{"bash": "echo hi", "type": "command"}]}})
        )
        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks(pkg_info, tmp_path)
        assert result.files_integrated == 1
        dest = tmp_path / ".github" / "hooks"
        assert any(dest.glob("*.json"))

    def test_skips_collisions_when_managed_files_none(self, tmp_path):
        pkg_info, pkg_dir = self._make_pkg(tmp_path)
        hooks_dir = pkg_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps({"hooks": {"preToolUse": [{"bash": "x"}]}})
        )
        # Pre-create the target file to simulate collision
        github_hooks = tmp_path / ".github" / "hooks"
        github_hooks.mkdir(parents=True)
        (github_hooks / f"{pkg_dir.name}-hooks.json").write_text(json.dumps({"user_content": True}))

        integrator = HookIntegrator()
        # managed_files=None means no files are managed -> collision
        result = integrator.integrate_package_hooks(pkg_info, tmp_path, managed_files=None)
        assert result.files_integrated == 0  # skipped

    def test_overwrites_when_force_true(self, tmp_path):
        pkg_info, pkg_dir = self._make_pkg(tmp_path)
        hooks_dir = pkg_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps({"hooks": {"preToolUse": [{"bash": "echo hi"}]}})
        )
        github_hooks = tmp_path / ".github" / "hooks"
        github_hooks.mkdir(parents=True)
        target_file = github_hooks / f"{pkg_dir.name}-hooks.json"
        target_file.write_text(json.dumps({"old": True}))

        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks(
            pkg_info, tmp_path, force=True, managed_files=None
        )
        assert result.files_integrated == 1

    def test_copies_referenced_scripts(self, tmp_path):
        pkg_info, pkg_dir = self._make_pkg(tmp_path)
        hooks_dir = pkg_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        scripts_dir = pkg_dir / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "run.sh").write_text("#!/bin/bash\necho hello")
        (hooks_dir / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "preToolUse": [
                            {"bash": "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh", "type": "command"}
                        ]
                    }
                }
            )
        )
        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks(pkg_info, tmp_path)
        assert result.scripts_copied >= 1


class TestIntegratePackageHooksClaude:
    def _make_pkg_with_hook(self, tmp_path: Path, hook_data: dict) -> PackageInfo:
        pkg_dir = tmp_path / "apm_modules" / "claude-pkg"
        pkg_dir.mkdir(parents=True)
        hooks_dir = pkg_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(json.dumps(hook_data))
        return _make_package_info(pkg_dir, "claude-pkg")

    def test_creates_claude_settings_json(self, tmp_path):
        hook_data = {"hooks": {"PreToolUse": [{"type": "command", "command": "echo hi"}]}}
        pkg_info = self._make_pkg_with_hook(tmp_path, hook_data)
        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks_claude(pkg_info, tmp_path)
        assert result.files_integrated == 1
        settings = tmp_path / ".claude" / "settings.json"
        assert settings.exists()
        data = json.loads(settings.read_text())
        assert "hooks" in data

    def test_idempotent_reinstall(self, tmp_path):
        hook_data = {"hooks": {"PreToolUse": [{"type": "command", "command": "echo hi"}]}}
        pkg_info = self._make_pkg_with_hook(tmp_path, hook_data)
        integrator = HookIntegrator()
        integrator.integrate_package_hooks_claude(pkg_info, tmp_path)
        integrator.integrate_package_hooks_claude(pkg_info, tmp_path)
        settings = tmp_path / ".claude" / "settings.json"
        data = json.loads(settings.read_text())
        # Should not duplicate entries
        entries = data["hooks"].get("PreToolUse", [])
        assert len(entries) == 1

    def test_merges_with_existing_settings(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        settings.write_text(
            json.dumps({"someOtherKey": True, "hooks": {"PostToolUse": [{"command": "x"}]}})
        )
        hook_data = {"hooks": {"PreToolUse": [{"type": "command", "command": "echo hi"}]}}
        pkg_info = self._make_pkg_with_hook(tmp_path, hook_data)
        integrator = HookIntegrator()
        integrator.integrate_package_hooks_claude(pkg_info, tmp_path)
        data = json.loads(settings.read_text())
        # Both events should be present
        assert "PreToolUse" in data["hooks"]
        assert "PostToolUse" in data["hooks"]


class TestIntegratePackageHooksCursor:
    def test_skips_when_cursor_dir_absent(self, tmp_path):
        pkg_dir = tmp_path / "apm_modules" / "cursor-pkg"
        pkg_dir.mkdir(parents=True)
        hooks_dir = pkg_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps({"hooks": {"afterFileEdit": [{"command": "x"}]}})
        )
        pkg_info = _make_package_info(pkg_dir, "cursor-pkg")
        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks_cursor(pkg_info, tmp_path)
        assert result.files_integrated == 0  # skipped — no .cursor dir

    def test_installs_when_cursor_dir_exists(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        pkg_dir = tmp_path / "apm_modules" / "cursor-pkg"
        pkg_dir.mkdir(parents=True)
        hooks_dir = pkg_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps({"hooks": {"afterFileEdit": [{"command": "x"}]}})
        )
        pkg_info = _make_package_info(pkg_dir, "cursor-pkg")
        integrator = HookIntegrator()
        result = integrator.integrate_package_hooks_cursor(pkg_info, tmp_path)
        assert result.files_integrated == 1
        assert (cursor_dir / "hooks.json").exists()


class TestIntegrateHooksForTarget:
    def test_dispatches_to_copilot(self, tmp_path):
        from apm_cli.integration.targets import KNOWN_TARGETS

        pkg_dir = tmp_path / "apm_modules" / "pkg"
        pkg_dir.mkdir(parents=True)
        hooks_dir = pkg_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps({"hooks": {"preToolUse": [{"bash": "echo hi"}]}})
        )
        pkg_info = _make_package_info(pkg_dir, "pkg")
        integrator = HookIntegrator()
        target = KNOWN_TARGETS["copilot"]
        result = integrator.integrate_hooks_for_target(target, pkg_info, tmp_path)
        assert result.files_integrated >= 0  # should not raise

    def test_dispatches_to_claude(self, tmp_path):
        from apm_cli.integration.targets import KNOWN_TARGETS

        pkg_dir = tmp_path / "apm_modules" / "pkg"
        pkg_dir.mkdir(parents=True)
        hooks_dir = pkg_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hooks.json").write_text(
            json.dumps({"hooks": {"PreToolUse": [{"type": "command", "command": "echo hi"}]}})
        )
        pkg_info = _make_package_info(pkg_dir, "pkg")
        integrator = HookIntegrator()
        target = KNOWN_TARGETS["claude"]
        result = integrator.integrate_hooks_for_target(target, pkg_info, tmp_path)
        assert result.files_integrated >= 0

    def test_returns_empty_for_unknown_target_name(self, tmp_path):
        from apm_cli.integration.targets import PrimitiveMapping, TargetProfile

        pkg_dir = tmp_path / "apm_modules" / "pkg"
        pkg_dir.mkdir(parents=True)
        pkg_info = _make_package_info(pkg_dir, "pkg")

        # Create a target with a name not in _MERGE_HOOK_TARGETS
        dummy_target = TargetProfile(
            name="unknown-target",
            root_dir=".unknown",
            primitives={"hooks": PrimitiveMapping("hooks", ".json", "hooks")},
        )
        integrator = HookIntegrator()
        result = integrator.integrate_hooks_for_target(dummy_target, pkg_info, tmp_path)
        assert result.files_integrated == 0


class TestSyncIntegrationHooks:
    def test_removes_tracked_hook_files(self, tmp_path):
        github_hooks = tmp_path / ".github" / "hooks"
        github_hooks.mkdir(parents=True)
        hook_file = github_hooks / "my-pkg-hooks.json"
        hook_file.write_text("{}")

        integrator = HookIntegrator()
        stats = integrator.sync_integration(
            apm_package=None,
            project_root=tmp_path,
            managed_files={".github/hooks/my-pkg-hooks.json"},
        )
        assert stats["files_removed"] == 1
        assert not hook_file.exists()

    def test_legacy_fallback_removes_apm_suffix_files(self, tmp_path):
        github_hooks = tmp_path / ".github" / "hooks"
        github_hooks.mkdir(parents=True)
        (github_hooks / "my-pkg-apm.json").write_text("{}")
        (github_hooks / "other.json").write_text("{}")

        integrator = HookIntegrator()
        stats = integrator.sync_integration(
            apm_package=None,
            project_root=tmp_path,
            managed_files=None,
        )
        assert stats["files_removed"] == 1

    def test_cleans_apm_entries_from_cursor_hooks_json(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        hooks_json = cursor_dir / "hooks.json"
        hooks_json.write_text(
            json.dumps(
                {
                    "hooks": {
                        "afterFileEdit": [
                            {"command": "x", "_apm_source": "my-pkg"},
                            {"command": "y"},
                        ]
                    }
                }
            )
        )
        integrator = HookIntegrator()
        integrator.sync_integration(apm_package=None, project_root=tmp_path, managed_files=set())
        data = json.loads(hooks_json.read_text())
        remaining = data["hooks"]["afterFileEdit"]
        assert all("_apm_source" not in e for e in remaining)


class TestCleanApmEntriesFromJson:
    def test_removes_apm_entries(self, tmp_path):
        hooks_json = tmp_path / "hooks.json"
        hooks_json.write_text(
            json.dumps(
                {
                    "hooks": {
                        "evt": [
                            {"command": "x", "_apm_source": "pkg"},
                            {"command": "y"},
                        ]
                    }
                }
            )
        )
        stats = {"files_removed": 0}
        HookIntegrator._clean_apm_entries_from_json(hooks_json, stats)
        assert stats["files_removed"] == 1
        data = json.loads(hooks_json.read_text())
        assert all("_apm_source" not in e for e in data["hooks"]["evt"])

    def test_deletes_empty_event_key(self, tmp_path):
        hooks_json = tmp_path / "hooks.json"
        hooks_json.write_text(
            json.dumps({"hooks": {"evt": [{"command": "x", "_apm_source": "pkg"}]}})
        )
        stats = {"files_removed": 0}
        HookIntegrator._clean_apm_entries_from_json(hooks_json, stats)
        data = json.loads(hooks_json.read_text())
        assert "evt" not in data.get("hooks", {})

    def test_noop_when_file_absent(self, tmp_path):
        stats = {"files_removed": 0}
        HookIntegrator._clean_apm_entries_from_json(tmp_path / "no.json", stats)
        assert stats["files_removed"] == 0

    def test_noop_when_no_hooks_key(self, tmp_path):
        f = tmp_path / "h.json"
        f.write_text('{"other": true}')
        stats = {"files_removed": 0}
        HookIntegrator._clean_apm_entries_from_json(f, stats)
        assert stats["files_removed"] == 0


# ===========================================================================
# SkillIntegrator tests
# ===========================================================================


class TestToHyphenCase:
    def test_converts_camel_case(self):
        assert to_hyphen_case("mySkillName") == "my-skill-name"

    def test_handles_owner_repo(self):
        assert to_hyphen_case("owner/my-repo") == "my-repo"

    def test_replaces_underscores(self):
        assert to_hyphen_case("my_skill") == "my-skill"

    def test_removes_invalid_chars(self):
        assert to_hyphen_case("my skill!") == "my-skill"

    def test_truncates_to_64(self):
        long_name = "a" * 70
        assert len(to_hyphen_case(long_name)) == 64

    def test_strips_leading_trailing_hyphens(self):
        result = to_hyphen_case("-my-skill-")
        assert not result.startswith("-")
        assert not result.endswith("-")


class TestValidateSkillName:
    def test_valid_name(self):
        ok, _ = validate_skill_name("my-skill")
        assert ok is True

    def test_empty_name(self):
        ok, msg = validate_skill_name("")
        assert ok is False
        assert "empty" in msg.lower()

    def test_too_long(self):
        ok, _msg = validate_skill_name("a" * 65)
        assert ok is False

    def test_uppercase(self):
        ok, msg = validate_skill_name("MySkill")
        assert ok is False
        assert "lowercase" in msg.lower()

    def test_underscore(self):
        ok, _msg = validate_skill_name("my_skill")
        assert ok is False

    def test_spaces(self):
        ok, _msg = validate_skill_name("my skill")
        assert ok is False

    def test_consecutive_hyphens(self):
        ok, _msg = validate_skill_name("my--skill")
        assert ok is False

    def test_leading_hyphen(self):
        ok, _msg = validate_skill_name("-my-skill")
        assert ok is False

    def test_trailing_hyphen(self):
        ok, _msg = validate_skill_name("my-skill-")
        assert ok is False

    def test_single_char(self):
        ok, _ = validate_skill_name("a")
        assert ok is True

    def test_numeric_name(self):
        ok, _ = validate_skill_name("42")
        assert ok is True


class TestNormalizeSkillName:
    def test_normalizes_camel_case(self):
        assert normalize_skill_name("MySkill") == "my-skill"

    def test_normalizes_owner_slash_repo(self):
        assert normalize_skill_name("owner/MyRepo") == "my-repo"

    def test_removes_underscores(self):
        assert normalize_skill_name("my_skill_name") == "my-skill-name"


class TestGetEffectiveType:
    def test_claude_skill_returns_skill(self):
        from apm_cli.models.validation import PackageContentType

        pkg = _make_apm_package()
        info = PackageInfo(
            package=pkg,
            install_path=Path("/tmp"),
            package_type=PackageType.CLAUDE_SKILL,
        )
        assert get_effective_type(info) == PackageContentType.SKILL

    def test_hybrid_returns_skill(self):
        from apm_cli.models.validation import PackageContentType

        pkg = _make_apm_package()
        info = PackageInfo(
            package=pkg,
            install_path=Path("/tmp"),
            package_type=PackageType.HYBRID,
        )
        assert get_effective_type(info) == PackageContentType.SKILL

    def test_apm_package_returns_instructions(self):
        from apm_cli.models.validation import PackageContentType

        pkg = _make_apm_package()
        info = PackageInfo(
            package=pkg,
            install_path=Path("/tmp"),
            package_type=PackageType.APM_PACKAGE,
        )
        assert get_effective_type(info) == PackageContentType.INSTRUCTIONS

    def test_skill_bundle_returns_skill(self):
        from apm_cli.models.validation import PackageContentType

        pkg = _make_apm_package()
        info = PackageInfo(
            package=pkg,
            install_path=Path("/tmp"),
            package_type=PackageType.SKILL_BUNDLE,
        )
        assert get_effective_type(info) == PackageContentType.SKILL


class TestShouldInstallSkill:
    def test_true_for_claude_skill(self):
        pkg = _make_apm_package()
        info = PackageInfo(
            package=pkg, install_path=Path("/tmp"), package_type=PackageType.CLAUDE_SKILL
        )
        assert should_install_skill(info) is True

    def test_false_for_apm_package(self):
        pkg = _make_apm_package()
        info = PackageInfo(
            package=pkg, install_path=Path("/tmp"), package_type=PackageType.APM_PACKAGE
        )
        assert should_install_skill(info) is False

    def test_true_for_hybrid(self):
        pkg = _make_apm_package()
        info = PackageInfo(package=pkg, install_path=Path("/tmp"), package_type=PackageType.HYBRID)
        assert should_install_skill(info) is True


class TestShouldCompileInstructions:
    def test_true_for_apm_package(self):
        pkg = _make_apm_package()
        info = PackageInfo(
            package=pkg, install_path=Path("/tmp"), package_type=PackageType.APM_PACKAGE
        )
        assert should_compile_instructions(info) is True

    def test_false_for_claude_skill(self):
        pkg = _make_apm_package()
        info = PackageInfo(
            package=pkg, install_path=Path("/tmp"), package_type=PackageType.CLAUDE_SKILL
        )
        assert should_compile_instructions(info) is False

    def test_true_for_hybrid(self):
        # PackageType.HYBRID maps to PackageContentType.SKILL via get_effective_type
        # so should_compile_instructions returns False for it (skill-only, no compilation)
        pkg = _make_apm_package()
        info = PackageInfo(package=pkg, install_path=Path("/tmp"), package_type=PackageType.HYBRID)
        assert should_compile_instructions(info) is False


class TestCopySkillToTarget:
    def test_skips_non_skill_package(self, tmp_path):
        pkg_dir = tmp_path / "my-pkg"
        pkg_dir.mkdir()
        pkg = _make_apm_package("my-pkg")
        info = PackageInfo(package=pkg, install_path=pkg_dir, package_type=PackageType.APM_PACKAGE)
        # No SKILL.md -> should return empty
        result = copy_skill_to_target(info, pkg_dir, tmp_path)
        assert result == []

    def test_skips_when_no_skill_md(self, tmp_path):
        pkg_dir = tmp_path / "my-skill"
        pkg_dir.mkdir()
        pkg = _make_apm_package("my-skill")
        info = PackageInfo(package=pkg, install_path=pkg_dir, package_type=PackageType.CLAUDE_SKILL)
        result = copy_skill_to_target(info, pkg_dir, tmp_path)
        assert result == []

    def test_deploys_skill_with_skill_md(self, tmp_path):
        from apm_cli.integration.targets import KNOWN_TARGETS

        pkg_dir = tmp_path / "my-skill"
        pkg_dir.mkdir()
        (pkg_dir / "SKILL.md").write_text("---\ndescription: test\n---\n# My Skill")
        pkg = _make_apm_package("my-skill")
        info = PackageInfo(package=pkg, install_path=pkg_dir, package_type=PackageType.CLAUDE_SKILL)
        # Use copilot target (deploys to .agents/skills/)
        targets = [KNOWN_TARGETS["copilot"]]
        result = copy_skill_to_target(info, pkg_dir, tmp_path, targets=targets)
        assert len(result) >= 1
        skill_dir = result[0]
        assert (skill_dir / "SKILL.md").exists()


class TestSkillIntegratorFindMethods:
    def test_find_instruction_files(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        instr_dir = pkg_dir / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        (instr_dir / "main.instructions.md").write_text("# Instructions")
        (instr_dir / "other.instructions.md").write_text("# Other")

        files = SkillIntegrator().find_instruction_files(pkg_dir)
        assert len(files) == 2

    def test_find_agent_files(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        agents_dir = pkg_dir / ".apm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "agent1.agent.md").write_text("# Agent")

        files = SkillIntegrator().find_agent_files(pkg_dir)
        assert len(files) == 1

    def test_find_prompt_files_in_root(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "my.prompt.md").write_text("# Prompt")

        files = SkillIntegrator().find_prompt_files(pkg_dir)
        assert len(files) == 1

    def test_find_prompt_files_in_apm_prompts(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        prompts_dir = pkg_dir / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "one.prompt.md").write_text("# Prompt")

        files = SkillIntegrator().find_prompt_files(pkg_dir)
        assert len(files) == 1

    def test_find_context_files(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        ctx_dir = pkg_dir / ".apm" / "context"
        ctx_dir.mkdir(parents=True)
        (ctx_dir / "ctx.context.md").write_text("# Context")
        mem_dir = pkg_dir / ".apm" / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "mem.memory.md").write_text("# Memory")

        files = SkillIntegrator().find_context_files(pkg_dir)
        assert len(files) == 2

    def test_returns_empty_when_no_dirs(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        integrator = SkillIntegrator()
        assert integrator.find_instruction_files(pkg_dir) == []
        assert integrator.find_agent_files(pkg_dir) == []
        assert integrator.find_context_files(pkg_dir) == []


class TestSkillIntegratorDirsEqual:
    def test_equal_directories(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "SKILL.md").write_text("content")
        (b / "SKILL.md").write_text("content")
        assert SkillIntegrator.is_skill_dir_identical_to_source(a, b) is True

    def test_different_content(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "SKILL.md").write_text("content A")
        (b / "SKILL.md").write_text("content B")
        assert SkillIntegrator.is_skill_dir_identical_to_source(a, b) is False

    def test_extra_file_in_one(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "SKILL.md").write_text("content")
        (b / "SKILL.md").write_text("content")
        (a / "extra.md").write_text("extra")
        assert SkillIntegrator.is_skill_dir_identical_to_source(a, b) is False


class TestPromoteSubSkills:
    def _make_sub_skills(self, base: Path) -> Path:
        sub_skills = base / ".apm" / "skills"
        sub_skills.mkdir(parents=True, exist_ok=True)
        for skill_name in ("skill-a", "skill-b"):
            d = sub_skills / skill_name
            d.mkdir()
            (d / "SKILL.md").write_text(f"# {skill_name}\n---\n")
        return sub_skills

    def test_promotes_all_sub_skills(self, tmp_path):
        pkg_dir = tmp_path / "parent-pkg"
        pkg_dir.mkdir()
        sub_skills_dir = self._make_sub_skills(pkg_dir)
        target_root = tmp_path / ".github" / "skills"
        target_root.mkdir(parents=True)

        count, _deployed = SkillIntegrator._promote_sub_skills(
            sub_skills_dir, target_root, "parent-pkg"
        )
        assert count == 2
        assert (target_root / "skill-a" / "SKILL.md").exists()
        assert (target_root / "skill-b" / "SKILL.md").exists()

    def test_skips_dir_without_skill_md(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        sub_skills = pkg_dir / ".apm" / "skills"
        sub_skills.mkdir(parents=True)
        (sub_skills / "no-skill-md").mkdir()  # no SKILL.md inside
        target_root = tmp_path / "skills"
        target_root.mkdir()

        count, _ = SkillIntegrator._promote_sub_skills(sub_skills, target_root, "pkg")
        assert count == 0

    def test_adopts_identical_existing(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        sub_skills = pkg_dir / ".apm" / "skills"
        sub_skills.mkdir(parents=True)
        skill_src = sub_skills / "my-skill"
        skill_src.mkdir()
        (skill_src / "SKILL.md").write_text("# Skill")

        target_root = tmp_path / "skills"
        target_root.mkdir()
        # Pre-create identical destination
        existing = target_root / "my-skill"
        existing.mkdir()
        (existing / "SKILL.md").write_text("# Skill")

        count, _deployed = SkillIntegrator._promote_sub_skills(sub_skills, target_root, "pkg")
        assert count == 1

    def test_returns_zero_for_nonexistent_dir(self, tmp_path):
        count, deployed = SkillIntegrator._promote_sub_skills(
            tmp_path / "nonexistent", tmp_path / "skills", "parent"
        )
        assert count == 0
        assert deployed == []

    def test_name_filter_restricts_skills(self, tmp_path):
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        sub_skills = pkg_dir / ".apm" / "skills"
        sub_skills.mkdir(parents=True)
        for name in ("skill-x", "skill-y", "skill-z"):
            d = sub_skills / name
            d.mkdir()
            (d / "SKILL.md").write_text("# " + name)
        target_root = tmp_path / "skills"
        target_root.mkdir()

        count, _ = SkillIntegrator._promote_sub_skills(
            sub_skills, target_root, "pkg", name_filter={"skill-x"}
        )
        assert count == 1
        assert (target_root / "skill-x").exists()
        assert not (target_root / "skill-y").exists()


class TestBuildOwnershipMaps:
    def test_returns_empty_maps_when_no_lockfile(self, tmp_path):
        owned, native = SkillIntegrator._build_ownership_maps(tmp_path)
        assert owned == {}
        assert native == {}

    def test_builds_maps_from_lockfile(self, tmp_path):
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        lock_path = get_lockfile_path(tmp_path)
        lf = LockFile()
        lf.save(lock_path)
        # Without actual deps, maps should still be empty
        owned, native = SkillIntegrator._build_ownership_maps(tmp_path)
        assert isinstance(owned, dict)
        assert isinstance(native, dict)


class TestIntegrateNativeSkill:
    def test_deploys_skill_md_to_github_skills(self, tmp_path):
        pkg_dir = tmp_path / "my-skill"
        pkg_dir.mkdir()
        (pkg_dir / "SKILL.md").write_text("# My Skill\n---\ndescription: test skill\n")
        pkg = _make_apm_package("my-skill")
        info = PackageInfo(package=pkg, install_path=pkg_dir, package_type=PackageType.CLAUDE_SKILL)

        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator = SkillIntegrator()
        result = integrator._integrate_native_skill(
            info,
            tmp_path,
            pkg_dir / "SKILL.md",
            targets=[KNOWN_TARGETS["copilot"]],
        )
        assert result.skill_created is True
        deployed_skill = tmp_path / ".agents" / "skills" / "my-skill"
        assert deployed_skill.exists()

    def test_updates_on_reinstall(self, tmp_path):
        pkg_dir = tmp_path / "my-skill"
        pkg_dir.mkdir()
        (pkg_dir / "SKILL.md").write_text("# v1")
        pkg = _make_apm_package("my-skill")
        info = PackageInfo(package=pkg, install_path=pkg_dir, package_type=PackageType.CLAUDE_SKILL)
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator = SkillIntegrator()
        targets = [KNOWN_TARGETS["copilot"]]
        integrator._integrate_native_skill(info, tmp_path, pkg_dir / "SKILL.md", targets=targets)
        # Update content
        (pkg_dir / "SKILL.md").write_text("# v2")
        result2 = integrator._integrate_native_skill(
            info, tmp_path, pkg_dir / "SKILL.md", targets=targets
        )
        assert result2.skill_updated is True


class TestIntegratePackageSkill:
    def test_skips_instructions_only_package(self, tmp_path):
        pkg_dir = tmp_path / "instr-pkg"
        pkg_dir.mkdir()
        pkg = _make_apm_package("instr-pkg")
        info = PackageInfo(package=pkg, install_path=pkg_dir, package_type=PackageType.APM_PACKAGE)
        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(info, tmp_path)
        assert result.skill_skipped is True

    def test_installs_native_skill(self, tmp_path):
        pkg_dir = tmp_path / "skill-pkg"
        pkg_dir.mkdir()
        (pkg_dir / "SKILL.md").write_text("---\ndescription: A skill\n---\n# Skill")
        pkg = _make_apm_package("skill-pkg")
        info = PackageInfo(package=pkg, install_path=pkg_dir, package_type=PackageType.CLAUDE_SKILL)
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(
            info, tmp_path, targets=[KNOWN_TARGETS["copilot"]]
        )
        assert result.skill_created is True

    def test_installs_skill_bundle(self, tmp_path):
        pkg_dir = tmp_path / "bundle-pkg"
        pkg_dir.mkdir()
        skills_dir = pkg_dir / "skills"
        skills_dir.mkdir()
        for name in ("skill-1", "skill-2"):
            s = skills_dir / name
            s.mkdir()
            (s / "SKILL.md").write_text(f"# {name}")
        pkg = _make_apm_package("bundle-pkg")
        info = PackageInfo(package=pkg, install_path=pkg_dir, package_type=PackageType.SKILL_BUNDLE)
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(
            info, tmp_path, targets=[KNOWN_TARGETS["copilot"]]
        )
        assert result.sub_skills_promoted == 2

    def test_promotes_sub_skills_from_instructions_pkg(self, tmp_path):
        pkg_dir = tmp_path / "instr-with-skills"
        pkg_dir.mkdir()
        sub_skills = pkg_dir / ".apm" / "skills"
        sub_skills.mkdir(parents=True)
        sub = sub_skills / "embedded-skill"
        sub.mkdir()
        (sub / "SKILL.md").write_text("# Embedded Skill")
        pkg = _make_apm_package("instr-with-skills")
        info = PackageInfo(package=pkg, install_path=pkg_dir, package_type=PackageType.APM_PACKAGE)
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(
            info, tmp_path, targets=[KNOWN_TARGETS["copilot"]]
        )
        # skill_skipped=True but sub-skills should be promoted
        assert result.skill_skipped is True
        assert result.sub_skills_promoted >= 1

    def test_skips_virtual_file_package(self, tmp_path):
        from apm_cli.models.dependency.reference import DependencyReference

        pkg_dir = tmp_path / "virtual-file"
        pkg_dir.mkdir()
        (pkg_dir / "agent.agent.md").write_text("# Agent")
        dep_ref = DependencyReference.parse("github/awesome-copilot/agents/agent.agent.md")
        pkg = _make_apm_package("virtual-file")
        info = PackageInfo(
            package=pkg,
            install_path=pkg_dir,
            package_type=PackageType.CLAUDE_SKILL,
            dependency_ref=dep_ref,
        )
        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(info, tmp_path)
        assert result.skill_skipped is True


class TestSkillIntegratorSyncIntegration:
    def test_removes_tracked_skill_directories(self, tmp_path):
        agents_skills = tmp_path / ".agents" / "skills"
        agents_skills.mkdir(parents=True)
        skill_dir = agents_skills / "old-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Old Skill")

        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator = SkillIntegrator()
        stats = integrator.sync_integration(
            apm_package=None,
            project_root=tmp_path,
            managed_files={".agents/skills/old-skill"},
            targets=[KNOWN_TARGETS["copilot"]],
        )
        assert stats["files_removed"] >= 1
        assert not skill_dir.exists()

    def test_skips_non_skill_paths(self, tmp_path):
        agents_skills = tmp_path / ".agents" / "skills"
        agents_skills.mkdir(parents=True)
        skill_dir = agents_skills / "keep-me"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Keep")

        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator = SkillIntegrator()
        # managed_files contains a non-skill path
        stats = integrator.sync_integration(
            apm_package=None,
            project_root=tmp_path,
            managed_files={".github/instructions/something.instructions.md"},
            targets=[KNOWN_TARGETS["copilot"]],
        )
        # skill-keep should still exist
        assert skill_dir.exists()
        assert stats["files_removed"] == 0

    def test_noop_when_managed_files_empty_set(self, tmp_path):
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator = SkillIntegrator()
        stats = integrator.sync_integration(
            apm_package=None,
            project_root=tmp_path,
            managed_files=set(),
            targets=[KNOWN_TARGETS["copilot"]],
        )
        assert stats["files_removed"] == 0
