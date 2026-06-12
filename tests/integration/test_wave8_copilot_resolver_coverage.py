"""Wave 8 integration tests -- Copilot adapter helpers + marketplace resolver.

Targets pure-function helpers in:
- apm_cli.adapters.client.copilot (_translate_env_placeholder, _extract_legacy_angle_vars,
  _has_env_placeholder, _stringify_env_literal, CopilotClientAdapter config paths)
- apm_cli.marketplace.resolver (_normalize_owner_repo_slug, _marketplace_project_slug,
  _normalize_repo_field_for_match, _repo_field_matches_marketplace,
  _coerce_dict_plugin_type, MarketplacePluginResolution, CrossRepoMisconfigRisk)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Copilot adapter helper tests
# ---------------------------------------------------------------------------


class TestTranslateEnvPlaceholder:
    """_translate_env_placeholder -- pure textual translation."""

    def test_legacy_angle_bracket(self) -> None:
        from apm_cli.adapters.client.copilot import _translate_env_placeholder

        assert _translate_env_placeholder("<MY_TOKEN>") == "${MY_TOKEN}"

    def test_brace_passthrough(self) -> None:
        from apm_cli.adapters.client.copilot import _translate_env_placeholder

        assert _translate_env_placeholder("${MY_TOKEN}") == "${MY_TOKEN}"

    def test_env_prefix_strip(self) -> None:
        from apm_cli.adapters.client.copilot import _translate_env_placeholder

        assert _translate_env_placeholder("${env:MY_TOKEN}") == "${MY_TOKEN}"

    def test_non_string_passthrough(self) -> None:
        from apm_cli.adapters.client.copilot import _translate_env_placeholder

        assert _translate_env_placeholder(42) == 42
        assert _translate_env_placeholder(None) is None

    def test_no_placeholder(self) -> None:
        from apm_cli.adapters.client.copilot import _translate_env_placeholder

        assert _translate_env_placeholder("plain-text") == "plain-text"

    def test_mixed_placeholders(self) -> None:
        from apm_cli.adapters.client.copilot import _translate_env_placeholder

        result = _translate_env_placeholder("<A> and ${env:B} and ${C}")
        assert result == "${A} and ${B} and ${C}"

    def test_idempotent(self) -> None:
        from apm_cli.adapters.client.copilot import _translate_env_placeholder

        v = "<TOKEN>"
        first = _translate_env_placeholder(v)
        second = _translate_env_placeholder(first)
        assert first == second == "${TOKEN}"

    def test_empty_string(self) -> None:
        from apm_cli.adapters.client.copilot import _translate_env_placeholder

        assert _translate_env_placeholder("") == ""


class TestExtractLegacyAngleVars:
    """_extract_legacy_angle_vars -- set of legacy <VAR> names."""

    def test_single_var(self) -> None:
        from apm_cli.adapters.client.copilot import _extract_legacy_angle_vars

        assert _extract_legacy_angle_vars("<MY_KEY>") == {"MY_KEY"}

    def test_multiple_vars(self) -> None:
        from apm_cli.adapters.client.copilot import _extract_legacy_angle_vars

        result = _extract_legacy_angle_vars("use <A> then <B>")
        assert result == {"A", "B"}

    def test_no_vars(self) -> None:
        from apm_cli.adapters.client.copilot import _extract_legacy_angle_vars

        assert _extract_legacy_angle_vars("no placeholders") == set()

    def test_non_string(self) -> None:
        from apm_cli.adapters.client.copilot import _extract_legacy_angle_vars

        assert _extract_legacy_angle_vars(123) == set()
        assert _extract_legacy_angle_vars(None) == set()

    def test_brace_not_angle(self) -> None:
        from apm_cli.adapters.client.copilot import _extract_legacy_angle_vars

        assert _extract_legacy_angle_vars("${MY_KEY}") == set()


class TestHasEnvPlaceholder:
    """_has_env_placeholder -- bool check for any env-var syntax."""

    def test_brace_syntax(self) -> None:
        from apm_cli.adapters.client.copilot import _has_env_placeholder

        assert _has_env_placeholder("${VAR}") is True

    def test_env_prefix(self) -> None:
        from apm_cli.adapters.client.copilot import _has_env_placeholder

        assert _has_env_placeholder("${env:VAR}") is True

    def test_legacy_angle(self) -> None:
        from apm_cli.adapters.client.copilot import _has_env_placeholder

        assert _has_env_placeholder("<VAR>") is True

    def test_no_placeholder(self) -> None:
        from apm_cli.adapters.client.copilot import _has_env_placeholder

        assert _has_env_placeholder("plain") is False

    def test_non_string(self) -> None:
        from apm_cli.adapters.client.copilot import _has_env_placeholder

        assert _has_env_placeholder(42) is False


class TestStringifyEnvLiteral:
    """_stringify_env_literal -- MCP env literal to string."""

    def test_bool_true(self) -> None:
        from apm_cli.adapters.client.copilot import _stringify_env_literal

        assert _stringify_env_literal(True) == "true"

    def test_bool_false(self) -> None:
        from apm_cli.adapters.client.copilot import _stringify_env_literal

        assert _stringify_env_literal(False) == "false"

    def test_int(self) -> None:
        from apm_cli.adapters.client.copilot import _stringify_env_literal

        assert _stringify_env_literal(42) == "42"

    def test_string(self) -> None:
        from apm_cli.adapters.client.copilot import _stringify_env_literal

        assert _stringify_env_literal("hello") == "hello"


class TestCopilotConfigPaths:
    """CopilotClientAdapter config path resolution."""

    def test_get_config_path(self) -> None:
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter()
        path = adapter.get_config_path()
        assert path.endswith("mcp-config.json")
        assert ".copilot" in path

    def test_get_current_config_missing_file(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter()
        with patch.object(adapter, "get_config_path", return_value=str(tmp_path / "nope.json")):
            cfg = adapter.get_current_config()
        assert cfg == {}

    def test_get_current_config_valid_json(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        config_file = tmp_path / "mcp-config.json"
        config_file.write_text(json.dumps({"mcpServers": {"a": {}}}))
        adapter = CopilotClientAdapter()
        with patch.object(adapter, "get_config_path", return_value=str(config_file)):
            cfg = adapter.get_current_config()
        assert "mcpServers" in cfg

    def test_get_current_config_bad_json(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        config_file = tmp_path / "bad.json"
        config_file.write_text("not json")
        adapter = CopilotClientAdapter()
        with patch.object(adapter, "get_config_path", return_value=str(config_file)):
            cfg = adapter.get_current_config()
        assert cfg == {}

    def test_update_config_creates_dir(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        config_file = tmp_path / "sub" / "mcp-config.json"
        adapter = CopilotClientAdapter()
        with patch.object(adapter, "get_config_path", return_value=str(config_file)):
            adapter.update_config({"test-server": {"command": "echo"}})
        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert "test-server" in data["mcpServers"]

    def test_update_config_preserves_existing(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        config_file = tmp_path / "mcp-config.json"
        config_file.write_text(json.dumps({"mcpServers": {"old": {"command": "a"}}}))
        adapter = CopilotClientAdapter()
        with patch.object(adapter, "get_config_path", return_value=str(config_file)):
            adapter.update_config({"new": {"command": "b"}})
        data = json.loads(config_file.read_text())
        assert "old" in data["mcpServers"]
        assert "new" in data["mcpServers"]


class TestCopilotCollectPreviouslyBaked:
    """CopilotClientAdapter._collect_previously_baked_keys."""

    def test_no_existing_config(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter()
        with patch.object(adapter, "get_config_path", return_value=str(tmp_path / "nope.json")):
            keys, headers = adapter._collect_previously_baked_keys("org/server", None)
        assert keys == set()
        assert headers is False

    def test_baked_env_detected(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        config_file = tmp_path / "mcp-config.json"
        config_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "server": {
                            "env": {"MY_KEY": "literal-value"},
                            "headers": {"Authorization": "Bearer xyz"},
                        }
                    }
                }
            )
        )
        adapter = CopilotClientAdapter()
        with patch.object(adapter, "get_config_path", return_value=str(config_file)):
            keys, headers = adapter._collect_previously_baked_keys("org/server", None)
        assert "MY_KEY" in keys
        assert headers is True

    def test_placeholder_env_not_flagged(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        config_file = tmp_path / "mcp-config.json"
        config_file.write_text(
            json.dumps({"mcpServers": {"server": {"env": {"K": "${K}"}, "headers": {}}}})
        )
        adapter = CopilotClientAdapter()
        with patch.object(adapter, "get_config_path", return_value=str(config_file)):
            keys, headers = adapter._collect_previously_baked_keys("org/server", None)
        assert keys == set()
        assert headers is False

    def test_server_name_override(self, tmp_path: Path) -> None:
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        config_file = tmp_path / "mcp-config.json"
        config_file.write_text(json.dumps({"mcpServers": {"custom": {"env": {"A": "val"}}}}))
        adapter = CopilotClientAdapter()
        with patch.object(adapter, "get_config_path", return_value=str(config_file)):
            keys, _ = adapter._collect_previously_baked_keys("org/server", "custom")
        assert "A" in keys


class TestCopilotEmitInstallSummary:
    """CopilotClientAdapter._emit_install_summary env tracking."""

    def test_unset_env_tracked(self) -> None:
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter()
        adapter._last_env_placeholder_keys = {"MISSING_VAR"}
        old_unset = dict(CopilotClientAdapter._unset_env_keys_by_server)
        try:
            with patch.dict(os.environ, {}, clear=True):
                adapter._emit_install_summary(
                    "test-server", {"env": {"MISSING_VAR": "${MISSING_VAR}"}}
                )
            assert "MISSING_VAR" in CopilotClientAdapter._unset_env_keys_by_server.get(
                "test-server", []
            )
        finally:
            CopilotClientAdapter._unset_env_keys_by_server = old_unset

    def test_set_env_not_tracked(self) -> None:
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter()
        adapter._last_env_placeholder_keys = {"PATH"}
        old_unset = dict(CopilotClientAdapter._unset_env_keys_by_server)
        try:
            adapter._emit_install_summary("test-srv", {"env": {"PATH": "${PATH}"}})
            keys = CopilotClientAdapter._unset_env_keys_by_server.get("test-srv", [])
            assert "PATH" not in keys
        finally:
            CopilotClientAdapter._unset_env_keys_by_server = old_unset


# ---------------------------------------------------------------------------
# Marketplace resolver pure-function tests
# ---------------------------------------------------------------------------


class TestNormalizeOwnerRepoSlug:
    """_normalize_owner_repo_slug."""

    def test_basic(self) -> None:
        from apm_cli.marketplace.resolver import _normalize_owner_repo_slug

        assert _normalize_owner_repo_slug("Owner/Repo") == "owner/repo"

    def test_trailing_slash(self) -> None:
        from apm_cli.marketplace.resolver import _normalize_owner_repo_slug

        assert _normalize_owner_repo_slug("owner/repo/") == "owner/repo"

    def test_dot_git_suffix(self) -> None:
        from apm_cli.marketplace.resolver import _normalize_owner_repo_slug

        assert _normalize_owner_repo_slug("owner/repo.git") == "owner/repo"

    def test_whitespace(self) -> None:
        from apm_cli.marketplace.resolver import _normalize_owner_repo_slug

        assert _normalize_owner_repo_slug("  owner/repo  ") == "owner/repo"


class TestMarketplaceProjectSlug:
    """_marketplace_project_slug."""

    def test_combines_owner_repo(self) -> None:
        from apm_cli.marketplace.resolver import _marketplace_project_slug

        assert _marketplace_project_slug("Microsoft", "APM") == "microsoft/apm"


class TestNormalizeRepoFieldForMatch:
    """_normalize_repo_field_for_match."""

    def test_bare_path(self) -> None:
        from apm_cli.marketplace.resolver import _normalize_repo_field_for_match

        assert _normalize_repo_field_for_match("owner/repo", "github.com") == "owner/repo"

    def test_https_url_same_host(self) -> None:
        from apm_cli.marketplace.resolver import _normalize_repo_field_for_match

        result = _normalize_repo_field_for_match("https://github.com/owner/repo", "github.com")
        assert result == "owner/repo"

    def test_https_url_different_host(self) -> None:
        from apm_cli.marketplace.resolver import _normalize_repo_field_for_match

        result = _normalize_repo_field_for_match("https://gitlab.com/owner/repo", "github.com")
        assert result == ""

    def test_ssh_url_same_host(self) -> None:
        from apm_cli.marketplace.resolver import _normalize_repo_field_for_match

        result = _normalize_repo_field_for_match("git@github.com:owner/repo", "github.com")
        assert result == "owner/repo"

    def test_ssh_url_different_host(self) -> None:
        from apm_cli.marketplace.resolver import _normalize_repo_field_for_match

        result = _normalize_repo_field_for_match("git@gitlab.com:owner/repo", "github.com")
        assert result == ""

    def test_host_qualified_shorthand(self) -> None:
        from apm_cli.marketplace.resolver import _normalize_repo_field_for_match

        result = _normalize_repo_field_for_match("github.com/owner/repo", "github.com")
        assert result == "owner/repo"

    def test_dot_git_stripped(self) -> None:
        from apm_cli.marketplace.resolver import _normalize_repo_field_for_match

        result = _normalize_repo_field_for_match("owner/repo.git", "github.com")
        assert result == "owner/repo"

    def test_trailing_slash_stripped(self) -> None:
        from apm_cli.marketplace.resolver import _normalize_repo_field_for_match

        result = _normalize_repo_field_for_match("owner/repo/", "github.com")
        assert result == "owner/repo"


class TestRepoFieldMatchesMarketplace:
    """_repo_field_matches_marketplace."""

    def test_match(self) -> None:
        from apm_cli.marketplace.resolver import _repo_field_matches_marketplace

        assert _repo_field_matches_marketplace("owner/repo", "owner", "repo", "github.com")

    def test_no_slash(self) -> None:
        from apm_cli.marketplace.resolver import _repo_field_matches_marketplace

        assert not _repo_field_matches_marketplace("noslash", "owner", "repo", "github.com")

    def test_empty(self) -> None:
        from apm_cli.marketplace.resolver import _repo_field_matches_marketplace

        assert not _repo_field_matches_marketplace("", "owner", "repo", "github.com")

    def test_different_repo(self) -> None:
        from apm_cli.marketplace.resolver import _repo_field_matches_marketplace

        assert not _repo_field_matches_marketplace("other/thing", "owner", "repo", "github.com")


class TestCoerceDictPluginType:
    """_coerce_dict_plugin_type."""

    def test_explicit_type(self) -> None:
        from apm_cli.marketplace.resolver import _coerce_dict_plugin_type

        assert _coerce_dict_plugin_type({"type": "GitHub"}) == "github"

    def test_source_key(self) -> None:
        from apm_cli.marketplace.resolver import _coerce_dict_plugin_type

        assert _coerce_dict_plugin_type({"source": "Docker"}) == "docker"

    def test_kind_key(self) -> None:
        from apm_cli.marketplace.resolver import _coerce_dict_plugin_type

        assert _coerce_dict_plugin_type({"kind": "NPM"}) == "npm"

    def test_infer_git_subdir(self) -> None:
        from apm_cli.marketplace.resolver import _coerce_dict_plugin_type

        result = _coerce_dict_plugin_type({"repo": "owner/repo", "subdir": "sub"})
        assert result == "git-subdir"

    def test_infer_github_with_path(self) -> None:
        from apm_cli.marketplace.resolver import _coerce_dict_plugin_type

        result = _coerce_dict_plugin_type({"repo": "owner/repo", "path": "p"})
        assert result == "github"

    def test_infer_github_bare_repo(self) -> None:
        from apm_cli.marketplace.resolver import _coerce_dict_plugin_type

        result = _coerce_dict_plugin_type({"repo": "owner/repo"})
        assert result == "github"

    def test_no_repo_returns_empty(self) -> None:
        from apm_cli.marketplace.resolver import _coerce_dict_plugin_type

        assert _coerce_dict_plugin_type({}) == ""

    def test_repo_no_slash_returns_empty(self) -> None:
        from apm_cli.marketplace.resolver import _coerce_dict_plugin_type

        assert _coerce_dict_plugin_type({"repo": "noslash"}) == ""


class TestMarketplacePluginResolution:
    """MarketplacePluginResolution iteration + fields."""

    def test_iter_yields_canonical_and_plugin(self) -> None:
        from apm_cli.marketplace.models import MarketplacePlugin
        from apm_cli.marketplace.resolver import MarketplacePluginResolution

        plugin = MarketplacePlugin(name="test", source="owner/repo")
        res = MarketplacePluginResolution(canonical="owner/repo", plugin=plugin)
        canonical, p = res
        assert canonical == "owner/repo"
        assert p is plugin

    def test_dependency_reference_default_none(self) -> None:
        from apm_cli.marketplace.models import MarketplacePlugin
        from apm_cli.marketplace.resolver import MarketplacePluginResolution

        plugin = MarketplacePlugin(name="test", source="owner/repo")
        res = MarketplacePluginResolution(canonical="owner/repo", plugin=plugin)
        assert res.dependency_reference is None
        assert res.cross_repo_misconfig_risk is None


class TestCrossRepoMisconfigRisk:
    """CrossRepoMisconfigRisk dataclass."""

    def test_fields(self) -> None:
        from apm_cli.marketplace.resolver import CrossRepoMisconfigRisk

        risk = CrossRepoMisconfigRisk(
            marketplace_host="corp.ghe.com",
            bare_repo_field="owner/repo",
            suggested_qualified_repo="corp.ghe.com/owner/repo",
        )
        assert risk.marketplace_host == "corp.ghe.com"
        assert risk.bare_repo_field == "owner/repo"
        assert risk.suggested_qualified_repo == "corp.ghe.com/owner/repo"


class TestMarketplaceRegex:
    """_MARKETPLACE_RE regex tests."""

    def test_simple_match(self) -> None:
        from apm_cli.marketplace.resolver import _MARKETPLACE_RE

        m = _MARKETPLACE_RE.match("plugin@marketplace")
        assert m is not None
        assert m.group(1) == "plugin"
        assert m.group(2) == "marketplace"
        assert m.group(3) is None

    def test_with_ref(self) -> None:
        from apm_cli.marketplace.resolver import _MARKETPLACE_RE

        m = _MARKETPLACE_RE.match("plugin@marketplace#v1.0")
        assert m is not None
        assert m.group(3) == "v1.0"

    def test_no_match_with_slash(self) -> None:
        from apm_cli.marketplace.resolver import _MARKETPLACE_RE

        assert _MARKETPLACE_RE.match("owner/repo@marketplace") is None

    def test_dots_and_hyphens(self) -> None:
        from apm_cli.marketplace.resolver import _MARKETPLACE_RE

        m = _MARKETPLACE_RE.match("my.plugin-v2@my-marketplace")
        assert m is not None
        assert m.group(1) == "my.plugin-v2"
        assert m.group(2) == "my-marketplace"


class TestSemverRangeChars:
    """_SEMVER_RANGE_CHARS regex."""

    def test_tilde(self) -> None:
        from apm_cli.marketplace.resolver import _SEMVER_RANGE_CHARS

        assert _SEMVER_RANGE_CHARS.search("~1.0") is not None

    def test_caret(self) -> None:
        from apm_cli.marketplace.resolver import _SEMVER_RANGE_CHARS

        assert _SEMVER_RANGE_CHARS.search("^1.0") is not None

    def test_gte(self) -> None:
        from apm_cli.marketplace.resolver import _SEMVER_RANGE_CHARS

        assert _SEMVER_RANGE_CHARS.search(">=1.0") is not None

    def test_plain_ref(self) -> None:
        from apm_cli.marketplace.resolver import _SEMVER_RANGE_CHARS

        assert _SEMVER_RANGE_CHARS.search("main") is None
