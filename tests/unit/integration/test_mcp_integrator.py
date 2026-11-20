"""Unit tests for MCPIntegrator.

Tests focus on pure-logic methods, ensuring the orchestration helpers
(deduplication, server-info building, drift detection, runtime detection,
lockfile update, and stale cleanup) behave correctly without requiring
live network calls or installed runtimes.
"""

import pytest

from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.dependency.mcp import MCPDependency

pytestmark = pytest.mark.unit

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


# ===========================================================================
# MCPIntegrator.deduplicate
# ===========================================================================


class TestDeduplicate:
    @pytest.mark.parametrize("names", [[], ["a", "b", "c"]], ids=["empty", "distinct"])
    def test_no_duplicates(self, names):
        result = MCPIntegrator.deduplicate([_make_dep(name) for name in names])
        assert [d.name for d in result] == names

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
    @pytest.mark.parametrize("names", [[], ["alpha", "beta"]], ids=["empty", "dep-objects"])
    def test_dep_objects(self, names):
        deps = [_make_dep(name) for name in names]
        assert MCPIntegrator.get_server_names(deps) == set(names)

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

    def test_resolved_by_not_leaked_into_config(self):
        # Provenance is transient install-time metadata; it must never appear
        # in the serialized config (else it pollutes drift comparisons) (#2081).
        dep = _make_dep("svc", transport="stdio")
        dep.resolved_by = "@qado/agent-config"
        configs = MCPIntegrator.get_server_configs([dep])
        assert "resolved_by" not in configs["svc"]


# ===========================================================================
# MCPIntegrator.get_server_provenance
# ===========================================================================


class TestGetServerProvenance:
    def test_empty(self):
        assert MCPIntegrator.get_server_provenance([]) == {}

    def test_direct_dep_has_no_provenance(self):
        # resolved_by defaults to None for root-declared servers -> omitted.
        assert MCPIntegrator.get_server_provenance([_make_dep("svc")]) == {}

    def test_transitive_dep_recorded(self):
        dep = _make_dep("shadcn")
        dep.resolved_by = "@qado/agent-config"
        assert MCPIntegrator.get_server_provenance([dep]) == {"shadcn": "@qado/agent-config"}

    def test_root_wins_over_transitive_after_dedup(self):
        # deduplicate() lists the root (resolved_by=None) entry first and drops
        # the transitive duplicate, so the surviving 'svc' is treated as direct.
        root = _make_dep("svc")
        transitive = _make_dep("svc")
        transitive.resolved_by = "@qado/agent-config"
        deduped = MCPIntegrator.deduplicate([root, transitive])
        assert MCPIntegrator.get_server_provenance(deduped) == {}

    def test_string_deps_ignored(self):
        assert MCPIntegrator.get_server_provenance(["plain-server"]) == {}


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

    @pytest.mark.parametrize(
        "scripts", [{}, {"run": "python main.py"}], ids=["empty", "no-runtime-words"]
    )
    def test_no_false_positives(self, scripts):
        assert MCPIntegrator._detect_runtimes(scripts) == []


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
        dep = _make_self_defined("remote-svc", transport="http", url="https://example.com/mcp")
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "remotes" in info
        assert info["remotes"][0]["url"] == "https://example.com/mcp"
        assert info["remotes"][0]["transport"] == "streamable-http"
        assert "packages" not in info

    def test_sse_transport_builds_remote(self):
        dep = _make_self_defined("sse-svc", transport="sse", url="https://example.com/sse")
        info = MCPIntegrator._build_self_defined_info(dep)
        assert "remotes" in info

    def test_streamable_http_transport_builds_remote(self):
        dep = _make_self_defined(
            "stream-svc", transport="streamable-http", url="https://example.com/mcp"
        )
        info = MCPIntegrator._build_self_defined_info(dep)
        assert info["remotes"][0]["transport"] == "streamable-http"
        assert info["remotes"][0]["url"] == "https://example.com/mcp"
        assert "packages" not in info

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
                "packages": [
                    {"runtime_hint": name, "runtime_arguments": [], "registry_name": "npm"}
                ],
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
        assert cache == {}

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

    def test_registry_str_overlay_no_longer_emits_warning(self):
        """registry: <url> is now honoured at install time, so no warning is emitted."""
        cache = {"svc": {"name": "svc"}}
        dep = _make_dep("svc", registry="https://custom.registry.example.com")
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning would raise
            MCPIntegrator._apply_overlay(cache, dep)  # must not raise


# ===========================================================================
# MCPIntegrator.update_lockfile
# ===========================================================================


# ===========================================================================
# MCPIntegrator.remove_stale - vscode
# ===========================================================================


# ===========================================================================
# MCPIntegrator.remove_stale - copilot
# ===========================================================================


# ===========================================================================
# MCPIntegrator.collect_transitive - edge cases
# ===========================================================================


# ===========================================================================
# _gate_project_scoped_runtimes -- issue #1335
# ===========================================================================


class TestExcludeReadsLikeTarget:
    """``--exclude`` folds case, honours the vscode alias and never ignores silently (C4)."""

    def test_case_and_alias_select_copilot(self, capsys) -> None:
        from apm_cli.integration.mcp_user_scope_runtimes import filter_excluded_mcp_runtimes

        for value in ("COPILOT", "VSCode", "vscode"):
            assert "copilot" not in filter_excluded_mcp_runtimes(["copilot", "claude"], value)

    def test_unknown_value_warns(self, capsys, monkeypatch) -> None:
        from apm_cli.integration import mcp_user_scope_runtimes
        from apm_cli.integration.mcp_user_scope_runtimes import filter_excluded_mcp_runtimes

        monkeypatch.setattr(mcp_user_scope_runtimes, "_unknown_exclusions_warned", set())
        assert filter_excluded_mcp_runtimes(["claude"], "bogus") == ["claude"]
        captured = capsys.readouterr()
        assert "names no target or MCP runtime" in (captured.out + captured.err).replace("\n", " ")
        # Frozen install resolves runtimes twice; the warning stays single.
        assert filter_excluded_mcp_runtimes(["claude"], "bogus") == ["claude"]
        captured = capsys.readouterr()
        assert "names no target" not in captured.out + captured.err
