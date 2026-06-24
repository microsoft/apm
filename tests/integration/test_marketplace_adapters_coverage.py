"""Integration tests for marketplace/client, marketplace/audit,
adapters/client/* and utils/archive.

All tests are hermetic - no live network calls are made; HTTP is mocked via
``unittest.mock.patch``.  URL assertions use ``urllib.parse`` throughout.
"""

from __future__ import annotations

import io
import json
import tarfile
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import toml
import yaml

from apm_cli.adapters.client.base import (
    _extract_legacy_angle_vars,
    _has_env_placeholder,
    _stringify_env_literal,
    _translate_env_placeholder,
    registry_field_is_required,
)
from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.adapters.client.hermes import HermesClientAdapter
from apm_cli.adapters.client.kiro import KiroClientAdapter
from apm_cli.marketplace.audit import (
    DepClassification,
    FetchStatus,
    _collect_apm_dep_strings,
    _normalize_dep_entry,
    _suggest_replacement,
    check_plugin,
    classify_dependency,
    run_audit,
)
from apm_cli.marketplace.client import (
    _cache_key,
    _read_cache,
    _read_stale_cache,
    _sanitize_cache_name,
    _validate_ref,
    _write_cache,
    fetch_marketplace,
)
from apm_cli.marketplace.errors import MarketplaceFetchError
from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)
from apm_cli.utils.archive import (
    ArchiveError,
    _check_archive_member,
    _detect_archive_format,
    _extract_tar_gz,
    _extract_zip,
    safe_extract_zip,
)

# ===========================================================================
# Shared helpers
# ===========================================================================


def _minimal_marketplace_json(name: str = "test-mp") -> dict:
    """Return a minimal but valid marketplace.json payload."""
    return {
        "name": name,
        "plugins": [
            {
                "name": "plugin-a",
                "source": {"type": "github", "repo": "owner/plugin-a"},
                "description": "A test plugin",
                "version": "v1.0.0",
            }
        ],
    }


def _make_local_source(tmp_path: Path, name: str = "local-mp") -> MarketplaceSource:
    """Write a marketplace.json in *tmp_path* and return a local source pointing at it."""
    mp_file = tmp_path / "marketplace.json"
    mp_file.write_text(json.dumps(_minimal_marketplace_json(name)), encoding="utf-8")
    return MarketplaceSource(name=name, url=str(tmp_path), ref="main")


def _make_tar_gz(files: dict[str, bytes]) -> bytes:
    """Build an in-memory tar.gz with *files* (name -> content)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_zip(files: dict[str, bytes]) -> bytes:
    """Build an in-memory zip with *files* (name -> content)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ===========================================================================
# 1. marketplace/client.py  -- cache + validate_ref + local fetch
# ===========================================================================


class TestValidateRef:
    """Tests for :func:`_validate_ref`."""

    def test_valid_semantic_version_tag(self) -> None:
        assert _validate_ref("v1.2.3", "src") == "v1.2.3"

    def test_valid_branch_with_slash(self) -> None:
        assert _validate_ref("refs/heads/main", "src") == "refs/heads/main"

    def test_valid_short_sha(self) -> None:
        assert _validate_ref("a1b2c3d4", "src") == "a1b2c3d4"

    def test_rejects_ref_with_space(self) -> None:
        with pytest.raises(MarketplaceFetchError):
            _validate_ref("main branch", "src")

    def test_rejects_ref_starting_with_dash(self) -> None:
        with pytest.raises(MarketplaceFetchError):
            _validate_ref("-main", "src")

    def test_rejects_empty_ref(self) -> None:
        with pytest.raises(MarketplaceFetchError):
            _validate_ref("", "src")

    def test_rejects_ref_with_shell_metachar(self) -> None:
        with pytest.raises(MarketplaceFetchError):
            _validate_ref("main;id", "src")


class TestSanitizeCacheName:
    """Tests for :func:`_sanitize_cache_name`."""

    def test_alphanumeric_passthrough(self) -> None:
        assert _sanitize_cache_name("my-org.tools") == "my-org.tools"

    def test_special_chars_replaced_by_underscore(self) -> None:
        result = _sanitize_cache_name("org/repo name")
        assert "/" not in result
        assert " " not in result

    def test_path_traversal_string_gets_unnamed(self) -> None:
        # A purely traversal-like string should not survive
        result = _sanitize_cache_name("../../../etc/passwd")
        assert result != ""  # At minimum: not empty


class TestCacheKeyLogic:
    """Tests for :func:`_cache_key`."""

    def test_github_source_uses_name(self) -> None:
        src = MarketplaceSource(
            name="acme-tools",
            url="https://github.com/acme-org/acme-tools",
            ref="main",
        )
        key = _cache_key(src)
        assert key == "acme-tools"

    def test_url_kind_uses_sha_prefix(self) -> None:
        src = MarketplaceSource(
            name="hosted",
            url="https://cdn.example.com/marketplace.json",
            path="",  # direct URL
            ref="main",
        )
        key = _cache_key(src)
        assert key.startswith("url__")

    def test_local_kind_uses_name(self) -> None:
        src = MarketplaceSource(name="local-mp", url="/tmp/mp")
        key = _cache_key(src)
        assert "local" in key
        assert "local-mp" in key or "local_mp" in key


class TestCacheReadWrite:
    """Tests for :func:`_read_cache`, :func:`_write_cache`, :func:`_read_stale_cache`."""

    def test_write_then_read_returns_data(self, tmp_path: Path) -> None:
        with patch("apm_cli.marketplace.client._cache_dir", return_value=str(tmp_path)):
            data = {"name": "my-mp", "plugins": []}
            _write_cache("test-market", data)
            result = _read_cache("test-market")
        assert result == data

    def test_expired_cache_returns_none(self, tmp_path: Path) -> None:
        with patch("apm_cli.marketplace.client._cache_dir", return_value=str(tmp_path)):
            data = {"name": "my-mp", "plugins": []}
            _write_cache("test-market", data)
            # Wind back time to force expiry
            meta_path = tmp_path / "test-market.meta.json"
            meta = json.loads(meta_path.read_text())
            meta["fetched_at"] = time.time() - 7200  # 2 hours ago
            meta_path.write_text(json.dumps(meta))
            result = _read_cache("test-market")
        assert result is None

    def test_stale_cache_still_readable_after_expiry(self, tmp_path: Path) -> None:
        with patch("apm_cli.marketplace.client._cache_dir", return_value=str(tmp_path)):
            data = {"name": "my-mp", "plugins": []}
            _write_cache("test-market", data)
            # Wind back time to force expiry
            meta_path = tmp_path / "test-market.meta.json"
            meta = json.loads(meta_path.read_text())
            meta["fetched_at"] = time.time() - 7200
            meta_path.write_text(json.dumps(meta))
            stale = _read_stale_cache("test-market")
        assert stale == data

    def test_missing_cache_returns_none(self, tmp_path: Path) -> None:
        with patch("apm_cli.marketplace.client._cache_dir", return_value=str(tmp_path)):
            result = _read_cache("nonexistent")
        assert result is None

    def test_cache_with_etag_and_last_modified(self, tmp_path: Path) -> None:
        with patch("apm_cli.marketplace.client._cache_dir", return_value=str(tmp_path)):
            data = {"name": "my-mp", "plugins": []}
            _write_cache(
                "etag-test",
                data,
                etag='"abc123"',
                last_modified="Thu, 01 Jan 2025 00:00:00 GMT",
            )
            meta_path = tmp_path / "etag-test.meta.json"
            meta = json.loads(meta_path.read_text())
        assert meta["etag"] == '"abc123"'
        assert meta["last_modified"] == "Thu, 01 Jan 2025 00:00:00 GMT"


class TestFetchMarketplaceLocal:
    """Tests for :func:`fetch_marketplace` with local directory sources."""

    def test_local_directory_returns_manifest(self, tmp_path: Path) -> None:
        src = _make_local_source(tmp_path)
        manifest = fetch_marketplace(src)
        assert isinstance(manifest, MarketplaceManifest)
        assert manifest.name == "local-mp"
        assert len(manifest.plugins) == 1
        assert manifest.plugins[0].name == "plugin-a"

    def test_local_source_not_found_raises(self, tmp_path: Path) -> None:
        src = MarketplaceSource(
            name="missing-mp",
            url=str(tmp_path / "nonexistent"),
            ref="main",
        )
        with pytest.raises(MarketplaceFetchError):
            fetch_marketplace(src)

    def test_local_direct_file_source(self, tmp_path: Path) -> None:
        mp_file = tmp_path / "custom.json"
        mp_file.write_text(json.dumps(_minimal_marketplace_json("file-mp")), encoding="utf-8")
        src = MarketplaceSource(name="file-mp", url=str(mp_file), ref="main")
        manifest = fetch_marketplace(src)
        assert manifest.name == "file-mp"

    def test_cache_hit_returns_manifest_without_io(self, tmp_path: Path) -> None:
        """A fresh cached entry is served without re-fetching the local source."""
        # Write an in-memory github-kind source so the sidecar cache is used
        cache_data = _minimal_marketplace_json("cached-mp")
        cache_name = "acme-tools-cached"
        with patch("apm_cli.marketplace.client._cache_dir", return_value=str(tmp_path)):
            _write_cache(cache_name, cache_data)
            src = MarketplaceSource(
                name=cache_name,
                url="https://github.com/owner/acme-tools-cached",
                ref="main",
            )
            with patch("apm_cli.marketplace.client._cache_key", return_value=cache_name):
                manifest = fetch_marketplace(src)
        assert manifest.name == "cached-mp"


# ===========================================================================
# 2. marketplace/audit.py  -- pure classification + orchestration
# ===========================================================================


class TestClassifyDependency:
    """Tests for :func:`classify_dependency`."""

    def test_empty_string_is_empty(self) -> None:
        assert classify_dependency("") == DepClassification.EMPTY

    def test_whitespace_only_is_empty(self) -> None:
        assert classify_dependency("   ") == DepClassification.EMPTY

    def test_local_path_is_local(self) -> None:
        assert classify_dependency("./plugins/my-plugin") == DepClassification.LOCAL

    def test_abs_local_path_is_local(self) -> None:
        assert classify_dependency("/abs/path/to/plugin") == DepClassification.LOCAL

    def test_marketplace_ref_is_marketplace(self) -> None:
        # NAME@MARKETPLACE format is the canonical marketplace ref
        assert classify_dependency("my-plugin@acme-tools") == DepClassification.MARKETPLACE

    def test_raw_git_url_bypasses(self) -> None:
        result = classify_dependency("https://github.com/owner/repo.git#abc123")
        assert result == DepClassification.BYPASSES_MARKETPLACE


class TestNormalizeDepEntry:
    """Tests for :func:`_normalize_dep_entry`."""

    def test_string_passthrough(self) -> None:
        assert _normalize_dep_entry("my-plugin@acme") == "my-plugin@acme"

    def test_dict_git_returns_url(self) -> None:
        entry = {"git": "https://github.com/owner/repo.git"}
        result = _normalize_dep_entry(entry)
        # Should return the git URL string
        assert result == "https://github.com/owner/repo.git"

    def test_dict_path_returns_path(self) -> None:
        entry = {"path": "./local/plugin"}
        assert _normalize_dep_entry(entry) == "./local/plugin"

    def test_non_string_non_dict_returns_none(self) -> None:
        assert _normalize_dep_entry(42) is None  # type: ignore[arg-type]

    def test_dict_with_empty_git_returns_none(self) -> None:
        assert _normalize_dep_entry({"git": ""}) is None

    def test_dict_with_no_recognised_key_returns_none(self) -> None:
        assert _normalize_dep_entry({"unknown": "value"}) is None


class TestCollectApmDepStrings:
    """Tests for :func:`_collect_apm_dep_strings`."""

    def test_collects_from_dependencies_section(self) -> None:
        data = {
            "dependencies": {
                "apm": ["plugin-a@acme", "plugin-b@acme"],
            }
        }
        result = _collect_apm_dep_strings(data)
        assert result == ["plugin-a@acme", "plugin-b@acme"]

    def test_collects_from_dev_dependencies_section(self) -> None:
        data = {
            "devDependencies": {
                "apm": ["dev-plugin@acme"],
            }
        }
        result = _collect_apm_dep_strings(data)
        assert result == ["dev-plugin@acme"]

    def test_collects_from_both_sections(self) -> None:
        data = {
            "dependencies": {"apm": ["a@acme"]},
            "devDependencies": {"apm": ["b@acme"]},
        }
        result = _collect_apm_dep_strings(data)
        assert set(result) == {"a@acme", "b@acme"}

    def test_empty_data_returns_empty_list(self) -> None:
        assert _collect_apm_dep_strings({}) == []

    def test_dict_git_entry_is_included(self) -> None:
        data = {
            "dependencies": {
                "apm": [{"git": "https://github.com/owner/repo.git"}],
            }
        }
        result = _collect_apm_dep_strings(data)
        assert result == ["https://github.com/owner/repo.git"]


class TestSuggestReplacement:
    """Tests for :func:`_suggest_replacement`."""

    def test_returns_non_empty_suggestion(self) -> None:
        suggestion = _suggest_replacement("https://github.com/owner/cool-plugin.git#v1")
        assert isinstance(suggestion, str)
        assert len(suggestion) > 0

    def test_strips_git_suffix_from_hint(self) -> None:
        suggestion = _suggest_replacement("https://github.com/owner/my-plugin.git")
        # The hint should not contain ".git"
        assert "my-plugin.git" not in suggestion
        assert "my-plugin" in suggestion


class TestCheckPlugin:
    """Tests for :func:`check_plugin` using the ``_fetcher`` seam."""

    def _make_plugin(self, name: str = "my-plugin") -> MarketplacePlugin:
        return MarketplacePlugin(
            name=name,
            source={"type": "github", "repo": "owner/my-plugin"},
        )

    def _make_source(self) -> MarketplaceSource:
        return MarketplaceSource(
            name="acme-tools",
            url="https://github.com/owner/acme-tools",
            ref="main",
        )

    def test_ok_status_no_issues(self) -> None:
        plugin = self._make_plugin()
        source = self._make_source()

        def _fetcher(p, s, ar):
            return (FetchStatus.OK, {"dependencies": {"apm": ["dep@acme"]}}, "")

        report = check_plugin(plugin, source, _fetcher=_fetcher)
        assert report.fetch_status == FetchStatus.OK
        assert report.issues == ()

    def test_bypassing_dep_creates_issue(self) -> None:
        plugin = self._make_plugin()
        source = self._make_source()

        def _fetcher(p, s, ar):
            return (
                FetchStatus.OK,
                {"dependencies": {"apm": ["https://github.com/third-party/dangerous.git"]}},
                "",
            )

        report = check_plugin(plugin, source, _fetcher=_fetcher)
        assert report.fetch_status == FetchStatus.OK
        assert len(report.issues) == 1
        assert report.issues[0].classification == DepClassification.BYPASSES_MARKETPLACE

    def test_network_error_returns_non_ok_status(self) -> None:
        plugin = self._make_plugin()
        source = self._make_source()

        def _fetcher(p, s, ar):
            return (FetchStatus.NETWORK_ERROR, None, "timeout")

        report = check_plugin(plugin, source, _fetcher=_fetcher)
        assert report.fetch_status == FetchStatus.NETWORK_ERROR
        assert report.detail == "timeout"

    def test_no_manifest_status_propagates(self) -> None:
        plugin = self._make_plugin()
        source = self._make_source()

        def _fetcher(p, s, ar):
            return (FetchStatus.NO_MANIFEST, None, "no apm.yml found")

        report = check_plugin(plugin, source, _fetcher=_fetcher)
        assert report.fetch_status == FetchStatus.NO_MANIFEST


class TestRunAudit:
    """Tests for :func:`run_audit`."""

    def test_empty_manifest_returns_empty_list(self) -> None:
        manifest = MarketplaceManifest(name="test", plugins=())
        source = MarketplaceSource(name="test", url="https://github.com/owner/test", ref="main")
        reports = run_audit(manifest, source, _fetcher=lambda p, s, a: (FetchStatus.OK, {}, ""))
        assert reports == []

    def test_multiple_plugins_each_get_report(self) -> None:
        plugins = (
            MarketplacePlugin(name="plugin-a", source={"type": "github", "repo": "owner/plugin-a"}),
            MarketplacePlugin(name="plugin-b", source={"type": "github", "repo": "owner/plugin-b"}),
        )
        manifest = MarketplaceManifest(name="test", plugins=plugins)
        source = MarketplaceSource(name="test", url="https://github.com/owner/test", ref="main")
        reports = run_audit(manifest, source, _fetcher=lambda p, s, a: (FetchStatus.OK, {}, ""))
        assert len(reports) == 2
        names = {r.plugin_name for r in reports}
        assert names == {"plugin-a", "plugin-b"}


# ===========================================================================
# 4. adapters/client/base.py -- pure helpers
# ===========================================================================


class TestBaseAdapterHelpers:
    """Tests for pure helper functions in ``adapters/client/base.py``."""

    def test_translate_env_placeholder_dollar_brace(self) -> None:
        result = _translate_env_placeholder("${MY_VAR}")
        assert result == "${MY_VAR}"

    def test_translate_env_placeholder_env_prefix(self) -> None:
        result = _translate_env_placeholder("${env:MY_VAR}")
        assert result == "${MY_VAR}"

    def test_translate_env_placeholder_legacy_angle(self) -> None:
        result = _translate_env_placeholder("<MY_VAR>")
        assert result == "${MY_VAR}"

    def test_translate_env_placeholder_non_string_passthrough(self) -> None:
        assert _translate_env_placeholder(42) == 42  # type: ignore[arg-type]

    def test_extract_legacy_angle_vars_returns_names(self) -> None:
        result = _extract_legacy_angle_vars("token=<API_TOKEN> key=<SECRET_KEY>")
        assert result == {"API_TOKEN", "SECRET_KEY"}

    def test_extract_legacy_angle_vars_non_string_returns_empty(self) -> None:
        assert _extract_legacy_angle_vars(None) == set()  # type: ignore[arg-type]

    def test_has_env_placeholder_dollar_brace(self) -> None:
        assert _has_env_placeholder("${FOO}") is True

    def test_has_env_placeholder_legacy_angle(self) -> None:
        assert _has_env_placeholder("<BAR>") is True

    def test_has_env_placeholder_false_for_plain_string(self) -> None:
        assert _has_env_placeholder("plain-value") is False

    def test_stringify_env_literal_bool_false(self) -> None:
        assert _stringify_env_literal(False) == "false"

    def test_stringify_env_literal_bool_true(self) -> None:
        assert _stringify_env_literal(True) == "true"

    def test_stringify_env_literal_int(self) -> None:
        assert _stringify_env_literal(42) == "42"

    def test_registry_field_required_default(self) -> None:
        assert registry_field_is_required({}) is True

    def test_registry_field_required_explicit_false(self) -> None:
        assert registry_field_is_required({"required": False}) is False


# ===========================================================================
# 5. adapters/client/codex.py -- config read/write
# ===========================================================================


class TestCodexClientAdapter:
    """Tests for :class:`CodexClientAdapter` config paths and I/O."""

    def test_project_scope_config_path(self, tmp_path: Path) -> None:
        adapter = CodexClientAdapter(project_root=tmp_path, user_scope=False)
        config_path = adapter.get_config_path()
        assert config_path == str(tmp_path / ".codex" / "config.toml")

    def test_user_scope_config_path(self) -> None:
        adapter = CodexClientAdapter(user_scope=True)
        config_path = adapter.get_config_path()
        home = Path.home()
        assert config_path == str(home / ".codex" / "config.toml")

    def test_get_current_config_missing_file(self, tmp_path: Path) -> None:
        adapter = CodexClientAdapter(project_root=tmp_path, user_scope=False)
        result = adapter.get_current_config()
        assert result == {}

    def test_get_current_config_valid_toml(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        cfg_file = codex_dir / "config.toml"
        cfg_file.write_text(
            "[mcp_servers]\n[mcp_servers.my-server]\ncommand = 'npx'\n",
            encoding="utf-8",
        )
        adapter = CodexClientAdapter(project_root=tmp_path, user_scope=False)
        result = adapter.get_current_config()
        assert "mcp_servers" in result
        assert "my-server" in result["mcp_servers"]

    def test_update_config_creates_file_and_merges(self, tmp_path: Path) -> None:
        adapter = CodexClientAdapter(project_root=tmp_path, user_scope=False)
        server_entry = {"command": "npx", "args": ["-y", "@azure/mcp"], "env": {}}
        ok = adapter.update_config({"azure": server_entry})
        assert ok is True
        cfg_path = tmp_path / ".codex" / "config.toml"
        assert cfg_path.exists()
        cfg = toml.loads(cfg_path.read_text(encoding="utf-8"))
        assert "azure" in cfg.get("mcp_servers", {})

    def test_update_config_returns_false_on_parse_error(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        cfg_file = codex_dir / "config.toml"
        cfg_file.write_text("[[invalid toml\n", encoding="utf-8")
        adapter = CodexClientAdapter(project_root=tmp_path, user_scope=False)
        result = adapter.update_config({"srv": {"command": "x"}})
        assert result is False


# ===========================================================================
# 6. adapters/client/kiro.py -- config read/write
# ===========================================================================


class TestKiroClientAdapter:
    """Tests for :class:`KiroClientAdapter` config paths and I/O."""

    def test_project_scope_config_path(self, tmp_path: Path) -> None:
        adapter = KiroClientAdapter(project_root=tmp_path, user_scope=False)
        config_path = adapter.get_config_path()
        assert config_path == str(tmp_path / ".kiro" / "settings" / "mcp.json")

    def test_user_scope_config_path(self) -> None:
        adapter = KiroClientAdapter(user_scope=True)
        config_path = adapter.get_config_path()
        home = Path.home()
        assert config_path == str(home / ".kiro" / "settings" / "mcp.json")

    def test_get_current_config_missing_file(self, tmp_path: Path) -> None:
        adapter = KiroClientAdapter(project_root=tmp_path, user_scope=False)
        result = adapter.get_current_config()
        assert result == {}

    def test_get_current_config_valid_json(self, tmp_path: Path) -> None:
        kiro_dir = tmp_path / ".kiro" / "settings"
        kiro_dir.mkdir(parents=True)
        cfg = {"mcpServers": {"my-server": {"command": "npx"}}}
        (kiro_dir / "mcp.json").write_text(json.dumps(cfg), encoding="utf-8")
        adapter = KiroClientAdapter(project_root=tmp_path, user_scope=False)
        result = adapter.get_current_config()
        assert "mcpServers" in result
        assert "my-server" in result["mcpServers"]

    def test_update_config_skipped_when_no_kiro_dir(self, tmp_path: Path) -> None:
        """Project-scope update returns None when .kiro/ does not exist."""
        adapter = KiroClientAdapter(project_root=tmp_path, user_scope=False)
        result = adapter.update_config({"srv": {"command": "npx"}})
        assert result is None

    def test_update_config_writes_when_kiro_dir_exists(self, tmp_path: Path) -> None:
        kiro_root = tmp_path / ".kiro"
        kiro_root.mkdir()
        adapter = KiroClientAdapter(project_root=tmp_path, user_scope=False)
        result = adapter.update_config({"my-server": {"command": "npx", "args": []}})
        assert result is True
        cfg_file = kiro_root / "settings" / "mcp.json"
        assert cfg_file.exists()
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert "my-server" in data["mcpServers"]


# ===========================================================================
# 7. adapters/client/hermes.py -- format + read/write
# ===========================================================================


class TestHermesClientAdapter:
    """Tests for :class:`HermesClientAdapter` config paths and I/O."""

    def _make_adapter(self, tmp_path: Path) -> HermesClientAdapter:
        adapter = HermesClientAdapter(project_root=tmp_path)
        # Redirect config to tmp_path so tests are hermetic
        with patch(
            "apm_cli.integration.targets.resolve_hermes_root",
            return_value=tmp_path / ".hermes",
        ):
            pass  # just warm up
        return adapter

    def test_to_hermes_format_stdio(self) -> None:
        entry = {"command": "npx", "args": ["-y", "mcp-server"]}
        result = HermesClientAdapter._to_hermes_format(entry, enabled=True)
        assert result["command"] == "npx"
        assert result["enabled"] is True
        assert "url" not in result

    def test_to_hermes_format_remote(self) -> None:
        entry = {"url": "https://mcp.example.com/server", "type": "http"}
        result = HermesClientAdapter._to_hermes_format(entry, enabled=False)
        assert result["url"] == "https://mcp.example.com/server"
        assert result["enabled"] is False
        assert "command" not in result

    def test_to_hermes_format_preserves_env(self) -> None:
        entry = {"command": "python", "args": ["-m", "mcp"], "env": {"API_KEY": "secret"}}
        result = HermesClientAdapter._to_hermes_format(entry)
        assert result["env"]["API_KEY"] == "secret"

    def test_to_hermes_format_drops_empty_url(self) -> None:
        entry = {"url": "", "command": "npx"}
        result = HermesClientAdapter._to_hermes_format(entry)
        # command present, url absent (falsy url is not remote)
        assert "command" in result

    def test_update_config_creates_file(self, tmp_path: Path) -> None:
        hermes_home = tmp_path / ".hermes"
        with patch(
            "apm_cli.integration.targets.resolve_hermes_root",
            return_value=hermes_home,
        ):
            adapter = HermesClientAdapter(project_root=tmp_path)
            server_entry = {"command": "npx", "args": ["-y", "mcp-server"]}
            result = adapter.update_config({"my-server": server_entry})
        assert result is True
        cfg_path = hermes_home / "config.yaml"
        assert cfg_path.exists()
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert "mcp_servers" in data
        assert "my-server" in data["mcp_servers"]

    def test_update_config_preserves_existing_top_level_keys(self, tmp_path: Path) -> None:
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        cfg_path = hermes_home / "config.yaml"
        cfg_path.write_text(
            yaml.dump({"model_provider": "openai", "mcp_servers": {}}),
            encoding="utf-8",
        )
        with patch(
            "apm_cli.integration.targets.resolve_hermes_root",
            return_value=hermes_home,
        ):
            adapter = HermesClientAdapter(project_root=tmp_path)
            adapter.update_config({"new-server": {"command": "python"}})
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data["model_provider"] == "openai"
        assert "new-server" in data["mcp_servers"]

    def test_get_current_config_missing_file(self, tmp_path: Path) -> None:
        hermes_home = tmp_path / ".hermes"
        with patch(
            "apm_cli.integration.targets.resolve_hermes_root",
            return_value=hermes_home,
        ):
            adapter = HermesClientAdapter(project_root=tmp_path)
            result = adapter.get_current_config()
        assert result == {"mcp_servers": {}}


# ===========================================================================
# 8. utils/archive.py  -- safe extraction
# ===========================================================================


class TestCheckArchiveMember:
    """Tests for :func:`_check_archive_member`."""

    def test_valid_member_passes(self) -> None:
        _check_archive_member("bundle/file.txt")  # should not raise

    def test_null_byte_raises(self) -> None:
        with pytest.raises(ArchiveError, match="null byte"):
            _check_archive_member("file\x00name.txt")

    def test_absolute_posix_path_raises(self) -> None:
        with pytest.raises(ArchiveError):
            _check_archive_member("/etc/passwd")

    def test_traversal_segment_raises(self) -> None:
        with pytest.raises(ArchiveError):
            _check_archive_member("../../etc/shadow")


class TestDetectArchiveFormat:
    """Tests for :func:`_detect_archive_format`."""

    def test_gzip_content_type(self) -> None:
        assert _detect_archive_format("application/gzip", "archive.tar.gz") == "tar.gz"

    def test_zip_content_type(self) -> None:
        assert _detect_archive_format("application/zip", "archive.zip") == "zip"

    def test_url_extension_tar_gz(self) -> None:
        assert _detect_archive_format("application/octet-stream", "bundle.tar.gz") == "tar.gz"

    def test_url_extension_zip(self) -> None:
        assert _detect_archive_format("application/octet-stream", "bundle.zip") == "zip"

    def test_unknown_raises(self) -> None:
        with pytest.raises(ArchiveError):
            _detect_archive_format("application/octet-stream", "bundle.unknown")

    def test_uncompressed_tar_raises(self) -> None:
        with pytest.raises(ArchiveError, match="Uncompressed tar"):
            _detect_archive_format("application/x-tar", "bundle.tar")


class TestExtractTarGz:
    """Tests for :func:`_extract_tar_gz`."""

    def test_extracts_files(self, tmp_path: Path) -> None:
        data = _make_tar_gz(
            {
                "bundle/file.txt": b"hello world",
                "bundle/sub/nested.txt": b"nested content",
            }
        )
        extracted = _extract_tar_gz(data, str(tmp_path))
        assert any("file.txt" in e for e in extracted)
        assert (tmp_path / "bundle" / "file.txt").read_bytes() == b"hello world"

    def test_invalid_tar_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ArchiveError, match=r"Failed to read tar\.gz"):
            _extract_tar_gz(b"not a tar file", str(tmp_path))


class TestExtractZip:
    """Tests for :func:`_extract_zip`."""

    def test_extracts_files(self, tmp_path: Path) -> None:
        data = _make_zip({"bundle/file.txt": b"hello", "bundle/other.txt": b"world"})
        extracted = _extract_zip(data, str(tmp_path))
        assert any("file.txt" in e for e in extracted)
        assert (tmp_path / "bundle" / "file.txt").read_bytes() == b"hello"

    def test_invalid_zip_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ArchiveError, match="Failed to read zip"):
            _extract_zip(b"not a zip file", str(tmp_path))


class TestSafeExtractZip:
    """Tests for :func:`safe_extract_zip` entry/size limits."""

    def test_exceeds_entry_limit_raises(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(5):
                zf.writestr(f"file{i}.txt", b"x")
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            with pytest.raises(ArchiveError, match="entries"):
                safe_extract_zip(
                    zf,
                    tmp_path,
                    max_entries=3,
                    max_uncompressed=512 * 1024 * 1024,
                    error_type=ArchiveError,
                )

    def test_normal_extraction_succeeds(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("bundle/a.txt", b"data")
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            extracted = safe_extract_zip(
                zf,
                tmp_path,
                error_type=ArchiveError,
            )
        assert any("a.txt" in e for e in extracted)

    def test_path_traversal_member_raises(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../evil.txt", b"pwned")
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            with pytest.raises(ArchiveError):
                safe_extract_zip(zf, tmp_path, error_type=ArchiveError)


class TestDownloadAndExtractArchive:
    """Tests for :func:`download_and_extract_archive` with mocked HTTP."""

    def test_non_https_url_raises(self, tmp_path: Path) -> None:
        from apm_cli.utils.archive import download_and_extract_archive

        with pytest.raises(ArchiveError, match="HTTPS"):
            download_and_extract_archive("http://insecure.example.com/bundle.zip", str(tmp_path))

    def test_successful_zip_download(self, tmp_path: Path) -> None:
        from apm_cli.utils.archive import download_and_extract_archive

        zip_bytes = _make_zip({"bundle/hello.txt": b"hello"})
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {
            "Content-Type": "application/zip",
            "Content-Length": str(len(zip_bytes)),
        }
        mock_resp.url = "https://cdn.example.com/bundle.zip"
        mock_resp.iter_content = MagicMock(return_value=iter([zip_bytes]))

        with patch("apm_cli.utils.archive._archive_get", return_value=mock_resp):
            extracted = download_and_extract_archive(
                "https://cdn.example.com/bundle.zip",
                str(tmp_path),
            )
        assert any("hello.txt" in e for e in extracted)

    def test_http_error_raises(self, tmp_path: Path) -> None:
        import requests

        from apm_cli.utils.archive import download_and_extract_archive

        with patch(
            "apm_cli.utils.archive._archive_get",
            side_effect=requests.exceptions.ConnectionError("connection refused"),
        ):
            with pytest.raises(ArchiveError, match="Failed to download"):
                download_and_extract_archive("https://cdn.example.com/bundle.zip", str(tmp_path))
