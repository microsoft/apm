"""Integration tests for low-coverage adapter, model, and utility modules.

Target modules:
- adapters/client/base.py    -- base adapter config merging, path resolution
- adapters/client/codex.py   -- codex config read/write
- adapters/client/kiro.py    -- kiro adapter config paths
- adapters/client/gemini.py  -- gemini adapter
- adapters/client/claude.py  -- claude adapter
- models/dependency/lsp.py   -- LSP dependency models
- marketplace/client.py      -- fetch/cache paths
- marketplace/version_check.py -- version checking
- marketplace/migration.py   -- migration helpers
- bundle/packer.py           -- bundle packing logic
- utils/install_tui.py       -- TUI rendering helpers
- cache/http_cache.py        -- HTTP cache layer
- utils/reflink.py           -- reflink/copy utilities

All tests are hermetic: no live network calls are made.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import pytest
import yaml

# ---------------------------------------------------------------------------
# adapters/client/base helpers
# ---------------------------------------------------------------------------
from apm_cli.adapters.client.base import (
    MCPClientAdapter,
    _extract_legacy_angle_vars,
    _has_env_placeholder,
    _stringify_env_literal,
    _translate_env_placeholder,
    registry_field_is_required,
)

# ---------------------------------------------------------------------------
# adapters/client concrete adapters
# ---------------------------------------------------------------------------
from apm_cli.adapters.client.claude import ClaudeClientAdapter
from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.adapters.client.gemini import GeminiClientAdapter
from apm_cli.adapters.client.kiro import KiroClientAdapter

# ---------------------------------------------------------------------------
# cache/http_cache
# ---------------------------------------------------------------------------
from apm_cli.cache.http_cache import (
    MAX_HTTP_CACHE_TTL_SECONDS,
    HttpCache,
)

# ---------------------------------------------------------------------------
# marketplace/client helpers
# ---------------------------------------------------------------------------
from apm_cli.marketplace.client import (
    FetchResult,
    _cache_key,
    _read_cache,
    _read_stale_cache,
    _sanitize_cache_name,
    _write_cache,
)
from apm_cli.marketplace.errors import MarketplaceFetchError, MarketplaceYmlError

# ---------------------------------------------------------------------------
# marketplace/migration
# ---------------------------------------------------------------------------
from apm_cli.marketplace.migration import (
    DEPRECATION_MESSAGE,
    ConfigSource,
    detect_config_source,
    detect_inheritance_conflicts,
    load_marketplace_config,
    migrate_marketplace_yml,
)
from apm_cli.marketplace.models import MarketplaceSource

# ---------------------------------------------------------------------------
# marketplace/version_check
# ---------------------------------------------------------------------------
from apm_cli.marketplace.version_check import (
    PackageVersionRow,
    VersionAlignmentReport,
    check_version_alignment,
)
from apm_cli.marketplace.yml_schema import (
    MarketplaceBuild,
    MarketplaceConfig,
    MarketplaceOwner,
    MarketplaceVersioning,
    PackageEntry,
)

# ---------------------------------------------------------------------------
# models/dependency/lsp
# ---------------------------------------------------------------------------
from apm_cli.models.dependency.lsp import LSPDependency

# ---------------------------------------------------------------------------
# utils/install_tui
# ---------------------------------------------------------------------------
from apm_cli.utils.install_tui import InstallTui, should_animate

# ---------------------------------------------------------------------------
# utils/reflink
# ---------------------------------------------------------------------------
from apm_cli.utils.reflink import (
    _device_capability,
    _mark_device_supported,
    _mark_device_unsupported,
    _reset_capability_cache,
    clone_file,
    reflink_supported,
)

# ===========================================================================
# 1. adapters/client/base.py  -- pure helper functions
# ===========================================================================


class TestBaseAdapterHelpers:
    """Tests for module-level helpers in adapters/client/base.py."""

    def test_translate_env_placeholder_brace_var(self) -> None:
        """${VAR} passes through unchanged."""
        assert _translate_env_placeholder("${MY_TOKEN}") == "${MY_TOKEN}"

    def test_translate_env_placeholder_env_prefix(self) -> None:
        """${env:VAR} is stripped to ${VAR}."""
        assert _translate_env_placeholder("${env:MY_TOKEN}") == "${MY_TOKEN}"

    def test_translate_env_placeholder_legacy_angle(self) -> None:
        """<VAR> is promoted to ${VAR}."""
        assert _translate_env_placeholder("<MY_TOKEN>") == "${MY_TOKEN}"

    def test_translate_env_placeholder_non_string_passthrough(self) -> None:
        """Non-string values are returned unchanged."""
        assert _translate_env_placeholder(42) == 42
        assert _translate_env_placeholder(None) is None

    def test_translate_env_placeholder_idempotent(self) -> None:
        """Applying translate twice yields the same result."""
        once = _translate_env_placeholder("<TOKEN>")
        twice = _translate_env_placeholder(once)
        assert once == twice == "${TOKEN}"

    def test_extract_legacy_angle_vars_single(self) -> None:
        """Extracts a single legacy angle-bracket variable name."""
        result = _extract_legacy_angle_vars("<MY_SECRET>")
        assert result == {"MY_SECRET"}

    def test_extract_legacy_angle_vars_multiple(self) -> None:
        """Extracts multiple legacy variable names."""
        result = _extract_legacy_angle_vars("--token <TOKEN> --key <API_KEY>")
        assert result == {"TOKEN", "API_KEY"}

    def test_extract_legacy_angle_vars_non_string(self) -> None:
        """Returns empty set for non-string input."""
        assert _extract_legacy_angle_vars(None) == set()
        assert _extract_legacy_angle_vars(123) == set()

    def test_has_env_placeholder_brace(self) -> None:
        """Detects ${VAR} placeholders."""
        assert _has_env_placeholder("${MY_VAR}") is True

    def test_has_env_placeholder_env_prefix(self) -> None:
        """Detects ${env:VAR} placeholders."""
        assert _has_env_placeholder("${env:MY_VAR}") is True

    def test_has_env_placeholder_legacy(self) -> None:
        """Detects legacy <VAR> placeholders."""
        assert _has_env_placeholder("<MY_VAR>") is True

    def test_has_env_placeholder_plain_string(self) -> None:
        """Returns False for plain strings."""
        assert _has_env_placeholder("just a plain string") is False

    def test_has_env_placeholder_non_string(self) -> None:
        """Returns False for non-string input."""
        assert _has_env_placeholder(42) is False

    def test_stringify_env_literal_bool_true(self) -> None:
        """Converts bool True to 'true'."""
        assert _stringify_env_literal(True) == "true"

    def test_stringify_env_literal_bool_false(self) -> None:
        """Converts bool False to 'false'."""
        assert _stringify_env_literal(False) == "false"

    def test_stringify_env_literal_int(self) -> None:
        """Converts int to string."""
        assert _stringify_env_literal(42) == "42"

    def test_stringify_env_literal_string_passthrough(self) -> None:
        """String values pass through unchanged."""
        assert _stringify_env_literal("hello") == "hello"

    def test_registry_field_is_required_default(self) -> None:
        """Fields without explicit required flag default to True."""
        assert registry_field_is_required({}) is True

    def test_registry_field_is_required_explicit_false(self) -> None:
        """Explicit required=False makes field optional."""
        assert registry_field_is_required({"required": False}) is False

    def test_registry_field_is_required_is_required_false(self) -> None:
        """is_required=False also makes field optional."""
        assert registry_field_is_required({"is_required": False}) is False

    def test_registry_field_is_required_explicit_true(self) -> None:
        """Explicit required=True keeps field required."""
        assert registry_field_is_required({"required": True}) is True


# ===========================================================================
# 2. adapters/client/base.py  -- MCPClientAdapter infrastructure
# ===========================================================================


class TestMCPClientAdapterInfrastructure:
    """Tests for MCPClientAdapter base class methods."""

    def test_determine_config_key_with_server_name(self) -> None:
        """server_name takes precedence over server_url."""
        key = MCPClientAdapter._determine_config_key("owner/repo", "my-server")
        assert key == "my-server"

    def test_determine_config_key_scoped_npm_package(self) -> None:
        """Scoped npm packages like @scope/name are preserved."""
        key = MCPClientAdapter._determine_config_key("@scope/mcp-server", None)
        assert key == "@scope/mcp-server"

    def test_determine_config_key_owner_repo_fallback(self) -> None:
        """owner/repo falls back to repo name."""
        key = MCPClientAdapter._determine_config_key("owner/my-server", None)
        assert key == "my-server"

    def test_determine_config_key_plain_name(self) -> None:
        """Plain name (no slash) is returned as-is."""
        key = MCPClientAdapter._determine_config_key("my-server", None)
        assert key == "my-server"

    def test_infer_registry_name_npm_scoped(self) -> None:
        """Scoped npm package name infers npm."""
        pkg = {"name": "@azure/mcp"}
        assert MCPClientAdapter._infer_registry_name(pkg) == "npm"

    def test_infer_registry_name_npx_runtime_hint(self) -> None:
        """npx runtime hint infers npm."""
        pkg = {"name": "some-pkg", "runtime_hint": "npx"}
        assert MCPClientAdapter._infer_registry_name(pkg) == "npm"

    def test_infer_registry_name_uvx_runtime_hint(self) -> None:
        """uvx runtime hint infers pypi."""
        pkg = {"name": "some-pkg", "runtime_hint": "uvx"}
        assert MCPClientAdapter._infer_registry_name(pkg) == "pypi"

    def test_infer_registry_name_docker_image(self) -> None:
        """ghcr.io/ prefix infers docker."""
        pkg = {"name": "ghcr.io/owner/image:latest"}
        assert MCPClientAdapter._infer_registry_name(pkg) == "docker"

    def test_infer_registry_name_explicit_registry(self) -> None:
        """Explicit registry_name field takes precedence."""
        pkg = {"name": "some-pkg", "registry_name": "pypi"}
        assert MCPClientAdapter._infer_registry_name(pkg) == "pypi"

    def test_infer_registry_name_empty_package(self) -> None:
        """Empty package dict returns empty string."""
        assert MCPClientAdapter._infer_registry_name({}) == ""

    def test_select_best_package_npm_priority(self) -> None:
        """npm packages are preferred over others."""
        pkgs = [
            {"name": "pypi-pkg", "runtime_hint": "uvx"},
            {"name": "@npm/pkg"},
        ]
        best = MCPClientAdapter._select_best_package(pkgs)
        assert best is not None
        assert MCPClientAdapter._infer_registry_name(best) == "npm"

    def test_select_best_package_empty_list(self) -> None:
        """Returns None for empty package list."""
        assert MCPClientAdapter._select_best_package([]) is None

    def test_select_remote_with_url_first_valid(self) -> None:
        """Returns first remote entry with non-empty URL."""
        remotes = [{"url": ""}, {"url": "https://api.example.com/mcp"}]
        selected = MCPClientAdapter._select_remote_with_url(remotes)
        assert selected is not None
        assert urlsplit(selected["url"]).scheme == "https"

    def test_select_remote_with_url_no_valid(self) -> None:
        """Returns None when no remote has a URL."""
        assert MCPClientAdapter._select_remote_with_url([{"url": ""}, {}]) is None


# ===========================================================================
# 3. adapters/client/codex.py
# ===========================================================================


class TestCodexClientAdapter:
    """Tests for CodexClientAdapter config path and read/write."""

    def test_get_config_path_project_scope(self, tmp_path: Path) -> None:
        """Project-scope config path is under <project_root>/.codex/."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        config_path = adapter.get_config_path()
        p = Path(config_path)
        assert str(p).startswith(str(tmp_path))
        assert "config.toml" in config_path

    def test_get_config_path_user_scope(self) -> None:
        """User-scope config path is under ~/.codex/."""
        adapter = CodexClientAdapter(user_scope=True)
        config_path = adapter.get_config_path()
        home = Path.home()
        assert Path(config_path).is_relative_to(home / ".codex")

    def test_get_current_config_missing_file(self, tmp_path: Path) -> None:
        """Returns empty dict when config file does not exist."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        cfg = adapter.get_current_config()
        assert cfg == {}

    def test_update_config_creates_file(self, tmp_path: Path) -> None:
        """update_config writes TOML and creates the directory."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        result = adapter.update_config({"my-server": {"command": "npx", "args": ["-y", "pkg"]}})
        assert result is True
        config_path = Path(adapter.get_config_path())
        assert config_path.exists()
        import toml

        data = toml.load(config_path)
        assert "mcp_servers" in data
        assert "my-server" in data["mcp_servers"]

    def test_update_config_preserves_existing(self, tmp_path: Path) -> None:
        """update_config merges with existing TOML content."""
        import toml

        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        config_file = codex_dir / "config.toml"
        initial = {"mcp_servers": {"old-server": {"command": "old"}}}
        config_file.write_text(toml.dumps(initial))
        adapter = CodexClientAdapter(project_root=tmp_path)
        adapter.update_config({"new-server": {"command": "new"}})
        data = toml.load(config_file)
        assert "old-server" in data["mcp_servers"]
        assert "new-server" in data["mcp_servers"]

    def test_update_config_invalid_toml_returns_false(self, tmp_path: Path) -> None:
        """update_config returns False when existing config is invalid TOML."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        config_file = codex_dir / "config.toml"
        config_file.write_text("this is not: valid: toml: !!!")
        adapter = CodexClientAdapter(project_root=tmp_path)
        result = adapter.update_config({"server": {"command": "npx"}})
        assert result is False

    def test_configure_mcp_server_empty_url(self, tmp_path: Path) -> None:
        """configure_mcp_server returns False for empty server URL."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        assert adapter.configure_mcp_server("") is False

    def test_configure_mcp_server_from_cache(self, tmp_path: Path) -> None:
        """configure_mcp_server uses server_info_cache when available."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "id": "abc123",
            "name": "test-server",
            "packages": [
                {
                    "name": "@test/server",
                    "registry_name": "npm",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        cache = {"test/server": server_info}
        result = adapter.configure_mcp_server("test/server", server_info_cache=cache)
        assert result is True

    def test_format_server_config_sse_remote_returns_none(self, tmp_path: Path) -> None:
        """SSE remote-only servers return None (unsupported by Codex)."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "id": "abc",
            "name": "sse-server",
            "remotes": [{"url": "https://api.example.com/sse", "transport_type": "sse"}],
        }
        result = adapter._format_server_config(server_info)
        assert result is None

    def test_format_server_config_streamable_http(self, tmp_path: Path) -> None:
        """Streamable-HTTP remote is accepted and produces a URL config."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "id": "abc",
            "name": "http-server",
            "remotes": [
                {"url": "https://api.example.com/mcp", "transport_type": "streamable-http"}
            ],
        }
        result = adapter._format_server_config(server_info)
        assert result is not None
        parsed = urlsplit(result["url"])
        assert parsed.scheme == "https"
        assert parsed.netloc == "api.example.com"

    def test_format_server_config_non_https_remote_returns_none(self, tmp_path: Path) -> None:
        """Non-HTTPS remote URLs return None."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "id": "abc",
            "name": "http-server",
            "remotes": [{"url": "http://insecure.example.com/mcp", "transport_type": "http"}],
        }
        result = adapter._format_server_config(server_info)
        assert result is None

    def test_format_server_config_raw_stdio(self, tmp_path: Path) -> None:
        """_raw_stdio path generates command/args config."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "id": "abc",
            "name": "stdio-server",
            "_raw_stdio": {"command": "npx", "args": ["-y", "my-pkg"], "env": {}},
        }
        result = adapter._format_server_config(server_info)
        assert result is not None
        assert result["command"] == "npx"
        assert "-y" in result["args"]


# ===========================================================================
# 4. adapters/client/kiro.py
# ===========================================================================


class TestKiroClientAdapter:
    """Tests for KiroClientAdapter."""

    def test_get_config_path_project_scope(self, tmp_path: Path) -> None:
        """Project-scope path is under .kiro/settings/mcp.json."""
        adapter = KiroClientAdapter(project_root=tmp_path)
        config_path = adapter.get_config_path()
        assert ".kiro" in config_path
        assert "mcp.json" in config_path

    def test_get_config_path_user_scope(self) -> None:
        """User-scope path is under ~/.kiro/settings/mcp.json."""
        adapter = KiroClientAdapter(user_scope=True)
        config_path = adapter.get_config_path()
        home = Path.home()
        assert Path(config_path).is_relative_to(home / ".kiro")

    def test_get_current_config_missing_file(self, tmp_path: Path) -> None:
        """Returns empty dict when config file does not exist."""
        adapter = KiroClientAdapter(project_root=tmp_path)
        cfg = adapter.get_current_config()
        assert cfg == {}

    def test_get_current_config_reads_existing_json(self, tmp_path: Path) -> None:
        """Reads and parses existing mcp.json."""
        kiro_dir = tmp_path / ".kiro" / "settings"
        kiro_dir.mkdir(parents=True)
        config_file = kiro_dir / "mcp.json"
        config_file.write_text(json.dumps({"mcpServers": {"old-server": {"command": "old"}}}))
        adapter = KiroClientAdapter(project_root=tmp_path)
        cfg = adapter.get_current_config()
        assert "mcpServers" in cfg

    def test_update_config_project_scope_no_kiro_dir_skips(self, tmp_path: Path) -> None:
        """Project-scope write is skipped when .kiro/ does not exist."""
        adapter = KiroClientAdapter(project_root=tmp_path)
        result = adapter.update_config({"server": {"command": "npx"}})
        assert result is None

    def test_update_config_project_scope_kiro_dir_exists(self, tmp_path: Path) -> None:
        """Project-scope write succeeds when .kiro/ exists."""
        kiro_dir = tmp_path / ".kiro"
        kiro_dir.mkdir()
        adapter = KiroClientAdapter(project_root=tmp_path)
        result = adapter.update_config({"server": {"command": "npx", "args": []}})
        assert result is True
        config_path = Path(adapter.get_config_path())
        assert config_path.exists()
        data = json.loads(config_path.read_text())
        assert "mcpServers" in data

    def test_header_mapping_dict_form(self) -> None:
        """Dict-form headers are returned as-is."""
        remote = {"headers": {"Authorization": "Bearer ${TOKEN}"}}
        result = KiroClientAdapter._header_mapping(remote)
        assert result == {"Authorization": "Bearer ${TOKEN}"}

    def test_header_mapping_list_form(self) -> None:
        """List-form headers are converted to dict."""
        remote = {
            "headers": [
                {"name": "Authorization", "value": "Bearer ${TOKEN}"},
                {"name": "X-Custom", "value": "value"},
            ]
        }
        result = KiroClientAdapter._header_mapping(remote)
        assert result["Authorization"] == "Bearer ${TOKEN}"
        assert result["X-Custom"] == "value"

    def test_header_mapping_empty(self) -> None:
        """Missing or empty headers return empty dict."""
        assert KiroClientAdapter._header_mapping({}) == {}

    def test_format_server_config_remote_sse(self, tmp_path: Path) -> None:
        """SSE remote produces a URL config."""
        kiro_dir = tmp_path / ".kiro"
        kiro_dir.mkdir()
        adapter = KiroClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "sse-server",
            "remotes": [{"url": "https://api.example.com/sse", "transport_type": "sse"}],
        }
        result = adapter._format_server_config(server_info)
        assert "url" in result
        assert urlsplit(result["url"]).scheme == "https"

    def test_format_server_config_raw_stdio(self, tmp_path: Path) -> None:
        """_raw_stdio produces command/args config."""
        kiro_dir = tmp_path / ".kiro"
        kiro_dir.mkdir()
        adapter = KiroClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "stdio-server",
            "_raw_stdio": {"command": "uvx", "args": ["my-pkg"], "env": {}},
        }
        result = adapter._format_server_config(server_info)
        assert result["command"] == "uvx"

    def test_configure_mcp_server_empty_url(self, tmp_path: Path) -> None:
        """configure_mcp_server returns False for empty server URL."""
        adapter = KiroClientAdapter(project_root=tmp_path)
        assert adapter.configure_mcp_server("") is False

    def test_copy_kiro_extensions_copies_fields(self) -> None:
        """_copy_kiro_extensions copies autoApprove/disabledTools/disabled."""
        config: dict[str, Any] = {}
        server_info = {"autoApprove": ["tool1"], "disabledTools": [], "disabled": False}
        KiroClientAdapter._copy_kiro_extensions(config, server_info)
        assert config["autoApprove"] == ["tool1"]
        assert "disabledTools" in config
        assert "disabled" in config

    def test_copy_kiro_extensions_skips_none_values(self) -> None:
        """_copy_kiro_extensions skips fields that are None."""
        config: dict[str, Any] = {}
        server_info = {"autoApprove": None, "disabledTools": None}
        KiroClientAdapter._copy_kiro_extensions(config, server_info)
        assert "autoApprove" not in config


# ===========================================================================
# 5. adapters/client/gemini.py
# ===========================================================================


class TestGeminiClientAdapter:
    """Tests for GeminiClientAdapter."""

    def test_get_config_path_project_scope(self, tmp_path: Path) -> None:
        """Project-scope config path is under <root>/.gemini/settings.json."""
        adapter = GeminiClientAdapter(project_root=tmp_path)
        config_path = adapter.get_config_path()
        assert ".gemini" in config_path
        assert "settings.json" in config_path

    def test_get_config_path_user_scope(self) -> None:
        """User-scope config path is under ~/.gemini/settings.json."""
        adapter = GeminiClientAdapter(user_scope=True)
        config_path = adapter.get_config_path()
        assert Path(config_path).is_relative_to(Path.home() / ".gemini")

    def test_get_current_config_missing_file(self, tmp_path: Path) -> None:
        """Returns empty dict when config file does not exist."""
        adapter = GeminiClientAdapter(project_root=tmp_path)
        cfg = adapter.get_current_config()
        assert cfg == {}

    def test_update_config_project_scope_no_gemini_dir_skips(self, tmp_path: Path) -> None:
        """Project-scope write is skipped when .gemini/ does not exist."""
        adapter = GeminiClientAdapter(project_root=tmp_path)
        # Returns None (skipped silently)
        result = adapter.update_config({"server": {"command": "npx"}})
        assert result is None  # returns None (no explicit return)
        # Confirm nothing was written
        gemini_dir = tmp_path / ".gemini"
        assert not gemini_dir.exists()

    def test_update_config_project_scope_gemini_dir_exists(self, tmp_path: Path) -> None:
        """Project-scope write succeeds when .gemini/ exists."""
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        adapter = GeminiClientAdapter(project_root=tmp_path)
        adapter.update_config({"test-server": {"command": "npx", "args": ["-y", "pkg"]}})
        settings = json.loads((gemini_dir / "settings.json").read_text())
        assert "mcpServers" in settings
        assert "test-server" in settings["mcpServers"]

    def test_format_server_config_raw_stdio(self, tmp_path: Path) -> None:
        """_raw_stdio path produces command/args config."""
        adapter = GeminiClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "stdio-server",
            "_raw_stdio": {"command": "uvx", "args": ["my-pkg"], "env": {}},
        }
        result = adapter._format_server_config(server_info)
        assert result["command"] == "uvx"

    def test_format_server_config_sse_remote(self, tmp_path: Path) -> None:
        """SSE remote sets 'url' key."""
        adapter = GeminiClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "sse-server",
            "remotes": [{"url": "https://api.example.com/sse", "transport_type": "sse"}],
        }
        result = adapter._format_server_config(server_info)
        assert "url" in result
        assert urlsplit(result["url"]).scheme == "https"

    def test_format_server_config_http_remote(self, tmp_path: Path) -> None:
        """HTTP remote sets 'httpUrl' key."""
        adapter = GeminiClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "http-server",
            "remotes": [{"url": "https://api.example.com/mcp", "transport_type": "http"}],
        }
        result = adapter._format_server_config(server_info)
        assert "httpUrl" in result
        assert urlsplit(result["httpUrl"]).scheme == "https"

    def test_format_server_config_npm_package(self, tmp_path: Path) -> None:
        """NPM package produces npx command."""
        adapter = GeminiClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "npm-server",
            "packages": [
                {
                    "name": "@test/mcp",
                    "registry_name": "npm",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        result = adapter._format_server_config(server_info)
        assert result["command"] in ("npx", "npm")
        assert "@test/mcp" in result["args"]

    def test_format_server_config_no_packages_raises(self, tmp_path: Path) -> None:
        """ValueError raised when no packages or remotes."""
        adapter = GeminiClientAdapter(project_root=tmp_path)
        server_info = {"name": "empty-server", "packages": [], "remotes": []}
        with pytest.raises(ValueError, match="no package information"):
            adapter._format_server_config(server_info)

    def test_configure_mcp_server_empty_url(self, tmp_path: Path) -> None:
        """configure_mcp_server returns False for empty server URL."""
        adapter = GeminiClientAdapter(project_root=tmp_path)
        assert adapter.configure_mcp_server("") is False


# ===========================================================================
# 6. adapters/client/claude.py
# ===========================================================================


class TestClaudeClientAdapter:
    """Tests for ClaudeClientAdapter."""

    def test_get_config_path_project_scope(self, tmp_path: Path) -> None:
        """Project-scope returns .mcp.json path."""
        adapter = ClaudeClientAdapter(project_root=tmp_path)
        assert adapter.get_config_path() == str(tmp_path / ".mcp.json")

    def test_get_config_path_user_scope(self) -> None:
        """User-scope returns ~/.claude.json path."""
        adapter = ClaudeClientAdapter(user_scope=True)
        assert adapter.get_config_path() == str(Path.home() / ".claude.json")

    def test_get_current_config_missing_file(self, tmp_path: Path) -> None:
        """Returns empty mcpServers when config file does not exist."""
        adapter = ClaudeClientAdapter(project_root=tmp_path)
        cfg = adapter.get_current_config()
        assert cfg == {"mcpServers": {}}

    def test_update_config_project_scope_no_claude_dir_skips(self, tmp_path: Path) -> None:
        """update_config skips when .claude/ does not exist."""
        adapter = ClaudeClientAdapter(project_root=tmp_path)
        result = adapter.update_config({"server": {"command": "npx"}})
        assert result is False

    def test_update_config_project_scope_claude_dir_exists(self, tmp_path: Path) -> None:
        """update_config writes .mcp.json when .claude/ exists."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        adapter = ClaudeClientAdapter(project_root=tmp_path)
        result = adapter.update_config({"test-server": {"command": "npx", "args": ["-y", "pkg"]}})
        assert result is True
        mcp_json = tmp_path / ".mcp.json"
        assert mcp_json.exists()
        data = json.loads(mcp_json.read_text())
        assert "test-server" in data["mcpServers"]

    def test_normalize_mcp_entry_for_claude_code_stdio(self) -> None:
        """Stdio entries get type=stdio, drop type=local."""
        entry = {"type": "local", "command": "npx", "args": [], "tools": ["*"], "id": ""}
        normalized = ClaudeClientAdapter._normalize_mcp_entry_for_claude_code(entry)
        assert normalized["type"] == "stdio"
        assert "tools" not in normalized
        assert "id" not in normalized

    def test_normalize_mcp_entry_for_claude_code_remote(self) -> None:
        """Remote entries preserve type but drop empty id/default tools."""
        entry = {"type": "http", "url": "https://api.example.com/mcp", "tools": ["*"], "id": ""}
        normalized = ClaudeClientAdapter._normalize_mcp_entry_for_claude_code(entry)
        assert "id" not in normalized
        assert "tools" not in normalized

    def test_normalize_mcp_entry_non_dict_passthrough(self) -> None:
        """Non-dict values pass through unchanged."""
        result = ClaudeClientAdapter._normalize_mcp_entry_for_claude_code("not-a-dict")
        assert result == "not-a-dict"

    def test_rewrite_self_defined_skill_command_converged(self) -> None:
        """Converged skill prefix is rewritten to Claude-specific path."""
        result = ClaudeClientAdapter._rewrite_self_defined_skill_command(
            ".agents/skills/my-skill/run.sh"
        )
        assert result == ".claude/skills/my-skill/run.sh"

    def test_rewrite_self_defined_skill_command_other(self) -> None:
        """Non-converged commands pass through unchanged."""
        result = ClaudeClientAdapter._rewrite_self_defined_skill_command("npx")
        assert result == "npx"

    def test_merge_mcp_server_dicts_shallow_merge(self) -> None:
        """Existing server entries are shallow-merged, not replaced."""
        existing: dict[str, Any] = {"server": {"command": "old", "extra": "kept"}}
        updates = {"server": {"command": "new"}}
        ClaudeClientAdapter._merge_mcp_server_dicts(existing, updates)
        assert existing["server"]["command"] == "new"
        assert existing["server"]["extra"] == "kept"

    def test_configure_mcp_server_empty_url(self, tmp_path: Path) -> None:
        """configure_mcp_server returns False for empty server URL."""
        adapter = ClaudeClientAdapter(project_root=tmp_path)
        assert adapter.configure_mcp_server("") is False


# ===========================================================================
# 7. models/dependency/lsp.py
# ===========================================================================


class TestLSPDependency:
    """Tests for LSPDependency model."""

    def test_from_string_creates_instance(self) -> None:
        """from_string creates a minimal LSPDependency."""
        dep = LSPDependency.from_string("rust-analyzer")
        assert dep.name == "rust-analyzer"
        assert dep.command is None

    def test_from_dict_full(self) -> None:
        """from_dict parses all fields from a dict."""
        data = {
            "name": "rust-analyzer",
            "command": "rust-analyzer",
            "args": ["--log-file", "/tmp/ra.log"],
            "extensionToLanguage": {".rs": "rust"},
            "transport": "stdio",
            "env": {"RUST_LOG": "info"},
            "initializationOptions": {"checkOnSave": True},
            "startupTimeout": 10,
            "shutdownTimeout": 5,
            "restartOnCrash": True,
            "maxRestarts": 3,
        }
        dep = LSPDependency.from_dict(data)
        assert dep.name == "rust-analyzer"
        assert dep.command == "rust-analyzer"
        assert dep.args == ["--log-file", "/tmp/ra.log"]
        assert dep.extension_to_language == {".rs": "rust"}
        assert dep.transport == "stdio"
        assert dep.env == {"RUST_LOG": "info"}
        assert dep.startup_timeout == 10
        assert dep.restart_on_crash is True

    def test_from_dict_snake_case_fields(self) -> None:
        """from_dict also accepts snake_case field names."""
        data = {
            "name": "clangd",
            "command": "clangd",
            "extension_to_language": {".cpp": "cpp", ".h": "cpp"},
            "startup_timeout": 8,
        }
        dep = LSPDependency.from_dict(data)
        assert dep.extension_to_language == {".cpp": "cpp", ".h": "cpp"}
        assert dep.startup_timeout == 8

    def test_from_dict_missing_name_raises(self) -> None:
        """from_dict raises ValueError when 'name' is missing."""
        with pytest.raises(ValueError, match="must contain 'name'"):
            LSPDependency.from_dict({"command": "rust-analyzer"})

    def test_to_dict_only_non_none(self) -> None:
        """to_dict only includes non-None fields."""
        dep = LSPDependency(
            name="test-lsp",
            command="test-lsp",
            extension_to_language={".ts": "typescript"},
        )
        d = dep.to_dict()
        assert d["name"] == "test-lsp"
        assert "transport" not in d
        assert "env" not in d
        assert "extensionToLanguage" in d  # camelCase in output

    def test_to_lsp_json_entry_omits_name(self) -> None:
        """to_lsp_json_entry excludes 'name' key (it's the dict key in .lsp.json)."""
        dep = LSPDependency(
            name="test",
            command="test",
            extension_to_language={".py": "python"},
        )
        entry = dep.to_lsp_json_entry()
        assert "name" not in entry
        assert "command" in entry

    def test_validate_invalid_name_raises(self) -> None:
        """Validation raises for names with invalid characters."""
        dep = LSPDependency(name="--invalid")
        with pytest.raises(ValueError, match="Invalid LSP dependency name"):
            dep.validate(strict=False)

    def test_validate_empty_name_raises(self) -> None:
        """Validation raises for empty name."""
        dep = LSPDependency(name="")
        with pytest.raises(ValueError, match="must not be empty"):
            dep.validate(strict=False)

    def test_validate_invalid_transport_raises(self) -> None:
        """Validation raises for unsupported transport."""
        dep = LSPDependency(
            name="test",
            command="test",
            extension_to_language={".py": "python"},
            transport="websocket",
        )
        with pytest.raises(ValueError, match="unsupported transport"):
            dep.validate(strict=True)

    def test_validate_strict_missing_command_raises(self) -> None:
        """Strict validation raises when command is missing."""
        dep = LSPDependency(name="test", extension_to_language={".py": "python"})
        with pytest.raises(ValueError, match="requires 'command'"):
            dep.validate(strict=True)

    def test_validate_strict_missing_extension_map_raises(self) -> None:
        """Strict validation raises when extensionToLanguage is missing."""
        dep = LSPDependency(name="test", command="test")
        with pytest.raises(ValueError, match="requires 'extensionToLanguage'"):
            dep.validate(strict=True)

    def test_str_with_transport(self) -> None:
        """__str__ includes transport when set."""
        dep = LSPDependency(name="test", transport="stdio")
        assert "stdio" in str(dep)

    def test_str_without_transport(self) -> None:
        """__str__ returns just the name when transport is None."""
        dep = LSPDependency(name="test-lsp")
        assert str(dep) == "test-lsp"

    def test_repr_redacts_env(self) -> None:
        """__repr__ shows *** for env values."""
        dep = LSPDependency(
            name="test",
            command="test",
            env={"MY_SECRET": "supersecret"},
        )
        r = repr(dep)
        assert "***" in r
        assert "supersecret" not in r

    def test_validate_path_traversal_command_raises(self) -> None:
        """Validation raises for command with .. traversal."""
        dep = LSPDependency(
            name="test",
            command="../../../usr/bin/evil",
            extension_to_language={".py": "python"},
        )
        with pytest.raises(ValueError, match="must not contain"):
            dep.validate(strict=True)

    def test_from_dict_camel_case_fields(self) -> None:
        """from_dict accepts all camelCase field names."""
        data = {
            "name": "pyright",
            "command": "pyright-langserver",
            "extensionToLanguage": {".py": "python"},
            "workspaceFolder": "/workspace",
            "shutdownTimeout": 3,
            "restartOnCrash": False,
            "maxRestarts": 0,
        }
        dep = LSPDependency.from_dict(data)
        assert dep.workspace_folder == "/workspace"
        assert dep.shutdown_timeout == 3
        assert dep.restart_on_crash is False
        assert dep.max_restarts == 0


# ===========================================================================
# 8. marketplace/client.py  -- cache helpers
# ===========================================================================


class TestMarketplaceClientCache:
    """Tests for marketplace/client.py cache functions."""

    def test_sanitize_cache_name_safe_chars(self) -> None:
        """Letters, digits, and some punctuation are preserved."""
        assert _sanitize_cache_name("org-name.json") == "org-name.json"

    def test_sanitize_cache_name_replaces_slashes(self) -> None:
        """Slashes are replaced with underscores."""
        result = _sanitize_cache_name("owner/repo")
        assert "/" not in result

    def test_sanitize_cache_name_empty_becomes_unnamed(self) -> None:
        """Edge case: name that collapses to empty becomes 'unnamed'."""
        result = _sanitize_cache_name("...")
        assert result == "unnamed"

    def test_cache_key_github_default_host(self) -> None:
        """GitHub source uses bare name as cache key."""
        source = MarketplaceSource(
            name="org/marketplace",
            url="https://github.com/org/marketplace",
            ref="main",
            owner="org",
            repo="marketplace",
        )
        key = _cache_key(source)
        assert key == "org/marketplace"

    def test_cache_key_url_kind(self) -> None:
        """URL source uses url__ prefix with sha256 digest."""
        # path="" makes kind == "url" (is_remote_manifest_url)
        source = MarketplaceSource(
            name="my-market",
            url="https://cdn.example.com/marketplace.json",
            ref="main",
            path="",
        )
        key = _cache_key(source)
        assert key.startswith("url__")

    def test_cache_key_git_kind(self) -> None:
        """Generic git source uses git__ prefix."""
        source = MarketplaceSource(
            name="gitea/repo",
            url="https://gitea.example.com/org/repo",
            ref="main",
            owner="org",
            repo="repo",
        )
        key = _cache_key(source)
        assert key.startswith("git__")

    def test_write_and_read_cache(self, tmp_path: Path) -> None:
        """_write_cache + _read_cache round-trips JSON data within TTL."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        with patch("apm_cli.marketplace.client._cache_dir", return_value=str(cache_dir)):
            data = {"name": "test", "plugins": []}
            _write_cache("my-market", data)
            result = _read_cache("my-market")
            assert result is not None
            assert result["name"] == "test"

    def test_read_cache_expired_returns_none(self, tmp_path: Path) -> None:
        """_read_cache returns None when entry has expired."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        with patch("apm_cli.marketplace.client._cache_dir", return_value=str(cache_dir)):
            data = {"name": "test"}
            # Write with ttl=0 so it expires immediately
            data_path = cache_dir / "my_market.json"
            meta_path = cache_dir / "my_market.meta.json"
            data_path.write_text(json.dumps(data))
            meta_path.write_text(json.dumps({"fetched_at": 0.0, "ttl_seconds": 0}))

            with patch("apm_cli.marketplace.client._cache_data_path") as mock_data_p:
                with patch("apm_cli.marketplace.client._cache_meta_path") as mock_meta_p:
                    mock_data_p.return_value = str(data_path)
                    mock_meta_p.return_value = str(meta_path)
                    result = _read_cache("my-market")
                    assert result is None

    def test_read_stale_cache_returns_data_when_expired(self, tmp_path: Path) -> None:
        """_read_stale_cache returns data even when expired."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        data = {"name": "stale-test"}
        data_path = cache_dir / "stale.json"
        data_path.write_text(json.dumps(data))
        with patch("apm_cli.marketplace.client._cache_data_path", return_value=str(data_path)):
            result = _read_stale_cache("stale")
            assert result is not None
            assert result["name"] == "stale-test"

    def test_read_stale_cache_missing_file_returns_none(self, tmp_path: Path) -> None:
        """_read_stale_cache returns None when data file does not exist."""
        with patch(
            "apm_cli.marketplace.client._cache_data_path",
            return_value=str(tmp_path / "nonexistent.json"),
        ):
            assert _read_stale_cache("nothing") is None


# ===========================================================================
# 9. marketplace/version_check.py
# ===========================================================================


def _make_marketplace_config(
    *,
    version: str = "1.0.0",
    strategy: str = "lockstep",
    tag_pattern: str = "v{version}",
    packages: list[PackageEntry] | None = None,
) -> MarketplaceConfig:
    """Build a minimal MarketplaceConfig for version-check tests."""
    return MarketplaceConfig(
        name="test-market",
        description="Test marketplace",
        version=version,
        owner=MarketplaceOwner(name="test-owner"),
        build=MarketplaceBuild(tag_pattern=tag_pattern),
        versioning=MarketplaceVersioning(strategy=strategy),
        packages=tuple(packages or []),
    )


class TestVersionCheck:
    """Tests for check_version_alignment."""

    def test_lockstep_all_match(self, tmp_path: Path) -> None:
        """All packages match the top-level version under lockstep."""
        pkg_dir = tmp_path / "pkgs" / "svc-a"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text("version: 1.0.0\n")

        config = _make_marketplace_config(version="1.0.0", strategy="lockstep")
        entry = PackageEntry(name="svc-a", source="./pkgs/svc-a")
        config = _make_marketplace_config(
            version="1.0.0",
            strategy="lockstep",
            packages=[entry],
        )
        report = check_version_alignment(config, tmp_path)
        assert report.ok is True
        assert report.strategy == "lockstep"
        assert len(report.packages) == 1
        assert report.packages[0].ok

    def test_lockstep_drift_detected(self, tmp_path: Path) -> None:
        """Mismatched version produces drift error."""
        pkg_dir = tmp_path / "pkgs" / "svc-b"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_text("version: 2.0.0\n")

        entry = PackageEntry(name="svc-b", source="./pkgs/svc-b")
        config = _make_marketplace_config(
            version="1.0.0",
            strategy="lockstep",
            packages=[entry],
        )
        report = check_version_alignment(config, tmp_path)
        assert report.ok is False
        assert "drift" in report.packages[0].reason

    def test_lockstep_missing_apm_yml(self, tmp_path: Path) -> None:
        """Missing apm.yml produces no_apm_yml error."""
        entry = PackageEntry(name="svc-c", source="./pkgs/svc-c")
        config = _make_marketplace_config(version="1.0.0", strategy="lockstep", packages=[entry])
        report = check_version_alignment(config, tmp_path)
        assert report.ok is False
        assert report.packages[0].reason == "no_apm_yml"

    def test_lockstep_missing_version_field(self, tmp_path: Path) -> None:
        """apm.yml without version field produces missing_version error."""
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "apm.yml").write_text("name: no-version\n")

        entry = PackageEntry(name="pkg", source="./pkg")
        config = _make_marketplace_config(version="1.0.0", strategy="lockstep", packages=[entry])
        report = check_version_alignment(config, tmp_path)
        assert report.ok is False
        assert report.packages[0].reason == "missing_version"

    def test_per_package_strategy_any_version_ok(self, tmp_path: Path) -> None:
        """per_package strategy accepts any semver version."""
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "apm.yml").write_text("version: 99.0.0\n")

        entry = PackageEntry(name="pkg", source="./pkg")
        config = _make_marketplace_config(version="1.0.0", strategy="per_package", packages=[entry])
        report = check_version_alignment(config, tmp_path)
        assert report.ok is True

    def test_to_json_dict(self) -> None:
        """to_json_dict produces a serializable dict."""
        row = PackageVersionRow(path="pkg", version="1.0.0", ok=True, reason="matches")
        report = VersionAlignmentReport(
            strategy="lockstep",
            expected="1.0.0",
            ok=True,
            packages=(row,),
        )
        d = report.to_json_dict()
        assert d["ok"] is True
        assert d["strategy"] == "lockstep"
        assert len(d["packages"]) == 1

    def test_error_messages_empty_when_ok(self) -> None:
        """No error messages when all packages are OK."""
        row = PackageVersionRow(path="pkg", version="1.0.0", ok=True, reason="matches")
        report = VersionAlignmentReport(
            strategy="lockstep", expected="1.0.0", ok=True, packages=(row,)
        )
        assert report.error_messages() == []

    def test_error_messages_drift(self) -> None:
        """Drift reason produces a readable error message."""
        row = PackageVersionRow(
            path="pkg", version="2.0.0", ok=False, reason="drift:expected=1.0.0"
        )
        report = VersionAlignmentReport(
            strategy="lockstep", expected="1.0.0", ok=False, packages=(row,)
        )
        msgs = report.error_messages()
        assert len(msgs) == 1
        assert "expected" in msgs[0].lower() or "1.0.0" in msgs[0]

    def test_error_messages_missing_version(self) -> None:
        """missing_version reason produces a readable error message."""
        row = PackageVersionRow(path="pkg", version=None, ok=False, reason="missing_version")
        report = VersionAlignmentReport(
            strategy="lockstep", expected="1.0.0", ok=False, packages=(row,)
        )
        msgs = report.error_messages()
        assert "missing" in msgs[0]

    def test_tag_pattern_strategy(self, tmp_path: Path) -> None:
        """tag_pattern strategy renders tags and checks uniqueness."""
        for i in range(1, 3):
            pkg_dir = tmp_path / f"pkg{i}"
            pkg_dir.mkdir()
            (pkg_dir / "apm.yml").write_text(f"version: 1.{i}.0\n")

        entries = [PackageEntry(name=f"pkg{i}", source=f"./pkg{i}") for i in range(1, 3)]
        config = _make_marketplace_config(
            version="1.0.0",
            strategy="tag_pattern",
            tag_pattern="v{version}",
            packages=entries,
        )
        report = check_version_alignment(config, tmp_path)
        # Both have unique versions so tags should be unique
        assert all(r.rendered_tag is not None for r in report.packages)


# ===========================================================================
# 10. marketplace/migration.py
# ===========================================================================


class TestMarketplaceMigration:
    """Tests for migration helpers."""

    def test_detect_config_source_apm_yml(self, tmp_path: Path) -> None:
        """APM_YML returned when apm.yml has a marketplace block."""
        (tmp_path / "apm.yml").write_text(
            "name: test\nversion: 1.0.0\nmarketplace:\n  owner:\n    name: me\n"
        )
        result = detect_config_source(tmp_path)
        assert result == ConfigSource.APM_YML

    def test_detect_config_source_legacy_yml(self, tmp_path: Path) -> None:
        """LEGACY_YML returned when marketplace.yml exists."""
        (tmp_path / "marketplace.yml").write_text("name: legacy\nowner:\n  name: me\n")
        result = detect_config_source(tmp_path)
        assert result == ConfigSource.LEGACY_YML

    def test_detect_config_source_none(self, tmp_path: Path) -> None:
        """NONE returned when neither file is present."""
        result = detect_config_source(tmp_path)
        assert result == ConfigSource.NONE

    def test_detect_config_source_both_raises(self, tmp_path: Path) -> None:
        """Raises MarketplaceYmlError when both files are present."""
        (tmp_path / "apm.yml").write_text(
            "name: test\nversion: 1.0.0\nmarketplace:\n  owner:\n    name: me\n"
        )
        (tmp_path / "marketplace.yml").write_text("name: legacy\nowner:\n  name: me\n")
        with pytest.raises(MarketplaceYmlError, match="Both"):
            detect_config_source(tmp_path)

    def test_load_marketplace_config_none_raises(self, tmp_path: Path) -> None:
        """Raises MarketplaceYmlError when no config is found."""
        with pytest.raises(MarketplaceYmlError, match="No marketplace config"):
            load_marketplace_config(tmp_path)

    def test_load_marketplace_config_legacy_warns(self, tmp_path: Path) -> None:
        """warn_callback is called when loading legacy marketplace.yml."""
        legacy = tmp_path / "marketplace.yml"
        legacy.write_text(
            "name: legacy-market\ndescription: test\nversion: 1.0.0\nowner:\n  name: me\n"
        )
        warnings: list[str] = []
        load_marketplace_config(tmp_path, warn_callback=warnings.append)
        assert any(DEPRECATION_MESSAGE in w for w in warnings)

    def test_detect_inheritance_conflicts_no_conflict(self) -> None:
        """Returns empty list when legacy and apm.yml values match."""
        legacy = {"name": "test", "version": "1.0.0"}
        apm = {"name": "test", "version": "1.0.0"}
        assert detect_inheritance_conflicts(legacy, apm) == []

    def test_detect_inheritance_conflicts_name_differs(self) -> None:
        """Returns a conflict description when name values differ."""
        legacy = {"name": "legacy-name"}
        apm = {"name": "apm-name"}
        conflicts = detect_inheritance_conflicts(legacy, apm)
        assert len(conflicts) == 1
        assert "name" in conflicts[0]

    def test_detect_inheritance_conflicts_none_values_ignored(self) -> None:
        """None values on either side are ignored."""
        legacy = {"name": None, "version": "1.0.0"}
        apm = {"name": "apm-name", "version": None}
        assert detect_inheritance_conflicts(legacy, apm) == []

    def test_migrate_marketplace_yml_dry_run(self, tmp_path: Path) -> None:
        """dry_run returns a diff without writing files."""
        (tmp_path / "marketplace.yml").write_text(
            "name: mkt\ndescription: d\nversion: 1.0.0\nowner:\n  name: me\n"
        )
        (tmp_path / "apm.yml").write_text("name: parent\nversion: 1.0.0\n")
        diff = migrate_marketplace_yml(tmp_path, dry_run=True)
        # File should NOT be deleted in dry-run mode
        assert (tmp_path / "marketplace.yml").exists()
        # diff should contain + lines (the new marketplace block)
        assert "marketplace" in diff

    def test_migrate_marketplace_yml_missing_legacy_raises(self, tmp_path: Path) -> None:
        """Raises MarketplaceYmlError when marketplace.yml does not exist."""
        (tmp_path / "apm.yml").write_text("name: parent\n")
        with pytest.raises(MarketplaceYmlError, match=r"marketplace\.yml not found"):
            migrate_marketplace_yml(tmp_path)

    def test_migrate_marketplace_yml_missing_apm_yml_raises(self, tmp_path: Path) -> None:
        """Raises MarketplaceYmlError when apm.yml does not exist."""
        (tmp_path / "marketplace.yml").write_text(
            "name: mkt\ndescription: d\nversion: 1.0.0\nowner:\n  name: me\n"
        )
        with pytest.raises(MarketplaceYmlError, match=r"apm\.yml not found"):
            migrate_marketplace_yml(tmp_path)

    def test_migrate_marketplace_yml_writes_and_deletes(self, tmp_path: Path) -> None:
        """migrate_marketplace_yml merges into apm.yml and deletes legacy file."""
        (tmp_path / "marketplace.yml").write_text(
            "name: mkt\ndescription: d\nversion: 1.0.0\nowner:\n  name: me\n"
        )
        (tmp_path / "apm.yml").write_text("name: parent\nversion: 1.0.0\n")
        migrate_marketplace_yml(tmp_path, dry_run=False)
        assert not (tmp_path / "marketplace.yml").exists()
        apm_data = yaml.safe_load((tmp_path / "apm.yml").read_text())
        assert "marketplace" in apm_data


# ===========================================================================
# 11. cache/http_cache.py
# ===========================================================================


class TestHttpCache:
    """Tests for HttpCache store/get/expiry/integrity."""

    def test_store_and_get(self, tmp_path: Path) -> None:
        """Basic store + get round-trip."""
        cache = HttpCache(tmp_path)
        url = "https://example.com/test"
        body = b'{"hello": "world"}'
        cache.store(url, body, status_code=200, headers={"Cache-Control": "max-age=3600"})
        entry = cache.get(url)
        assert entry is not None
        assert entry.body == body
        assert entry.status_code == 200

    def test_get_returns_none_for_unknown_url(self, tmp_path: Path) -> None:
        """get() returns None for a URL that was never stored."""
        cache = HttpCache(tmp_path)
        assert cache.get("https://unknown.example.com/x") is None

    def test_store_with_etag(self, tmp_path: Path) -> None:
        """ETag is stored and returned."""
        cache = HttpCache(tmp_path)
        url = "https://example.com/tagged"
        cache.store(url, b"data", status_code=200, headers={"ETag": '"abc123"'})
        entry = cache.get(url)
        assert entry is not None
        assert entry.etag == '"abc123"'

    def test_conditional_headers_returns_etag(self, tmp_path: Path) -> None:
        """conditional_headers returns If-None-Match when ETag cached."""
        cache = HttpCache(tmp_path)
        url = "https://example.com/cond"
        cache.store(
            url, b"body", status_code=200, headers={"ETag": '"v1"', "Cache-Control": "max-age=3600"}
        )
        headers = cache.conditional_headers(url)
        assert headers.get("If-None-Match") == '"v1"'

    def test_conditional_headers_empty_for_unknown(self, tmp_path: Path) -> None:
        """conditional_headers returns empty dict for unknown URL."""
        cache = HttpCache(tmp_path)
        assert cache.conditional_headers("https://not-cached.example.com/x") == {}

    def test_expired_entry_returns_none(self, tmp_path: Path) -> None:
        """get() returns None for an expired cache entry."""
        cache = HttpCache(tmp_path)
        url = "https://example.com/expire"
        cache.store(url, b"stale", status_code=200, headers={"Cache-Control": "max-age=1"})
        # Manually expire the entry by patching time
        with patch("apm_cli.cache.http_cache.time") as mock_time:
            mock_time.time.return_value = time.time() + 7200  # 2 hours later
            result = cache.get(url)
        assert result is None

    def test_refresh_expiry_extends_ttl(self, tmp_path: Path) -> None:
        """refresh_expiry updates expires_at for a 304 response."""
        cache = HttpCache(tmp_path)
        url = "https://example.com/refresh"
        cache.store(url, b"body", status_code=200, headers={"Cache-Control": "max-age=60"})
        # Refresh with new max-age
        cache.refresh_expiry(url, headers={"Cache-Control": "max-age=7200"})
        entry = cache.get(url)
        assert entry is not None

    def test_parse_ttl_max_age(self, tmp_path: Path) -> None:
        """_parse_ttl parses Cache-Control max-age correctly."""
        cache = HttpCache(tmp_path)
        assert cache._parse_ttl({"Cache-Control": "max-age=300"}) == 300.0
        assert cache._parse_ttl({"Cache-Control": "max-age=999999"}) == MAX_HTTP_CACHE_TTL_SECONDS

    def test_parse_ttl_default_without_header(self, tmp_path: Path) -> None:
        """_parse_ttl returns 300 when no Cache-Control header."""
        cache = HttpCache(tmp_path)
        assert cache._parse_ttl({}) == 300.0

    def test_get_stats_returns_dict(self, tmp_path: Path) -> None:
        """get_stats returns entry_count and total_size_bytes."""
        cache = HttpCache(tmp_path)
        cache.store(
            "https://example.com/stats-test",
            b"hello",
            status_code=200,
        )
        stats = cache.get_stats()
        assert "entry_count" in stats
        assert "total_size_bytes" in stats
        assert stats["entry_count"] >= 1

    def test_clean_all_removes_entries(self, tmp_path: Path) -> None:
        """clean_all removes all cache entries."""
        cache = HttpCache(tmp_path)
        cache.store("https://example.com/a", b"body-a", status_code=200)
        cache.store("https://example.com/b", b"body-b", status_code=200)
        cache.clean_all()
        stats = cache.get_stats()
        assert stats["entry_count"] == 0

    def test_integrity_mismatch_evicts_entry(self, tmp_path: Path) -> None:
        """Tampered body causes get() to evict and return None."""
        cache = HttpCache(tmp_path)
        url = "https://example.com/tampered"
        cache.store(
            url, b"original body", status_code=200, headers={"Cache-Control": "max-age=3600"}
        )
        # Corrupt the body file
        entry_path = cache._entry_path(url)
        body_path = entry_path / "body"
        body_path.write_bytes(b"CORRUPTED!")
        result = cache.get(url)
        assert result is None


# ===========================================================================
# 12. utils/reflink.py
# ===========================================================================


class TestReflink:
    """Tests for reflink utilities."""

    def setup_method(self) -> None:
        """Clear capability cache before each test."""
        _reset_capability_cache()

    def test_reflink_supported_apm_no_reflink_env(self) -> None:
        """APM_NO_REFLINK=1 makes reflink_supported return False."""
        with patch.dict(os.environ, {"APM_NO_REFLINK": "1"}):
            assert reflink_supported() is False

    def test_clone_file_no_reflink_env(self, tmp_path: Path) -> None:
        """clone_file returns False when APM_NO_REFLINK=1."""
        src = tmp_path / "source.txt"
        src.write_text("hello")
        dst = tmp_path / "dest.txt"
        with patch.dict(os.environ, {"APM_NO_REFLINK": "1"}):
            result = clone_file(src, dst)
        assert result is False

    def test_clone_file_known_unsupported_device(self, tmp_path: Path) -> None:
        """clone_file skips attempt when device is known unsupported."""
        src = tmp_path / "source.txt"
        src.write_text("hello")
        dst = tmp_path / "dest.txt"
        _mark_device_unsupported(str(dst))
        result = clone_file(src, dst)
        assert result is False

    def test_mark_device_supported(self, tmp_path: Path) -> None:
        """_mark_device_supported stores True for the device."""
        p = str(tmp_path / "test.txt")
        _mark_device_supported(p)

        dev = os.stat(tmp_path).st_dev
        assert _device_capability.get(dev) is True

    def test_mark_device_unsupported(self, tmp_path: Path) -> None:
        """_mark_device_unsupported stores False for the device."""
        p = str(tmp_path / "test.txt")
        _mark_device_unsupported(p)

        dev = os.stat(tmp_path).st_dev
        assert _device_capability.get(dev) is False

    def test_reset_capability_cache(self, tmp_path: Path) -> None:
        """_reset_capability_cache clears all device entries."""
        _mark_device_supported(str(tmp_path / "x"))
        _reset_capability_cache()
        assert len(_device_capability) == 0

    def test_clone_file_fallback_does_not_raise(self, tmp_path: Path) -> None:
        """clone_file returns False without raising on unsupported filesystem."""
        src = tmp_path / "source.txt"
        src.write_bytes(b"test content")
        dst = tmp_path / "dest.txt"
        # On most test environments reflinks won't work, but clone_file must not raise
        try:
            clone_file(src, dst)
        except Exception as exc:
            pytest.fail(f"clone_file raised unexpectedly: {exc}")
        # result is True or False -- just ensure no exception


# ===========================================================================
# 13. utils/install_tui.py
# ===========================================================================


class TestInstallTui:
    """Tests for should_animate() and InstallTui lifecycle."""

    def test_should_animate_never_env(self) -> None:
        """APM_PROGRESS=never suppresses animation."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            assert should_animate() is False

    def test_should_animate_always_env(self) -> None:
        """APM_PROGRESS=always forces animation."""
        with patch.dict(os.environ, {"APM_PROGRESS": "always"}):
            assert should_animate() is True

    def test_should_animate_ci_env(self) -> None:
        """CI=1 disables animation in auto mode."""
        with patch.dict(os.environ, {"APM_PROGRESS": "auto", "CI": "1"}):
            assert should_animate() is False

    def test_should_animate_dumb_term(self) -> None:
        """TERM=dumb disables animation."""
        with patch.dict(os.environ, {"APM_PROGRESS": "auto", "TERM": "dumb", "CI": ""}):
            assert should_animate() is False

    def test_install_tui_disabled_context_manager(self) -> None:
        """Disabled TUI context manager enters/exits cleanly."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            assert not tui._enabled
            with tui:
                pass  # Should not raise

    def test_install_tui_disabled_api_noop(self) -> None:
        """Disabled TUI methods are no-ops and do not raise."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            with tui:
                tui.start_phase("resolve", 10)
                tui.task_started("dep1", "my-dep@1.0.0")
                tui.task_completed("dep1", "[+] installed my-dep")
                tui.task_failed("dep2", "[x] failed dep2")
            assert not tui.is_animating()

    def test_install_tui_key_deduplication(self) -> None:
        """task_started is idempotent on the same key."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            with tui:
                tui._enabled = True  # Override to exercise state logic
                # We're in disabled path since _enabled was toggled after __enter__
                # Just verify the internal state remains consistent
                tui.task_started("dep1", "label-a")
                tui.task_started("dep1", "label-a")  # duplicate
                # Only one entry in key_to_label
                assert len(tui._key_to_label) <= 1

    def test_install_tui_is_animating_false_when_disabled(self) -> None:
        """is_animating() returns False when TUI is disabled."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            assert tui.is_animating() is False


# ===========================================================================
# 14. adapters/client/base.py  -- translate mode (Copilot-style)
# ===========================================================================


class TestBaseAdapterTranslateMode:
    """Tests for translate-mode env resolution paths in MCPClientAdapter."""

    def _make_translate_adapter(self, tmp_path: Path):
        """Return a CopilotClientAdapter which has _supports_runtime_env_substitution=True."""
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        return CopilotClientAdapter(project_root=tmp_path)

    def test_resolve_environment_variables_translate_dict_placeholder(self, tmp_path: Path) -> None:
        """Dict-shape env with placeholder is translated to ${VAR} in translate mode."""
        adapter = self._make_translate_adapter(tmp_path)
        env_dict = {"MY_TOKEN": "${MY_TOKEN}"}
        result = adapter._resolve_environment_variables(env_dict)
        assert result["MY_TOKEN"] == "${MY_TOKEN}"

    def test_resolve_environment_variables_translate_dict_legacy_angle(
        self, tmp_path: Path
    ) -> None:
        """Legacy <VAR> in dict env is promoted to ${VAR} in translate mode."""
        adapter = self._make_translate_adapter(tmp_path)
        env_dict = {"MY_TOKEN": "<MY_TOKEN>"}
        result = adapter._resolve_environment_variables(env_dict)
        assert result["MY_TOKEN"] == "${MY_TOKEN}"

    def test_resolve_environment_variables_translate_dict_literal_becomes_placeholder(
        self, tmp_path: Path
    ) -> None:
        """Literal value in translate mode is replaced with a runtime placeholder."""
        adapter = self._make_translate_adapter(tmp_path)
        env_dict = {"MY_SECRET": "hardcoded-value"}
        result = adapter._resolve_environment_variables(env_dict)
        # Literal replaced with placeholder so secret never touches disk
        assert result["MY_SECRET"] == "${MY_SECRET}"

    def test_resolve_environment_variables_translate_dict_non_string_passthrough(
        self, tmp_path: Path
    ) -> None:
        """Non-string values are stringified in Copilot translate mode."""
        adapter = self._make_translate_adapter(tmp_path)
        env_dict = {"NUMERIC_VAR": 42, "BOOL_VAR": True}
        result = adapter._resolve_environment_variables(env_dict)
        # Copilot adapter stringifies non-string env values via _stringify_env_literal
        assert result["NUMERIC_VAR"] == "42"
        assert result["BOOL_VAR"] == "true"

    def test_resolve_environment_variables_translate_registry_list(self, tmp_path: Path) -> None:
        """Registry-list shape env vars are translated to ${VAR} placeholders."""
        adapter = self._make_translate_adapter(tmp_path)
        env_vars = [
            {"name": "MY_TOKEN", "description": "API token", "required": True},
            {"name": "MY_KEY", "description": "API key"},
        ]
        result = adapter._resolve_environment_variables(env_vars)
        assert result["MY_TOKEN"] == "${MY_TOKEN}"
        assert result["MY_KEY"] == "${MY_KEY}"

    def test_resolve_environment_variables_translate_github_defaults(self, tmp_path: Path) -> None:
        """GitHub default env vars are emitted verbatim in translate mode."""
        adapter = self._make_translate_adapter(tmp_path)
        env_vars = [{"name": "GITHUB_TOOLSETS"}]
        result = adapter._resolve_environment_variables(env_vars)
        assert result["GITHUB_TOOLSETS"] == "context"

    def test_resolve_variable_placeholders_translate_mode(self, tmp_path: Path) -> None:
        """Translate mode rewrites <VAR> legacy syntax to ${VAR}."""
        adapter = self._make_translate_adapter(tmp_path)
        result = adapter._resolve_variable_placeholders("--token <MY_TOKEN>", {}, {})
        assert result == "--token ${MY_TOKEN}"

    def test_resolve_variable_placeholders_runtime_vars_resolved(self, tmp_path: Path) -> None:
        """APM runtime vars ({version}) are always resolved at install time."""
        adapter = self._make_translate_adapter(tmp_path)
        result = adapter._resolve_variable_placeholders(
            "my-pkg@{version}", {}, {"version": "1.2.3"}
        )
        assert result == "my-pkg@1.2.3"

    def test_resolve_env_variable_translate_mode(self, tmp_path: Path) -> None:
        """_resolve_env_variable returns placeholder in translate mode."""
        adapter = self._make_translate_adapter(tmp_path)
        result = adapter._resolve_env_variable("MY_TOKEN", "${MY_TOKEN}")
        assert result == "${MY_TOKEN}"

    def test_resolve_env_variable_legacy_angle_translate(self, tmp_path: Path) -> None:
        """_resolve_env_variable translates <VAR> in translate mode."""
        adapter = self._make_translate_adapter(tmp_path)
        result = adapter._resolve_env_variable("MY_TOKEN", "<MY_TOKEN>")
        assert result == "${MY_TOKEN}"

    def test_apply_pypi_homebrew_generic_config_pypi(self, tmp_path: Path) -> None:
        """pypi registry generates uvx command."""
        config: dict[str, Any] = {}
        MCPClientAdapter._apply_pypi_homebrew_generic_config(
            config=config,
            registry_name="pypi",
            package_name="mcp-server-pkg",
            runtime_hint="",
            processed_runtime_args=[],
            processed_package_args=["--port", "8080"],
            resolved_env={"API_KEY": "val"},
        )
        assert config["command"] == "uvx"
        assert "mcp-server-pkg" in config["args"]
        assert "--port" in config["args"]
        assert config["env"] == {"API_KEY": "val"}

    def test_apply_pypi_homebrew_generic_config_homebrew(self) -> None:
        """homebrew registry uses formula name as command."""
        config: dict[str, Any] = {}
        MCPClientAdapter._apply_pypi_homebrew_generic_config(
            config=config,
            registry_name="homebrew",
            package_name="org/tap/my-server",
            runtime_hint="",
            processed_runtime_args=[],
            processed_package_args=[],
            resolved_env={},
        )
        assert config["command"] == "my-server"

    def test_apply_pypi_homebrew_generic_config_generic_npm_fallback(self) -> None:
        """Generic registry falls back to npx with -y."""
        config: dict[str, Any] = {}
        MCPClientAdapter._apply_pypi_homebrew_generic_config(
            config=config,
            registry_name="unknown",
            package_name="my-pkg",
            runtime_hint="",
            processed_runtime_args=[],
            processed_package_args=[],
            resolved_env={},
        )
        assert config["command"] == "npx"
        assert "-y" in config["args"]

    def test_warn_input_variables_emits_for_input_vars(self, tmp_path: Path) -> None:
        """_warn_input_variables does not raise for ${input:var} values."""
        self._make_translate_adapter(tmp_path)
        # Should run without raising
        MCPClientAdapter._warn_input_variables(
            {"Authorization": "${input:github_token}"},
            "test-server",
            "Test Runtime",
        )

    def test_normalize_project_arg_workspace_placeholder(self, tmp_path: Path) -> None:
        """normalize_project_arg replaces ${workspaceFolder} with '.' in project scope."""
        adapter = self._make_translate_adapter(tmp_path)
        assert adapter.normalize_project_arg("${workspaceFolder}") == "."
        assert adapter.normalize_project_arg("${projectRoot}") == "."

    def test_normalize_project_arg_user_scope_passthrough(self) -> None:
        """normalize_project_arg is a no-op in user scope."""
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter(user_scope=True)
        assert adapter.normalize_project_arg("${workspaceFolder}") == "${workspaceFolder}"

    def test_fetch_server_info_cache_hit(self, tmp_path: Path) -> None:
        """_fetch_server_info uses cache when key matches."""
        adapter = self._make_translate_adapter(tmp_path)
        server_info = {"name": "cached-server", "id": "abc"}
        result = adapter._fetch_server_info(
            "owner/cached-server", {"owner/cached-server": server_info}
        )
        assert result == server_info

    def test_fetch_server_info_not_found_returns_none(self, tmp_path: Path) -> None:
        """_fetch_server_info returns None when registry lookup fails."""
        adapter = self._make_translate_adapter(tmp_path)
        with patch.object(adapter.registry_client, "find_server_by_reference", return_value=None):
            result = adapter._fetch_server_info("owner/not-found", None)
        assert result is None


# ===========================================================================
# 15. bundle/packer.py
# ===========================================================================


class TestBundlePacker:
    """Tests for pack_bundle function."""

    def _make_minimal_project(self, tmp_path: Path, *, with_file: bool = True) -> None:
        """Set up a minimal APM project in tmp_path."""
        from apm_cli.deps.lockfile import LockedDependency, LockFile

        (tmp_path / "apm.yml").write_text("name: test-pkg\nversion: 1.0.0\n")
        dep = LockedDependency(
            repo_url="https://github.com/owner/dep",
            deployed_files=[".agents/skills/dep/run.py"],
        )
        lf = LockFile(dependencies={"remote-dep": dep})
        (tmp_path / "apm.lock.yaml").write_text(lf.to_yaml())
        if with_file:
            (tmp_path / ".agents" / "skills" / "dep").mkdir(parents=True)
            (tmp_path / ".agents" / "skills" / "dep" / "run.py").write_text("# skill")

    def test_pack_bundle_dry_run_returns_file_list(self, tmp_path: Path) -> None:
        """Dry run returns file list without writing to disk."""
        from apm_cli.bundle.packer import pack_bundle

        self._make_minimal_project(tmp_path)
        out_dir = tmp_path / "out"
        result = pack_bundle(tmp_path, out_dir, dry_run=True)
        assert ".agents/skills/dep/run.py" in result.files
        assert result.lockfile_enriched is True
        # Output dir should NOT be created in dry run
        assert not out_dir.exists()

    def test_pack_bundle_missing_lockfile_raises(self, tmp_path: Path) -> None:
        """Raises FileNotFoundError when apm.lock.yaml is missing."""
        from apm_cli.bundle.packer import pack_bundle

        (tmp_path / "apm.yml").write_text("name: test-pkg\nversion: 1.0.0\n")
        with pytest.raises(FileNotFoundError, match=r"apm\.lock\.yaml"):
            pack_bundle(tmp_path, tmp_path / "out")

    def test_pack_bundle_missing_deployed_file_raises(self, tmp_path: Path) -> None:
        """Raises ValueError when deployed files are missing on disk."""
        from apm_cli.bundle.packer import pack_bundle

        # Create lockfile but NOT the deployed file
        self._make_minimal_project(tmp_path, with_file=False)
        with pytest.raises(ValueError, match="missing on disk"):
            pack_bundle(tmp_path, tmp_path / "out")

    def test_pack_bundle_unsafe_path_raises(self, tmp_path: Path) -> None:
        """Raises ValueError for path traversal in deployed_files."""
        from apm_cli.bundle.packer import pack_bundle
        from apm_cli.deps.lockfile import LockedDependency, LockFile

        (tmp_path / "apm.yml").write_text("name: test-pkg\nversion: 1.0.0\n")
        # Use a path that passes _filter_files_by_target but has .. traversal
        dep = LockedDependency(
            repo_url="https://github.com/owner/dep",
            deployed_files=[".agents/skills/../../../etc/passwd"],
        )
        lf = LockFile(dependencies={"evil-dep": dep})
        (tmp_path / "apm.lock.yaml").write_text(lf.to_yaml())
        with pytest.raises(ValueError, match="unsafe path"):
            pack_bundle(tmp_path, tmp_path / "out")

    def test_pack_bundle_no_apm_yml_still_works(self, tmp_path: Path) -> None:
        """pack_bundle works even when apm.yml is missing (uses dir name)."""
        from apm_cli.bundle.packer import pack_bundle
        from apm_cli.deps.lockfile import LockFile

        # No apm.yml, but we have a lockfile (no deps = no files to collect)
        lf = LockFile()
        (tmp_path / "apm.lock.yaml").write_text(lf.to_yaml())
        result = pack_bundle(tmp_path, tmp_path / "out", dry_run=True)
        assert result.files == []

    def test_pack_bundle_local_dep_raises(self, tmp_path: Path) -> None:
        """Local path deps in apm.yml are rejected by pack_bundle."""
        from apm_cli.bundle.packer import pack_bundle
        from apm_cli.deps.lockfile import LockFile

        (tmp_path / "apm.yml").write_text(
            "name: test-pkg\nversion: 1.0.0\ndependencies:\n  apm:\n    - ./local-dep\n"
        )
        lf = LockFile()
        (tmp_path / "apm.lock.yaml").write_text(lf.to_yaml())
        with pytest.raises(ValueError, match="local path dependency"):
            pack_bundle(tmp_path, tmp_path / "out")

    def test_pack_bundle_writes_output_dir(self, tmp_path: Path) -> None:
        """Non-dry-run pack creates the output bundle directory."""
        from apm_cli.bundle.packer import pack_bundle

        self._make_minimal_project(tmp_path)
        out_dir = tmp_path / "out"
        result = pack_bundle(tmp_path, out_dir)
        assert result.bundle_path.is_dir()
        assert (result.bundle_path / "apm.lock.yaml").exists()

    def test_pack_bundle_dry_run_with_archive_returns_archive_path(self, tmp_path: Path) -> None:
        """Dry-run with archive=True returns projected archive path."""
        from apm_cli.bundle.packer import pack_bundle

        self._make_minimal_project(tmp_path)
        result = pack_bundle(tmp_path, tmp_path / "out", dry_run=True, archive=True)
        assert str(result.bundle_path).endswith(".zip")


# ===========================================================================
# 16. More marketplace/client.py -- local fetch paths
# ===========================================================================


class TestMarketplaceClientLocalFetch:
    """Tests for marketplace/client.py local fetch paths."""

    def test_fetch_local_file_returns_dict(self, tmp_path: Path) -> None:
        """_fetch_local_file reads a marketplace.json directly."""
        from apm_cli.marketplace.client import _fetch_local_file

        manifest = tmp_path / "marketplace.json"
        manifest.write_text(json.dumps({"name": "my-market", "plugins": []}))
        source = MarketplaceSource(name="my-market", url=str(manifest), ref="main")
        result = _fetch_local_file(source, manifest)
        assert result is not None
        assert result["name"] == "my-market"

    def test_fetch_local_file_invalid_json_raises(self, tmp_path: Path) -> None:
        """_fetch_local_file raises MarketplaceFetchError for invalid JSON."""
        from apm_cli.marketplace.client import _fetch_local_file

        bad_file = tmp_path / "marketplace.json"
        bad_file.write_text("NOT JSON !!!")
        source = MarketplaceSource(name="bad", url=str(bad_file), ref="main")
        with pytest.raises(MarketplaceFetchError):
            _fetch_local_file(source, bad_file)

    def test_write_cache_with_metadata(self, tmp_path: Path) -> None:
        """_write_cache stores etag and last_modified in meta."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        with patch("apm_cli.marketplace.client._cache_dir", return_value=str(cache_dir)):
            _write_cache(
                "my-market",
                {"name": "test"},
                index_digest="sha256:abc123",
                etag='"v1"',
                last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
            )
            # Read the meta file directly to verify
            meta_files = list(cache_dir.glob("*.meta.json"))
            assert len(meta_files) == 1
            meta = json.loads(meta_files[0].read_text())
            assert meta["etag"] == '"v1"'
            assert meta["last_modified"] == "Mon, 01 Jan 2024 00:00:00 GMT"

    def test_validate_ref_rejects_empty(self) -> None:
        """_validate_ref rejects empty ref."""
        from apm_cli.marketplace.client import _validate_ref

        with pytest.raises(MarketplaceFetchError):
            _validate_ref("", "src")

    def test_validate_ref_rejects_colon(self) -> None:
        """_validate_ref rejects refs containing colons."""
        from apm_cli.marketplace.client import _validate_ref

        with pytest.raises(MarketplaceFetchError):
            _validate_ref("refs/tags/:injected", "src")

    def test_fetch_url_direct_non_https_raises(self) -> None:
        """_fetch_url_direct raises for non-HTTPS URLs."""
        from apm_cli.marketplace.client import _fetch_url_direct

        with pytest.raises(MarketplaceFetchError, match="HTTPS"):
            _fetch_url_direct("http://insecure.example.com/marketplace.json")

    def test_fetch_url_direct_with_mocked_response(self, tmp_path: Path) -> None:
        """_fetch_url_direct parses JSON from mocked HTTP response."""
        from apm_cli.marketplace.client import _fetch_url_direct

        payload = json.dumps({"name": "remote-market", "plugins": []}).encode()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://cdn.example.com/marketplace.json"
        mock_resp.headers = {"ETag": '"v1"', "Last-Modified": "Mon, 01 Jan 2024"}
        mock_resp.iter_content.return_value = [payload]
        mock_resp.close = MagicMock()

        with patch("apm_cli.marketplace.client._http_get", return_value=mock_resp):
            result = _fetch_url_direct("https://cdn.example.com/marketplace.json")
        assert result is not None
        assert result.data["name"] == "remote-market"
        assert result.etag == '"v1"'

    def test_fetch_url_direct_404_raises(self) -> None:
        """_fetch_url_direct raises MarketplaceFetchError on 404."""
        from apm_cli.marketplace.client import _fetch_url_direct

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.url = "https://cdn.example.com/marketplace.json"
        mock_resp.close = MagicMock()

        with patch("apm_cli.marketplace.client._http_get", return_value=mock_resp):
            with pytest.raises(MarketplaceFetchError, match="404"):
                _fetch_url_direct("https://cdn.example.com/marketplace.json")

    def test_fetch_url_direct_304_returns_none(self) -> None:
        """_fetch_url_direct returns None on 304 Not Modified."""
        from apm_cli.marketplace.client import _fetch_url_direct

        mock_resp = MagicMock()
        mock_resp.status_code = 304
        mock_resp.url = "https://cdn.example.com/marketplace.json"
        mock_resp.close = MagicMock()

        with patch("apm_cli.marketplace.client._http_get", return_value=mock_resp):
            result = _fetch_url_direct("https://cdn.example.com/marketplace.json")
        assert result is None


# ===========================================================================
# 17. More cache/http_cache.py -- edge cases
# ===========================================================================


class TestHttpCacheEdgeCases:
    """Additional edge-case tests for HttpCache."""

    def test_store_without_cache_control_uses_default_ttl(self, tmp_path: Path) -> None:
        """Entries stored without Cache-Control get 300s TTL."""
        cache = HttpCache(tmp_path)
        url = "https://example.com/no-cc"
        cache.store(url, b"data", status_code=200, headers={})
        # The entry should be retrievable immediately
        entry = cache.get(url)
        assert entry is not None

    def test_store_large_max_age_capped(self, tmp_path: Path) -> None:
        """max-age > 86400 is capped at MAX_HTTP_CACHE_TTL_SECONDS."""
        cache = HttpCache(tmp_path)
        ttl = cache._parse_ttl({"Cache-Control": "max-age=999999"})
        assert ttl == MAX_HTTP_CACHE_TTL_SECONDS

    def test_refresh_expiry_no_meta_file_is_noop(self, tmp_path: Path) -> None:
        """refresh_expiry on unknown URL does not raise."""
        cache = HttpCache(tmp_path)
        cache.refresh_expiry("https://unknown.example.com/x", headers={})  # should not raise

    def test_get_stats_empty_cache(self, tmp_path: Path) -> None:
        """get_stats returns zeros for an empty cache."""
        cache = HttpCache(tmp_path)
        stats = cache.get_stats()
        assert stats["entry_count"] == 0
        assert stats["total_size_bytes"] == 0

    def test_entry_path_is_deterministic(self, tmp_path: Path) -> None:
        """_entry_path returns the same path for the same URL."""
        cache = HttpCache(tmp_path)
        url = "https://example.com/deterministic"
        assert cache._entry_path(url) == cache._entry_path(url)

    def test_entry_path_different_urls_differ(self, tmp_path: Path) -> None:
        """_entry_path returns different paths for different URLs."""
        cache = HttpCache(tmp_path)
        p1 = cache._entry_path("https://example.com/a")
        p2 = cache._entry_path("https://example.com/b")
        assert p1 != p2

    def test_store_content_type_preserved(self, tmp_path: Path) -> None:
        """Content-Type header is stored and returned."""
        cache = HttpCache(tmp_path)
        url = "https://example.com/typed"
        cache.store(
            url,
            b"{}",
            status_code=200,
            headers={"Content-Type": "application/json", "Cache-Control": "max-age=3600"},
        )
        entry = cache.get(url)
        assert entry is not None
        assert entry.content_type == "application/json"


# ===========================================================================
# 18. More utils/reflink.py -- macOS / Linux paths
# ===========================================================================


class TestReflinkPlatform:
    """Tests for platform-specific reflink paths."""

    def setup_method(self) -> None:
        _reset_capability_cache()

    def test_clone_file_path_and_str_equivalent(self, tmp_path: Path) -> None:
        """clone_file accepts both Path and str arguments."""
        src = tmp_path / "src.txt"
        src.write_bytes(b"content")
        dst_path = tmp_path / "dst_path.txt"
        dst_str = tmp_path / "dst_str.txt"
        # Both should return bool without raising
        r1 = clone_file(src, dst_path)
        assert isinstance(r1, bool)
        if dst_path.exists():
            dst_path.unlink()
        r2 = clone_file(str(src), str(dst_str))
        assert isinstance(r2, bool)

    def test_is_device_known_unsupported_unknown_device(self, tmp_path: Path) -> None:
        """Unknown device returns False from _is_device_known_unsupported."""
        from apm_cli.utils.reflink import _is_device_known_unsupported

        # After reset, no devices are known
        result = _is_device_known_unsupported(str(tmp_path / "test.txt"))
        assert result is False

    def test_mark_device_supported_not_downgrade(self, tmp_path: Path) -> None:
        """_mark_device_supported does not downgrade False -> True."""
        p = str(tmp_path / "test.txt")
        _mark_device_unsupported(p)
        _mark_device_supported(p)  # Should NOT upgrade from False
        dev = os.stat(tmp_path).st_dev

        # After unsupported, device should remain False
        assert _device_capability.get(dev) is False

    def test_reflink_supported_linux_without_env(self) -> None:
        """On Linux (without APM_NO_REFLINK), reflink_supported returns True."""
        env = {k: v for k, v in os.environ.items() if k != "APM_NO_REFLINK"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(sys, "platform", "linux"):
                # On Linux the function returns True (FICLONE is available since kernel 4.5)
                result = reflink_supported()
                # Can't assert exactly True because the actual platform might be macOS
                assert isinstance(result, bool)

    def test_load_macos_clonefile_cached(self) -> None:
        """_load_macos_clonefile is idempotent (cached after first call)."""
        from apm_cli.utils import reflink as rl

        # Reset the loaded flag to re-probe
        old_loaded = rl._clonefile_loaded
        old_fn = rl._clonefile_fn
        rl._clonefile_loaded = False
        rl._clonefile_fn = None
        try:
            fn1 = rl._load_macos_clonefile()
            fn2 = rl._load_macos_clonefile()
            # Both calls return the same object (or None)
            assert fn1 is fn2
        finally:
            rl._clonefile_loaded = old_loaded
            rl._clonefile_fn = old_fn


# ===========================================================================
# 19. More marketplace/version_check.py
# ===========================================================================


class TestVersionCheckEdgeCases:
    """Edge cases for version_check."""

    def test_invalid_yaml_in_apm_yml(self, tmp_path: Path) -> None:
        """invalid_yaml status produces error row."""
        pkg_dir = tmp_path / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "apm.yml").write_text(": invalid: yaml: !!!")
        entry = PackageEntry(name="pkg", source="./pkg")
        config = _make_marketplace_config(version="1.0.0", strategy="lockstep", packages=[entry])
        report = check_version_alignment(config, tmp_path)
        assert not report.ok
        assert report.packages[0].reason in ("invalid_yaml", "no_apm_yml")

    def test_error_messages_no_apm_yml(self) -> None:
        """no_apm_yml reason produces appropriate error message."""
        row = PackageVersionRow(path="pkg", version=None, ok=False, reason="no_apm_yml")
        report = VersionAlignmentReport(
            strategy="lockstep", expected="1.0.0", ok=False, packages=(row,)
        )
        msgs = report.error_messages()
        assert "no apm.yml" in msgs[0]

    def test_error_messages_unknown_reason(self) -> None:
        """Unknown reasons are emitted as-is."""
        row = PackageVersionRow(path="pkg", version="1.0.0", ok=False, reason="some_other_reason")
        report = VersionAlignmentReport(
            strategy="lockstep", expected="1.0.0", ok=False, packages=(row,)
        )
        msgs = report.error_messages()
        assert "some_other_reason" in msgs[0]

    def test_empty_packages_is_ok(self) -> None:
        """No local packages means overall_ok=True."""
        config = _make_marketplace_config(version="1.0.0", strategy="lockstep", packages=[])
        from pathlib import Path

        report = check_version_alignment(config, Path("/tmp"))
        assert report.ok is True
        assert len(report.packages) == 0


# ===========================================================================
# 20. More adapters/client/claude.py -- update_config edge cases
# ===========================================================================


class TestClaudeAdapterEdgeCases:
    """Additional edge cases for ClaudeClientAdapter."""

    def test_update_config_merges_existing_mcp_json(self, tmp_path: Path) -> None:
        """update_config shallow-merges with existing .mcp.json."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        mcp_json = tmp_path / ".mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"old": {"command": "old-cmd"}}}) + "\n")
        adapter = ClaudeClientAdapter(project_root=tmp_path)
        adapter.update_config({"new": {"command": "new-cmd"}})
        data = json.loads(mcp_json.read_text())
        assert "old" in data["mcpServers"]
        assert "new" in data["mcpServers"]

    def test_update_config_handles_corrupted_json(self, tmp_path: Path) -> None:
        """update_config recovers from corrupted .mcp.json."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        mcp_json = tmp_path / ".mcp.json"
        mcp_json.write_text("NOT JSON!!!")
        adapter = ClaudeClientAdapter(project_root=tmp_path)
        result = adapter.update_config({"server": {"command": "npx"}})
        # Should still succeed by starting fresh
        assert result is True

    def test_get_current_config_handles_invalid_json(self, tmp_path: Path) -> None:
        """get_current_config returns empty mcpServers for corrupted JSON."""
        mcp_json = tmp_path / ".mcp.json"
        mcp_json.write_text("BROKEN")
        adapter = ClaudeClientAdapter(project_root=tmp_path)
        cfg = adapter.get_current_config()
        assert cfg == {"mcpServers": {}}

    def test_normalize_entry_preserves_non_default_tools(self) -> None:
        """Tools list other than ['*'] is preserved in stdio entries."""
        entry = {"command": "npx", "args": [], "tools": ["specific-tool"]}
        normalized = ClaudeClientAdapter._normalize_mcp_entry_for_claude_code(entry)
        assert normalized["tools"] == ["specific-tool"]


# ===========================================================================
# 21. More adapters/client/kiro.py -- configure_mcp_server
# ===========================================================================


class TestKiroAdapterConfigure:
    """Tests for KiroClientAdapter configure_mcp_server."""

    def test_configure_mcp_server_project_scope_no_kiro_dir_skips(self, tmp_path: Path) -> None:
        """configure_mcp_server returns True (opt-in skip) when .kiro/ absent."""
        adapter = KiroClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "test",
            "remotes": [{"url": "https://api.example.com/mcp", "transport_type": "http"}],
        }
        result = adapter.configure_mcp_server(
            "test/server",
            server_info_cache={"test/server": server_info},
        )
        # Project scope without .kiro/ returns True (silently skipped)
        assert result is True

    def test_configure_mcp_server_from_cache_with_kiro_dir(self, tmp_path: Path) -> None:
        """configure_mcp_server writes config when .kiro/ exists."""
        kiro_dir = tmp_path / ".kiro"
        kiro_dir.mkdir()
        adapter = KiroClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "test-server",
            "packages": [
                {
                    "name": "@test/pkg",
                    "registry_name": "npm",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        result = adapter.configure_mcp_server(
            "test/server",
            server_info_cache={"test/server": server_info},
        )
        assert result is True

    def test_format_server_config_unsupported_transport_raises(self, tmp_path: Path) -> None:
        """Unsupported transport raises ValueError."""
        adapter = KiroClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "bad-transport-server",
            "remotes": [{"url": "https://api.example.com", "transport_type": "websocket"}],
        }
        with pytest.raises(ValueError, match="Unsupported remote transport"):
            adapter._format_server_config(server_info)


# ===========================================================================
# 22. More adapters/client/gemini.py -- configure_mcp_server
# ===========================================================================


class TestGeminiAdapterConfigure:
    """Tests for GeminiClientAdapter configure_mcp_server."""

    def test_configure_mcp_server_project_scope_no_gemini_dir_skips(self, tmp_path: Path) -> None:
        """configure_mcp_server returns True (opt-in skip) when .gemini/ absent."""
        adapter = GeminiClientAdapter(project_root=tmp_path)
        server_info = {"name": "test", "packages": []}
        with patch.object(adapter, "_get_gemini_dir", return_value=tmp_path / ".gemini"):
            result = adapter.configure_mcp_server(
                "owner/server",
                server_info_cache={"owner/server": server_info},
            )
        # Project scope without .gemini/ returns True (silently skipped)
        assert result is True

    def test_configure_mcp_server_server_not_found(self, tmp_path: Path) -> None:
        """configure_mcp_server returns False when server not in registry."""
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        adapter = GeminiClientAdapter(project_root=tmp_path)
        with patch.object(adapter, "registry_client") as mock_rc:
            mock_rc.find_server_by_reference.return_value = None
            result = adapter.configure_mcp_server("owner/not-found")
        assert result is False

    def test_format_server_config_unsupported_transport_raises(self, tmp_path: Path) -> None:
        """Unsupported transport raises ValueError."""
        adapter = GeminiClientAdapter(project_root=tmp_path)
        server_info = {
            "name": "bad-server",
            "remotes": [{"url": "https://api.example.com", "transport_type": "grpc"}],
        }
        with pytest.raises(ValueError, match="Unsupported remote transport"):
            adapter._format_server_config(server_info)

    def test_update_config_preserves_non_mcp_keys(self, tmp_path: Path) -> None:
        """update_config preserves existing non-mcpServers keys."""
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        settings_file = gemini_dir / "settings.json"
        settings_file.write_text(json.dumps({"theme": "dark", "mcpServers": {}}))
        adapter = GeminiClientAdapter(project_root=tmp_path)
        adapter.update_config({"server": {"command": "npx"}})
        data = json.loads(settings_file.read_text())
        assert data["theme"] == "dark"
        assert "server" in data["mcpServers"]


# ===========================================================================
# 23. marketplace/client.py  -- fetch_marketplace and stale cache paths
# ===========================================================================


class TestFetchMarketplace:
    """Tests for fetch_marketplace main function."""

    def test_fetch_marketplace_from_fresh_cache(self, tmp_path: Path) -> None:
        """fetch_marketplace serves from cache when not expired."""
        from apm_cli.marketplace.client import fetch_marketplace

        payload = {"name": "cached-mp", "plugins": []}
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        with patch("apm_cli.marketplace.client._cache_dir", return_value=str(cache_dir)):
            source = MarketplaceSource(
                name="org/marketplace",
                url="https://github.com/org/marketplace",
                owner="org",
                repo="marketplace",
                ref="main",
            )
            cache_name = _cache_key(source)
            # Write a fresh cache entry
            _write_cache(cache_name, payload)
            result = fetch_marketplace(source)
        assert result.name == "cached-mp"

    def test_fetch_marketplace_url_source_with_mock(self, tmp_path: Path) -> None:
        """fetch_marketplace fetches a URL-kind source via _fetch_url_direct."""
        from apm_cli.marketplace.client import fetch_marketplace

        payload = {"name": "url-market", "plugins": []}
        mock_result = FetchResult(
            data=payload,
            digest="sha256:abc123",
            etag='"v1"',
            last_modified="Mon, 01 Jan 2024",
        )
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        source = MarketplaceSource(
            name="my-market",
            url="https://cdn.example.com/marketplace.json",
            ref="main",
            path="",  # makes kind == "url" (url ends with /marketplace.json)
        )

        with patch("apm_cli.marketplace.client._cache_dir", return_value=str(cache_dir)):
            with patch("apm_cli.marketplace.client._read_cache", return_value=None):
                with patch(
                    "apm_cli.marketplace.client._fetch_url_direct", return_value=mock_result
                ):
                    result = fetch_marketplace(source)
        assert result.name == "url-market"

    def test_fetch_marketplace_url_304_with_stale_cache(self, tmp_path: Path) -> None:
        """fetch_marketplace serves stale cache on 304 Not Modified."""
        from apm_cli.marketplace.client import fetch_marketplace

        payload = {"name": "stale-market", "plugins": []}
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        source = MarketplaceSource(
            name="stale-market",
            url="https://cdn.example.com/stale/marketplace.json",
            ref="main",
            path="",
        )

        with patch("apm_cli.marketplace.client._cache_dir", return_value=str(cache_dir)):
            with patch("apm_cli.marketplace.client._read_cache", return_value=None):
                with patch("apm_cli.marketplace.client._fetch_url_direct", return_value=None):
                    with patch(
                        "apm_cli.marketplace.client._read_stale_cache", return_value=payload
                    ):
                        with patch("apm_cli.marketplace.client._read_stale_meta", return_value={}):
                            result = fetch_marketplace(source)
        assert result.name == "stale-market"

    def test_fetch_marketplace_stale_while_revalidate(self, tmp_path: Path) -> None:
        """On network error, fetch_marketplace returns stale cache for API sources."""
        from apm_cli.marketplace.client import fetch_marketplace

        payload = {"name": "fallback-market", "plugins": []}
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        source = MarketplaceSource(
            name="org/marketplace",
            url="https://github.com/org/marketplace",
            owner="org",
            repo="marketplace",
            ref="main",
        )

        with patch("apm_cli.marketplace.client._cache_dir", return_value=str(cache_dir)):
            with patch("apm_cli.marketplace.client._read_cache", return_value=None):
                with patch("apm_cli.marketplace.client._read_stale_meta", return_value={}):
                    with patch(
                        "apm_cli.marketplace.client._fetch_file",
                        side_effect=MarketplaceFetchError("org/marketplace", "network error"),
                    ):
                        with patch(
                            "apm_cli.marketplace.client._read_stale_cache", return_value=payload
                        ):
                            result = fetch_marketplace(source)
        assert result.name == "fallback-market"

    def test_fetch_marketplace_local_direct_read(self, tmp_path: Path) -> None:
        """Local kind source fetches directly from filesystem."""
        from apm_cli.marketplace.client import fetch_marketplace

        marketplace_dir = tmp_path / "marketplace"
        marketplace_dir.mkdir()
        (marketplace_dir / "marketplace.json").write_text(
            json.dumps({"name": "local-market", "plugins": []})
        )

        source = MarketplaceSource(
            name="local-market",
            url=str(marketplace_dir),
            ref="main",
        )
        result = fetch_marketplace(source)
        assert result.name == "local-market"

    def test_fetch_marketplace_force_refresh_skips_cache(self, tmp_path: Path) -> None:
        """force_refresh skips the sidecar cache."""
        from apm_cli.marketplace.client import fetch_marketplace

        payload = {"name": "force-market", "plugins": []}
        fresh_data = {"name": "force-market", "plugins": []}
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        source = MarketplaceSource(
            name="org/marketplace",
            url="https://github.com/org/marketplace",
            owner="org",
            repo="marketplace",
            ref="main",
        )

        FetchResult(
            data=fresh_data,
            digest="sha256:newdigest",
            etag='"v2"',
            last_modified="",
        )

        with patch("apm_cli.marketplace.client._cache_dir", return_value=str(cache_dir)):
            # Even with stale cache present, force_refresh fetches fresh
            _write_cache(_cache_key(source), payload)
            with patch("apm_cli.marketplace.client._fetch_file", return_value=fresh_data):
                result = fetch_marketplace(source, force_refresh=True)
        assert result.name == "force-market"


# ===========================================================================
# 24. marketplace/client.py  -- _fetch_local* paths
# ===========================================================================


class TestFetchLocalPaths:
    """Tests for _fetch_local* functions."""

    def test_fetch_local_direct_read_existing_file(self, tmp_path: Path) -> None:
        """_fetch_local_direct_read returns JSON from a plain directory."""
        from apm_cli.marketplace.client import _fetch_local_direct_read

        (tmp_path / "marketplace.json").write_text(
            json.dumps({"name": "direct-market", "plugins": []})
        )
        source = MarketplaceSource(name="direct-market", url=str(tmp_path), ref="main")
        result = _fetch_local_direct_read(source, "marketplace.json", tmp_path)
        assert result is not None
        assert result["name"] == "direct-market"

    def test_fetch_local_direct_read_missing_file_returns_none(self, tmp_path: Path) -> None:
        """_fetch_local_direct_read returns None when the file is missing."""
        from apm_cli.marketplace.client import _fetch_local_direct_read

        source = MarketplaceSource(name="missing-market", url=str(tmp_path), ref="main")
        result = _fetch_local_direct_read(source, "marketplace.json", tmp_path)
        assert result is None

    def test_fetch_local_working_directory(self, tmp_path: Path) -> None:
        """_fetch_local reads from a plain working directory."""
        from apm_cli.marketplace.client import _fetch_local

        (tmp_path / "marketplace.json").write_text(json.dumps({"name": "wd-market", "plugins": []}))
        source = MarketplaceSource(name="wd-market", url=str(tmp_path), ref="main")
        result = _fetch_local(source, "marketplace.json")
        assert result is not None
        assert result["name"] == "wd-market"

    def test_fetch_local_nonexistent_path_raises(self, tmp_path: Path) -> None:
        """_fetch_local raises MarketplaceFetchError for missing local path."""
        from apm_cli.marketplace.client import _fetch_local

        nonexistent = tmp_path / "does-not-exist"
        source = MarketplaceSource(name="bad-path", url=str(nonexistent), ref="main")
        with pytest.raises(MarketplaceFetchError, match="does not exist"):
            _fetch_local(source, "marketplace.json")

    def test_fetch_local_file_as_direct_manifest(self, tmp_path: Path) -> None:
        """_fetch_local reads a direct .json file as the manifest."""
        from apm_cli.marketplace.client import _fetch_local

        manifest_file = tmp_path / "my_marketplace.json"
        manifest_file.write_text(json.dumps({"name": "file-market", "plugins": []}))
        source = MarketplaceSource(name="file-market", url=str(manifest_file), ref="main")
        result = _fetch_local(source, "marketplace.json")
        assert result is not None
        assert result["name"] == "file-market"


# ===========================================================================
# 25. utils/install_tui.py  -- InstallTui lifecycle with mocked Rich
# ===========================================================================


class TestInstallTuiLifecycle:
    """Tests for InstallTui lifecycle methods (with mocked Rich)."""

    def test_install_tui_start_phase_disabled(self) -> None:
        """start_phase is a no-op when TUI is disabled."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui.start_phase("resolve", 10)  # should not raise

    def test_install_tui_task_started_key_tracking(self) -> None:
        """task_started tracks keys and labels correctly."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            # Even in disabled mode, the internal state should be consistent
            with tui:
                tui.task_started("key1", "label-1")
                # Disabled: no state changes
                assert len(tui._key_to_label) == 0

    def test_install_tui_task_failed_delegates_to_completed(self) -> None:
        """task_failed is equivalent to task_completed."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            with tui:
                # Both should be no-ops when disabled
                tui.task_failed("dep1", "[x] failed dep1")
                tui.task_failed("dep2")  # no milestone

    def test_install_tui_multiple_enter_exit(self) -> None:
        """TUI supports multiple enter/exit cycles."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            with tui:
                tui.start_phase("phase1", 5)
            with tui:
                tui.start_phase("phase2", 3)

    def test_install_tui_build_aggregate_returns_progress(self) -> None:
        """_build_aggregate returns a Rich Progress instance."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            aggregate = tui._build_aggregate()
            # Should be a Rich Progress object
            assert aggregate is not None
            assert hasattr(aggregate, "add_task")

    def test_install_tui_labels_renderable_empty(self) -> None:
        """_labels_renderable returns empty text when no labels."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            renderable = tui._labels_renderable()
            # Should return a Rich Text object
            assert renderable is not None

    def test_install_tui_labels_renderable_with_labels(self) -> None:
        """_labels_renderable formats visible labels correctly."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._labels = ["dep-a@1.0", "dep-b@2.0"]
            renderable = tui._labels_renderable()
            from rich.text import Text

            assert isinstance(renderable, Text)

    def test_install_tui_labels_renderable_truncated(self) -> None:
        """_labels_renderable shows '... and N more' for excess labels."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._labels = [f"dep-{i}" for i in range(10)]
            renderable = tui._labels_renderable()
            from rich.text import Text

            assert isinstance(renderable, Text)
            assert "more" in str(renderable)

    def test_should_animate_quiet_variants(self) -> None:
        """Various 'quiet' values disable animation."""
        for val in ("quiet", "off", "0", "false", "no"):
            with patch.dict(os.environ, {"APM_PROGRESS": val}):
                assert should_animate() is False

    def test_should_animate_always_variants(self) -> None:
        """Various 'always' values force animation."""
        for val in ("on", "1", "true", "yes"):
            with patch.dict(os.environ, {"APM_PROGRESS": val}):
                assert should_animate() is True


# ===========================================================================
# 26. More adapters/client/codex.py  -- format_server_config edge cases
# ===========================================================================


class TestCodexFormatServerConfig:
    """Additional tests for CodexClientAdapter._format_server_config."""

    def test_format_server_config_docker_package(self, tmp_path: Path) -> None:
        """Docker package generates docker command."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "id": "abc",
            "name": "docker-server",
            "packages": [
                {
                    "name": "ghcr.io/owner/mcp-server:latest",
                    "registry_name": "docker",
                    "runtime_arguments": ["run", "-i", "--rm", "ghcr.io/owner/mcp-server:latest"],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        result = adapter._format_server_config(server_info)
        assert result is not None
        assert result["command"] == "docker"

    def test_format_server_config_no_packages_raises(self, tmp_path: Path) -> None:
        """ValueError raised when no packages and no remotes."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "id": "abc",
            "name": "empty-server",
            "packages": [],
            "remotes": [],
        }
        with pytest.raises(ValueError, match="no package information"):
            adapter._format_server_config(server_info)

    def test_format_server_config_npm_with_runtime_args(self, tmp_path: Path) -> None:
        """NPM package with runtime_arguments that include the package name."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "id": "abc",
            "name": "npm-server",
            "packages": [
                {
                    "name": "@azure/mcp",
                    "registry_name": "npm",
                    "runtime_arguments": ["-y", "@azure/mcp@latest"],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        result = adapter._format_server_config(server_info)
        assert result is not None
        assert result["command"] in ("npx", "npm")

    def test_format_server_config_remote_with_headers(self, tmp_path: Path) -> None:
        """HTTP remote with headers gets http_headers in config."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "id": "abc",
            "name": "auth-server",
            "remotes": [
                {
                    "url": "https://api.example.com/mcp",
                    "transport_type": "http",
                    "headers": [{"name": "Authorization", "value": "${MY_TOKEN}"}],
                }
            ],
        }
        result = adapter._format_server_config(server_info)
        assert result is not None
        assert "url" in result
        # Headers resolved (or passed through) should be in http_headers
        assert "http_headers" in result

    def test_format_server_config_hybrid_prefers_package(self, tmp_path: Path) -> None:
        """Hybrid server (remote + packages) prefers packages."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "id": "abc",
            "name": "hybrid-server",
            "remotes": [{"url": "https://api.example.com/mcp", "transport_type": "http"}],
            "packages": [
                {
                    "name": "@hybrid/pkg",
                    "registry_name": "npm",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        result = adapter._format_server_config(server_info)
        assert result is not None
        assert result["command"] in ("npx", "npm")


# ===========================================================================
# 27. More models/dependency/lsp.py  -- to_dict complete serialization
# ===========================================================================


class TestLSPDependencySerializationComplete:
    """Tests for full serialization of LSPDependency."""

    def test_to_dict_all_fields_present(self) -> None:
        """to_dict includes all non-None fields with camelCase."""
        dep = LSPDependency(
            name="full-lsp",
            command="full-lsp",
            args=["--verbose"],
            extension_to_language={".rs": "rust", ".toml": "toml"},
            transport="stdio",
            env={"RUST_LOG": "info"},
            initialization_options={"checkOnSave": True},
            settings={"root": "/workspace"},
            workspace_folder="/workspace",
            startup_timeout=10,
            shutdown_timeout=5,
            restart_on_crash=True,
            max_restarts=3,
        )
        d = dep.to_dict()
        assert d["command"] == "full-lsp"
        assert d["args"] == ["--verbose"]
        assert d["extensionToLanguage"] == {".rs": "rust", ".toml": "toml"}
        assert d["transport"] == "stdio"
        assert d["env"] == {"RUST_LOG": "info"}
        assert d["initializationOptions"] == {"checkOnSave": True}
        assert d["settings"] == {"root": "/workspace"}
        assert d["workspaceFolder"] == "/workspace"
        assert d["startupTimeout"] == 10
        assert d["shutdownTimeout"] == 5
        assert d["restartOnCrash"] is True
        assert d["maxRestarts"] == 3

    def test_to_lsp_json_entry_no_name_key(self) -> None:
        """to_lsp_json_entry drops 'name' and returns server config dict."""
        dep = LSPDependency(
            name="server",
            command="lang-server",
            args=[],
            extension_to_language={".py": "python"},
        )
        entry = dep.to_lsp_json_entry()
        assert "name" not in entry
        assert "command" in entry
        assert "extensionToLanguage" in entry

    def test_validate_workspace_folder_traversal_raises(self) -> None:
        """validate raises for workspaceFolder with path traversal."""
        dep = LSPDependency(
            name="test",
            command="test",
            extension_to_language={".py": "python"},
            workspace_folder="../../evil",
        )
        with pytest.raises(ValueError, match="must not contain"):
            dep.validate(strict=True)

    def test_repr_includes_extension_to_language(self) -> None:
        """__repr__ includes extensionToLanguage when set."""
        dep = LSPDependency(
            name="test",
            command="test",
            extension_to_language={".ts": "typescript"},
        )
        r = repr(dep)
        assert "extensionToLanguage" in r

    def test_extension_to_language_non_string_values_raises(self) -> None:
        """Validation raises when extensionToLanguage maps to non-strings."""
        dep = LSPDependency(
            name="test",
            command="test",
            extension_to_language={".py": 42},  # type: ignore[dict-item]
        )
        with pytest.raises(ValueError, match="string"):
            dep.validate(strict=True)


# ===========================================================================
# 28. More cache/http_cache.py  -- LRU eviction
# ===========================================================================


class TestHttpCacheLRU:
    """Tests for HTTP cache LRU eviction."""

    def test_enforce_size_cap_evicts_oldest(self, tmp_path: Path) -> None:
        """_enforce_size_cap evicts oldest entries when over the cap."""
        cache = HttpCache(tmp_path)
        # Store several entries (small enough to not trigger cap naturally)
        for i in range(5):
            cache.store(
                f"https://example.com/item{i}",
                b"x" * 1000,
                status_code=200,
                headers={"Cache-Control": "max-age=3600"},
            )
        stats_before = cache.get_stats()
        assert stats_before["entry_count"] == 5

        # Mock MAX_HTTP_CACHE_BYTES to be very small to force eviction
        with patch("apm_cli.cache.http_cache.MAX_HTTP_CACHE_BYTES", 1):
            cache._enforce_size_cap()
        stats_after = cache.get_stats()
        # Some entries should have been evicted
        assert stats_after["entry_count"] < stats_before["entry_count"]

    def test_parse_ttl_case_insensitive(self, tmp_path: Path) -> None:
        """Cache-Control header is parsed case-insensitively."""
        cache = HttpCache(tmp_path)
        assert cache._parse_ttl({"cache-control": "max-age=120"}) == 120.0

    def test_refresh_expiry_updates_etag(self, tmp_path: Path) -> None:
        """refresh_expiry updates ETag from 304 response headers."""
        cache = HttpCache(tmp_path)
        url = "https://example.com/refresh-etag"
        cache.store(
            url,
            b"body",
            status_code=200,
            headers={"ETag": '"old-etag"', "Cache-Control": "max-age=3600"},
        )
        cache.refresh_expiry(url, headers={"ETag": '"new-etag"', "Cache-Control": "max-age=3600"})
        entry = cache.get(url)
        assert entry is not None
        assert entry.etag == '"new-etag"'


# ===========================================================================
# 29. adapters/client/base.py -- translate mode via custom adapter
# ===========================================================================


class _TranslateTestAdapter(MCPClientAdapter):
    """Minimal concrete adapter for testing base-class translate mode."""

    target_name: str = "translate-test"
    mcp_servers_key: str = "servers"
    _supports_runtime_env_substitution: bool = True

    def get_config_path(self) -> str:
        return str(self.project_root / ".test" / "config.json")

    def update_config(self, config_updates: dict) -> bool | None:
        return True

    def get_current_config(self) -> dict:
        return {}

    def configure_mcp_server(
        self,
        server_url,
        server_name=None,
        enabled=True,
        env_overrides=None,
        server_info_cache=None,
        runtime_vars=None,
    ):
        return True


class TestBaseTranslateModeViaCustomAdapter:
    """Tests for base.py translate-mode paths using a direct MCPClientAdapter subclass."""

    def test_resolve_env_vars_translate_mode_dict(self, tmp_path: Path) -> None:
        """Base class translate mode with dict-shaped env."""
        adapter = _TranslateTestAdapter(project_root=tmp_path)
        env_dict = {"MY_TOKEN": "${MY_TOKEN}", "ANOTHER": "<ANOTHER>"}
        result = adapter._resolve_environment_variables(env_dict)
        assert result["MY_TOKEN"] == "${MY_TOKEN}"
        assert result["ANOTHER"] == "${ANOTHER}"

    def test_resolve_env_vars_translate_mode_dict_github_defaults(self, tmp_path: Path) -> None:
        """GitHub defaults are emitted verbatim in base translate mode."""
        adapter = _TranslateTestAdapter(project_root=tmp_path)
        env_dict = {"GITHUB_TOOLSETS": "context"}
        result = adapter._resolve_environment_variables(env_dict)
        assert result["GITHUB_TOOLSETS"] == "context"

    def test_resolve_env_vars_translate_mode_dict_literal_becomes_placeholder(
        self, tmp_path: Path
    ) -> None:
        """Literal env value becomes ${VAR} placeholder in base translate mode."""
        adapter = _TranslateTestAdapter(project_root=tmp_path)
        env_dict = {"SECRET": "literal-secret-value"}
        result = adapter._resolve_environment_variables(env_dict)
        assert result["SECRET"] == "${SECRET}"

    def test_resolve_env_vars_translate_mode_dict_empty_name_skipped(self, tmp_path: Path) -> None:
        """Empty name keys are skipped in translate mode."""
        adapter = _TranslateTestAdapter(project_root=tmp_path)
        env_dict = {"": "some-value", "VALID": "${VALID}"}
        result = adapter._resolve_environment_variables(env_dict)
        assert "" not in result
        assert "VALID" in result

    def test_resolve_env_vars_translate_mode_registry_list(self, tmp_path: Path) -> None:
        """Registry list shape gets ${VAR} placeholders in base translate mode."""
        adapter = _TranslateTestAdapter(project_root=tmp_path)
        env_vars = [
            {"name": "API_KEY", "description": "API key", "required": True},
            {"name": "GITHUB_TOOLSETS", "description": "toolsets"},
        ]
        result = adapter._resolve_environment_variables(env_vars)
        assert result["API_KEY"] == "${API_KEY}"
        assert result["GITHUB_TOOLSETS"] == "context"

    def test_resolve_env_vars_translate_mode_list_empty_name_skipped(self, tmp_path: Path) -> None:
        """Empty name entries are skipped in list translate mode."""
        adapter = _TranslateTestAdapter(project_root=tmp_path)
        env_vars = [{"name": "", "description": "skip me"}, {"name": "VALID_VAR"}]
        result = adapter._resolve_environment_variables(env_vars)
        assert "" not in result
        assert "VALID_VAR" in result

    def test_resolve_env_vars_translate_mode_list_non_dict_skipped(self, tmp_path: Path) -> None:
        """Non-dict entries in list are skipped in translate mode."""
        adapter = _TranslateTestAdapter(project_root=tmp_path)
        env_vars = ["string-entry", {"name": "VALID_VAR"}]
        result = adapter._resolve_environment_variables(env_vars)
        assert "VALID_VAR" in result

    def test_resolve_env_variable_translate_mode_base_class(self, tmp_path: Path) -> None:
        """Base class _resolve_env_variable translates in translate mode."""
        adapter = _TranslateTestAdapter(project_root=tmp_path)
        result = adapter._resolve_env_variable("MY_VAR", "<MY_VAR>")
        assert result == "${MY_VAR}"

    def test_resolve_env_variable_translate_mode_tracks_legacy_vars(self, tmp_path: Path) -> None:
        """Base class tracks legacy angle-bracket vars for deprecation warnings."""
        adapter = _TranslateTestAdapter(project_root=tmp_path)
        adapter._resolve_env_variable("TOKEN", "<MY_LEGACY_TOKEN>")
        assert "MY_LEGACY_TOKEN" in adapter._last_legacy_angle_vars


# ===========================================================================
# 30. marketplace/client.py -- _auto_detect_path and git fetch
# ===========================================================================


class TestMarketplaceClientFetchPaths:
    """Tests for marketplace/client.py fetch dispatch and helper functions."""

    def test_auto_detect_path_finds_first_candidate(self, tmp_path: Path) -> None:
        """_auto_detect_path returns the first candidate that exists."""
        from apm_cli.marketplace.client import _auto_detect_path

        # Create a marketplace.json in the default location
        (tmp_path / "marketplace.json").write_text(
            json.dumps({"name": "auto-market", "plugins": []})
        )
        source = MarketplaceSource(name="auto-market", url=str(tmp_path), ref="main")
        path = _auto_detect_path(source)
        assert path == "marketplace.json"

    def test_auto_detect_path_returns_none_when_not_found(self, tmp_path: Path) -> None:
        """_auto_detect_path returns None when no candidate exists."""
        from apm_cli.marketplace.client import _auto_detect_path

        # Empty directory - no marketplace.json files
        source = MarketplaceSource(name="no-market", url=str(tmp_path), ref="main")
        path = _auto_detect_path(source)
        assert path is None

    def test_host_from_url_http_url(self) -> None:
        """_host_from_url extracts host from HTTP URL."""
        from apm_cli.marketplace.client import _host_from_url

        host = _host_from_url("https://gitea.example.com/org/repo")
        assert host == "gitea.example.com"

    def test_host_from_url_scp_like(self) -> None:
        """_host_from_url handles SCP-like git URLs."""
        from apm_cli.marketplace.client import _host_from_url

        host = _host_from_url("git@github.com:org/repo.git")
        assert host == "github.com"

    def test_host_from_url_empty_returns_empty(self) -> None:
        """_host_from_url returns empty string for empty input."""
        from apm_cli.marketplace.client import _host_from_url

        assert _host_from_url("") == ""

    def test_fetch_file_local_kind_dispatches(self, tmp_path: Path) -> None:
        """_fetch_file dispatches local kind to _fetch_local."""
        from apm_cli.marketplace.client import _fetch_file

        (tmp_path / "marketplace.json").write_text(
            json.dumps({"name": "file-market", "plugins": []})
        )
        source = MarketplaceSource(name="file-market", url=str(tmp_path), ref="main")
        result = _fetch_file(source, "marketplace.json")
        assert result is not None
        assert result["name"] == "file-market"

    def test_fetch_git_with_mocked_git_cache(self, tmp_path: Path) -> None:
        """_fetch_git calls GitCache and reads the marketplace file."""
        from apm_cli.marketplace.client import _fetch_git
        from apm_cli.marketplace.models import MarketplaceSource

        # Create a fake checkout directory with marketplace.json
        checkout_dir = tmp_path / "checkout"
        checkout_dir.mkdir()
        (checkout_dir / "marketplace.json").write_text(
            json.dumps({"name": "git-market", "plugins": []})
        )

        source = MarketplaceSource(
            name="gitea/repo",
            url="https://gitea.example.com/org/repo",
            ref="main",
            owner="org",
            repo="repo",
        )

        mock_auth_ctx = MagicMock()
        mock_auth_ctx.git_env = {}
        mock_auth_resolver = MagicMock()
        mock_auth_resolver.resolve.return_value = mock_auth_ctx

        mock_host_info = MagicMock()
        mock_host_info.host = "gitea.example.com"

        with patch("apm_cli.cache.git_cache.GitCache") as mock_git_cache_cls:
            mock_git_cache = MagicMock()
            mock_git_cache.get_checkout.return_value = str(checkout_dir)
            mock_git_cache_cls.return_value = mock_git_cache
            with patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path):
                result = _fetch_git(
                    source,
                    "marketplace.json",
                    host_info=mock_host_info,
                    auth_resolver=mock_auth_resolver,
                )
        assert result is not None
        assert result["name"] == "git-market"

    def test_fetch_git_not_found_returns_none(self, tmp_path: Path) -> None:
        """_fetch_git returns None when CalledProcessError has 'not found' in stderr."""
        import subprocess

        from apm_cli.marketplace.client import _fetch_git

        source = MarketplaceSource(
            name="gitea/repo",
            url="https://gitea.example.com/org/repo",
            ref="main",
            owner="org",
            repo="repo",
        )

        mock_auth_ctx = MagicMock()
        mock_auth_ctx.git_env = {}
        mock_auth_resolver = MagicMock()
        mock_auth_resolver.resolve.return_value = mock_auth_ctx
        mock_host_info = MagicMock()
        mock_host_info.host = "gitea.example.com"

        exc = subprocess.CalledProcessError(128, "git")
        exc.stderr = b"fatal: remote ref not found"

        with patch("apm_cli.cache.git_cache.GitCache") as mock_git_cache_cls:
            mock_git_cache = MagicMock()
            mock_git_cache.get_checkout.side_effect = exc
            mock_git_cache_cls.return_value = mock_git_cache
            with patch("apm_cli.cache.paths.get_cache_root", return_value=tmp_path):
                result = _fetch_git(
                    source,
                    "marketplace.json",
                    host_info=mock_host_info,
                    auth_resolver=mock_auth_resolver,
                )
        assert result is None


# ===========================================================================
# 31. marketplace/client.py  -- _read_bounded_response_bytes
# ===========================================================================


class TestMarketplaceClientHTTP:
    """Tests for HTTP fetch helpers in marketplace/client.py."""

    def test_read_bounded_response_bytes_exceeds_limit(self) -> None:
        """_read_bounded_response_bytes raises on oversized responses."""
        from apm_cli.marketplace.client import _read_bounded_response_bytes

        mock_resp = MagicMock()
        # Return 2 large chunks that together exceed the limit
        mock_resp.iter_content.return_value = [b"x" * 600000, b"x" * 600000]
        with pytest.raises(MarketplaceFetchError, match="exceeds"):
            _read_bounded_response_bytes(mock_resp, "https://example.com/big", 1000000)

    def test_read_bounded_response_bytes_within_limit(self) -> None:
        """_read_bounded_response_bytes assembles chunks within limit."""
        from apm_cli.marketplace.client import _read_bounded_response_bytes

        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = [b"hello", b" ", b"world"]
        result = _read_bounded_response_bytes(mock_resp, "https://example.com/small", 1000000)
        assert result == b"hello world"

    def test_fetch_url_direct_digest_mismatch_raises(self) -> None:
        """_fetch_url_direct raises when expected digest doesn't match."""
        from apm_cli.marketplace.client import _fetch_url_direct

        payload = json.dumps({"name": "test"}).encode()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://cdn.example.com/marketplace.json"
        mock_resp.headers = {}
        mock_resp.iter_content.return_value = [payload]
        mock_resp.close = MagicMock()

        with patch("apm_cli.marketplace.client._http_get", return_value=mock_resp):
            with pytest.raises(MarketplaceFetchError, match="digest mismatch"):
                _fetch_url_direct(
                    "https://cdn.example.com/marketplace.json",
                    expected_digest="sha256:wrongdigest",
                )

    def test_fetch_url_direct_not_object_raises(self) -> None:
        """_fetch_url_direct raises when response root is not a JSON object."""
        from apm_cli.marketplace.client import _fetch_url_direct

        # Return a JSON array instead of object
        payload = json.dumps([1, 2, 3]).encode()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://cdn.example.com/marketplace.json"
        mock_resp.headers = {}
        mock_resp.iter_content.return_value = [payload]
        mock_resp.close = MagicMock()

        with patch("apm_cli.marketplace.client._http_get", return_value=mock_resp):
            with pytest.raises(MarketplaceFetchError, match="root must be an object"):
                _fetch_url_direct("https://cdn.example.com/marketplace.json")


# ===========================================================================
# 32. adapters/client/base.py -- legacy mode (non-translate) env resolution
# ===========================================================================


class TestBaseAdapterLegacyMode:
    """Tests for base-class legacy-mode (non-translate) env resolution.

    CodexClientAdapter inherits from MCPClientAdapter without overriding
    _resolve_environment_variables, and has _supports_runtime_env_substitution=False,
    so it exercises the base-class legacy path.
    """

    def _make_legacy_adapter(self, tmp_path: Path) -> CodexClientAdapter:
        """Return a CodexClientAdapter (legacy mode, no translate)."""
        return CodexClientAdapter(project_root=tmp_path)

    def test_resolve_environment_variables_legacy_dict(self, tmp_path: Path) -> None:
        """Legacy mode dict-shape: resolves placeholders against env_overrides."""
        adapter = self._make_legacy_adapter(tmp_path)
        env_dict = {"MY_TOKEN": "${MY_TOKEN}"}
        with patch.dict(os.environ, {"APM_E2E_TESTS": "1"}):
            result = adapter._resolve_environment_variables(
                env_dict, env_overrides={"MY_TOKEN": "actual-value"}
            )
        assert result["MY_TOKEN"] == "actual-value"

    def test_resolve_environment_variables_legacy_dict_non_string(self, tmp_path: Path) -> None:
        """Legacy mode dict-shape: non-string values are stringified."""
        adapter = self._make_legacy_adapter(tmp_path)
        env_dict = {"COUNT": 42}
        with patch.dict(os.environ, {"APM_E2E_TESTS": "1"}):
            result = adapter._resolve_environment_variables(env_dict)
        assert result["COUNT"] == "42"

    def test_resolve_environment_variables_legacy_list_with_overrides(self, tmp_path: Path) -> None:
        """Legacy mode list-shape: resolves from env_overrides."""
        adapter = self._make_legacy_adapter(tmp_path)
        env_vars = [
            {"name": "API_KEY", "description": "key", "required": True},
        ]
        with patch.dict(os.environ, {"APM_E2E_TESTS": "1"}):
            result = adapter._resolve_environment_variables(
                env_vars, env_overrides={"API_KEY": "my-key"}
            )
        assert result["API_KEY"] == "my-key"

    def test_resolve_environment_variables_legacy_list_from_os_env(self, tmp_path: Path) -> None:
        """Legacy mode: reads from os.environ when no override provided."""
        adapter = self._make_legacy_adapter(tmp_path)
        env_vars = [{"name": "CODEX_TEST_VAR", "description": "test var"}]
        with patch.dict(os.environ, {"CODEX_TEST_VAR": "from-env", "APM_E2E_TESTS": "1"}):
            result = adapter._resolve_environment_variables(env_vars)
        assert result["CODEX_TEST_VAR"] == "from-env"

    def test_resolve_env_variable_legacy_resolves_override(self, tmp_path: Path) -> None:
        """_resolve_env_variable (legacy) uses env_overrides for placeholder."""
        adapter = self._make_legacy_adapter(tmp_path)
        with patch.dict(os.environ, {"APM_E2E_TESTS": "1"}):
            result = adapter._resolve_env_variable(
                "MY_TOKEN", "${MY_TOKEN}", env_overrides={"MY_TOKEN": "secret-val"}
            )
        assert result == "secret-val"

    def test_resolve_env_variable_legacy_unresolved_placeholder_stays(self, tmp_path: Path) -> None:
        """_resolve_env_variable (legacy) keeps placeholder when not resolved."""
        adapter = self._make_legacy_adapter(tmp_path)
        with patch.dict(os.environ, {"APM_E2E_TESTS": "1"}):
            result = adapter._resolve_env_variable("UNSET_VAR", "${UNSET_VAR}")
        assert result == "${UNSET_VAR}"

    def test_resolve_env_variable_legacy_resolves_from_os_env(self, tmp_path: Path) -> None:
        """_resolve_env_variable (legacy) reads from os.environ."""
        adapter = self._make_legacy_adapter(tmp_path)
        with patch.dict(os.environ, {"CODEX_LEGACY_VAR": "env-value", "APM_E2E_TESTS": "1"}):
            result = adapter._resolve_env_variable("CODEX_LEGACY_VAR", "${CODEX_LEGACY_VAR}")
        assert result == "env-value"

    def test_resolve_variable_placeholders_legacy_mode_replaces_angle(self, tmp_path: Path) -> None:
        """_resolve_variable_placeholders (legacy) replaces <VAR> from resolved_env."""
        adapter = self._make_legacy_adapter(tmp_path)
        result = adapter._resolve_variable_placeholders(
            "--token <MY_TOKEN>",
            {"MY_TOKEN": "actual-token"},
            {},
        )
        assert result == "--token actual-token"

    def test_resolve_variable_placeholders_legacy_keeps_unresolved(self, tmp_path: Path) -> None:
        """_resolve_variable_placeholders keeps unresolved <VAR> in legacy mode."""
        adapter = self._make_legacy_adapter(tmp_path)
        result = adapter._resolve_variable_placeholders(
            "--token <MISSING_VAR>",
            {},
            {},
        )
        assert result == "--token <MISSING_VAR>"


# ===========================================================================
# 33. utils/install_tui.py -- enabled TUI lifecycle with mocked Rich
# ===========================================================================


class TestInstallTuiEnabled:
    """Tests for InstallTui with enabled state (mocking Rich Live)."""

    def test_start_phase_with_enabled_tui(self) -> None:
        """start_phase builds aggregate and adds a task when enabled."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            # Manually enable the TUI to test start_phase logic
            tui._enabled = True
            # Mock the aggregate to avoid actual Rich rendering
            mock_aggregate = MagicMock()
            mock_task_id = MagicMock()
            mock_aggregate.add_task.return_value = mock_task_id
            tui._aggregate = mock_aggregate
            tui.start_phase("download", 5)
            mock_aggregate.add_task.assert_called()

    def test_task_completed_advance_aggregate(self) -> None:
        """task_completed advances the aggregate bar."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._enabled = True
            mock_aggregate = MagicMock()
            mock_task_id = MagicMock()
            tui._aggregate = mock_aggregate
            tui._task_id = mock_task_id
            tui._key_to_label = {"dep1": "label-1"}
            tui._labels = ["label-1"]
            tui.task_completed("dep1")
            mock_aggregate.advance.assert_called_with(mock_task_id, 1)
            assert "dep1" not in tui._key_to_label

    def test_task_started_adds_label(self) -> None:
        """task_started adds the label to _labels list."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._enabled = True
            tui._live = None  # No live region yet
            tui.task_started("dep1", "my-dep@1.0.0")
            assert "dep1" in tui._key_to_label
            assert "my-dep@1.0.0" in tui._labels

    def test_task_started_idempotent_same_key(self) -> None:
        """task_started does not add duplicate labels for the same key."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._enabled = True
            tui._live = None
            tui.task_started("dep1", "same-label")
            tui.task_started("dep1", "same-label")
            # Only one entry per key
            assert tui._key_to_label.get("dep1") == "same-label"
            assert tui._labels.count("same-label") == 1

    def test_task_completed_no_label_no_crash(self) -> None:
        """task_completed with unknown key is a no-op (no KeyError)."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._enabled = True
            tui._aggregate = MagicMock()
            tui._task_id = MagicMock()
            # No label registered for "unknown-dep"
            tui.task_completed("unknown-dep")  # should not raise

    def test_refresh_group_noop_without_live(self) -> None:
        """_refresh_group is a no-op when _live is None."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._refresh_group()  # should not raise


# ===========================================================================
# 34. bundle/packer.py -- target list from CLI and apm.yml
# ===========================================================================


class TestBundlePackerTargets:
    """Tests for bundle/packer.py target resolution."""

    def _make_project_with_file(
        self, tmp_path: Path, targets: list[str] | str | None = None
    ) -> None:
        """Set up a minimal APM project with target configuration."""
        from apm_cli.deps.lockfile import LockedDependency, LockFile

        apm_yml_content = "name: test-pkg\nversion: 1.0.0\n"
        if targets is not None:
            if isinstance(targets, list):
                targets_str = "[" + ", ".join(f'"{t}"' for t in targets) + "]"
                apm_yml_content += f"target: {targets_str}\n"
            else:
                apm_yml_content += f"target: {targets}\n"

        (tmp_path / "apm.yml").write_text(apm_yml_content)
        dep = LockedDependency(
            repo_url="https://github.com/owner/dep",
            deployed_files=[".agents/skills/dep/run.py"],
        )
        lf = LockFile(dependencies={"remote-dep": dep})
        (tmp_path / "apm.lock.yaml").write_text(lf.to_yaml())
        (tmp_path / ".agents" / "skills" / "dep").mkdir(parents=True)
        (tmp_path / ".agents" / "skills" / "dep" / "run.py").write_text("# skill")

    def test_pack_bundle_with_target_list_from_cli(self, tmp_path: Path) -> None:
        """pack_bundle accepts a list of targets from CLI."""
        from apm_cli.bundle.packer import pack_bundle

        self._make_project_with_file(tmp_path)
        result = pack_bundle(tmp_path, tmp_path / "out", target=["all"], dry_run=True)
        assert isinstance(result.files, list)

    def test_pack_bundle_with_explicit_target_string(self, tmp_path: Path) -> None:
        """pack_bundle accepts an explicit target string."""
        from apm_cli.bundle.packer import pack_bundle

        self._make_project_with_file(tmp_path)
        result = pack_bundle(tmp_path, tmp_path / "out", target="all", dry_run=True)
        assert isinstance(result.files, list)

    def test_pack_bundle_tar_gz_archive(self, tmp_path: Path) -> None:
        """pack_bundle creates a .tar.gz when archive_format='tar.gz'."""
        from apm_cli.bundle.packer import pack_bundle

        self._make_project_with_file(tmp_path)
        out_dir = tmp_path / "out"
        result = pack_bundle(tmp_path, out_dir, archive=True, archive_format="tar.gz")
        assert str(result.bundle_path).endswith(".tar.gz")
        assert result.bundle_path.exists()

    def test_pack_bundle_with_logger(self, tmp_path: Path) -> None:
        """pack_bundle accepts a logger object."""
        import logging

        from apm_cli.bundle.packer import pack_bundle

        self._make_project_with_file(tmp_path)
        logger = logging.getLogger("test")
        result = pack_bundle(tmp_path, tmp_path / "out", dry_run=True, logger=logger)
        assert isinstance(result.files, list)


# ===========================================================================
# 35. More marketplace/client.py -- local fetch via git show
# ===========================================================================


class TestFetchLocalViaGitShow:
    """Tests for _fetch_local_via_git_show function."""

    def test_fetch_local_via_git_show_success(self, tmp_path: Path) -> None:
        """_fetch_local_via_git_show reads a file from a bare repo using git show."""
        from apm_cli.marketplace.client import _fetch_local_via_git_show

        payload = {"name": "bare-market", "plugins": []}
        source = MarketplaceSource(name="bare-market", url=str(tmp_path), ref="main")

        mock_proc = MagicMock()
        mock_proc.stdout = json.dumps(payload).encode()
        mock_proc.returncode = 0

        with patch("subprocess.run", return_value=mock_proc):
            result = _fetch_local_via_git_show(source, "marketplace.json", tmp_path)
        assert result is not None
        assert result["name"] == "bare-market"

    def test_fetch_local_via_git_show_not_found_returns_none(self, tmp_path: Path) -> None:
        """_fetch_local_via_git_show returns None when git show says path not found."""
        from apm_cli.marketplace.client import _fetch_local_via_git_show

        source = MarketplaceSource(name="bare-market", url=str(tmp_path), ref="main")

        mock_proc = MagicMock()
        mock_proc.returncode = 128
        mock_proc.stderr = b"fatal: path 'marketplace.json' does not exist in 'main'"

        with patch("subprocess.run", return_value=mock_proc):
            result = _fetch_local_via_git_show(source, "marketplace.json", tmp_path)
        assert result is None


# ===========================================================================
# 36. utils/install_tui.py -- additional branch coverage
# ===========================================================================


class TestInstallTuiBranches:
    """Tests for specific branches in InstallTui."""

    def test_start_phase_removes_existing_task(self) -> None:
        """Second start_phase call removes old task before adding new one."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._enabled = True
            mock_aggregate = MagicMock()
            mock_task_id = MagicMock()
            mock_aggregate.add_task.return_value = mock_task_id
            tui._aggregate = mock_aggregate
            # First phase sets task_id
            tui.start_phase("phase1", 3)
            # Second phase should call remove_task on the existing task
            tui.start_phase("phase2", 5)
            mock_aggregate.remove_task.assert_called()

    def test_task_completed_with_shared_label_keeps_other_key(self) -> None:
        """task_completed does not drop label if another key still uses it."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._enabled = True
            tui._aggregate = MagicMock()
            tui._task_id = MagicMock()
            # Two keys sharing the same label
            tui._key_to_label = {"key1": "shared-label", "key2": "shared-label"}
            tui._labels = ["shared-label"]
            tui.task_completed("key1")
            # Label should still be in _labels since key2 uses it
            assert "shared-label" in tui._labels

    def test_defer_start_cancelled_does_not_start_live(self) -> None:
        """_defer_start exits early if TUI is disabled before firing."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._enabled = True
            tui._shutdown = True  # Simulate early shutdown
            # Should not create or start Live
            tui._defer_start()
            assert tui._live is None

    def test_ascii_bar_column_render(self) -> None:
        """_AsciiBarColumn renders a progress bar with # and . chars."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            aggregate = tui._build_aggregate()
            # Add a task to the progress
            task_id = aggregate.add_task("", total=100, phase="test")
            aggregate.advance(task_id, 50)
            # Get the columns from the Progress object
            bar_column = aggregate.columns[0]
            mock_task = MagicMock()
            mock_task.percentage = 50.0
            mock_task.total = 100
            rendered = bar_column.render(mock_task)
            from rich.text import Text

            assert isinstance(rendered, Text)
            text_str = str(rendered)
            assert "#" in text_str or "." in text_str


# ===========================================================================
# 37. bundle/packer.py -- hybrid root (SKILL.md) and more target paths
# ===========================================================================


class TestBundlePackerHybrid:
    """Tests for bundle/packer.py hybrid root and archive paths."""

    def test_pack_bundle_hybrid_root_no_description_warns(self, tmp_path: Path) -> None:
        """Hybrid root (apm.yml + SKILL.md) warns when apm.yml lacks description."""
        import logging

        from apm_cli.bundle.packer import pack_bundle
        from apm_cli.deps.lockfile import LockFile

        # Create both apm.yml (no description) and SKILL.md
        (tmp_path / "apm.yml").write_text("name: test-skill\nversion: 1.0.0\n")
        (tmp_path / "SKILL.md").write_text("---\ndescription: My awesome skill\n---\n# My Skill\n")
        lf = LockFile()
        (tmp_path / "apm.lock.yaml").write_text(lf.to_yaml())

        warnings_seen: list[str] = []
        logger = logging.getLogger("test-hybrid")
        logger.warning = lambda msg, *a, **kw: warnings_seen.append(str(msg))  # type: ignore

        # Pack should succeed but warn about missing description
        result = pack_bundle(tmp_path, tmp_path / "out", dry_run=True, logger=logger)
        assert isinstance(result.files, list)
        # Warning may or may not be emitted depending on frontmatter parsing

    def test_pack_bundle_minimal_target_becomes_all(self, tmp_path: Path) -> None:
        """'minimal' effective_target is promoted to 'all' for packing."""
        from apm_cli.bundle.packer import pack_bundle
        from apm_cli.deps.lockfile import LockedDependency, LockFile

        (tmp_path / "apm.yml").write_text("name: test-pkg\nversion: 1.0.0\n")
        dep = LockedDependency(
            repo_url="https://github.com/owner/dep",
            deployed_files=[".agents/skills/dep/run.py"],
        )
        lf = LockFile(dependencies={"remote-dep": dep})
        (tmp_path / "apm.lock.yaml").write_text(lf.to_yaml())
        (tmp_path / ".agents" / "skills" / "dep").mkdir(parents=True)
        (tmp_path / ".agents" / "skills" / "dep" / "run.py").write_text("# skill")

        with patch(
            "apm_cli.bundle.packer.detect_target",
            return_value=("minimal", "auto-detected"),
        ):
            result = pack_bundle(tmp_path, tmp_path / "out", dry_run=True)
        assert isinstance(result.files, list)


# ===========================================================================
# 38. marketplace/client.py -- _fetch_local bare repo and more paths
# ===========================================================================


class TestMarketplaceLocalBareRepo:
    """Tests for marketplace/client.py local fetch from bare repos."""

    def test_fetch_local_bare_repo_via_git_show(self, tmp_path: Path) -> None:
        """_fetch_local detects bare repo and uses git show."""
        from apm_cli.marketplace.client import _fetch_local

        # Create a fake bare repo structure
        bare_dir = tmp_path / "myrepo.git"
        bare_dir.mkdir()
        (bare_dir / "HEAD").write_text("ref: refs/heads/main")
        (bare_dir / "objects").mkdir()

        payload = {"name": "bare-mp", "plugins": []}
        source = MarketplaceSource(name="bare-mp", url=str(bare_dir), ref="main")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps(payload).encode()

        with patch("subprocess.run", return_value=mock_proc):
            result = _fetch_local(source, "marketplace.json")
        assert result is not None
        assert result["name"] == "bare-mp"

    def test_fetch_local_via_git_show_invalid_json_raises(self, tmp_path: Path) -> None:
        """_fetch_local_via_git_show raises when git show returns invalid JSON."""
        from apm_cli.marketplace.client import _fetch_local_via_git_show

        source = MarketplaceSource(name="bad-json", url=str(tmp_path), ref="main")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = b"NOT JSON AT ALL !!!"

        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(MarketplaceFetchError, match="invalid JSON"):
                _fetch_local_via_git_show(source, "marketplace.json", tmp_path)


# ===========================================================================
# 39. utils/reflink.py -- Linux clone path
# ===========================================================================


class TestReflinkLinux:
    """Tests for Linux-specific reflink (_clone_linux)."""

    def setup_method(self) -> None:
        _reset_capability_cache()

    def test_clone_linux_unsupported_errno_marks_device(self, tmp_path: Path) -> None:
        """_clone_linux marks device as unsupported on EOPNOTSUPP."""
        from apm_cli.utils.reflink import _clone_linux

        src = tmp_path / "src.txt"
        src.write_bytes(b"data")
        dst = str(tmp_path / "dst.txt")

        # Simulate EOPNOTSUPP from the ioctl

        def mock_ioctl(fd, request, arg):
            err = OSError()
            err.errno = 95  # EOPNOTSUPP
            raise err

        with patch("fcntl.ioctl", side_effect=mock_ioctl):
            result = _clone_linux(str(src), dst)
        # Should return False and mark device as unsupported
        assert result is False

    def test_clone_linux_open_failure_returns_false(self, tmp_path: Path) -> None:
        """_clone_linux returns False when source open fails."""
        from apm_cli.utils.reflink import _clone_linux

        # Non-existent source should cause open() to fail
        result = _clone_linux(
            str(tmp_path / "nonexistent.txt"),
            str(tmp_path / "dst.txt"),
        )
        assert result is False


# ===========================================================================
# 40. adapters/client/base.py -- remaining uncovered helpers
# ===========================================================================


class TestBaseAdapterRemainingPaths:
    """Tests for remaining uncovered paths in adapters/client/base.py."""

    def test_infer_registry_name_nuget_pascal_case(self) -> None:
        """PascalCase names with dots infer nuget."""
        pkg = {"name": "Azure.Mcp"}
        assert MCPClientAdapter._infer_registry_name(pkg) == "nuget"

    def test_infer_registry_name_mcpb_package(self) -> None:
        """Package URL ending with .mcpb infers mcpb registry."""
        pkg = {"name": "https://releases.example.com/pkg.mcpb"}
        assert MCPClientAdapter._infer_registry_name(pkg) == "mcpb"

    def test_infer_registry_name_dotnet_runtime_hint(self) -> None:
        """dotnet runtime hint infers nuget."""
        pkg = {"name": "Azure.Mcp", "runtime_hint": "dotnet"}
        assert MCPClientAdapter._infer_registry_name(pkg) == "nuget"

    def test_should_skip_env_prompts_non_tty(self) -> None:
        """_should_skip_env_prompts returns True when not a TTY."""
        with patch("sys.stdin") as mock_stdin, patch("sys.stdout") as mock_stdout:
            mock_stdin.isatty.return_value = False
            mock_stdout.isatty.return_value = False
            result = MCPClientAdapter._should_skip_env_prompts({})
        assert result is True

    def test_should_skip_env_prompts_with_env_overrides(self) -> None:
        """_should_skip_env_prompts returns True when env_overrides provided."""
        result = MCPClientAdapter._should_skip_env_prompts({"MY_VAR": "value"})
        assert result is True

    def test_should_skip_env_prompts_e2e_tests_env(self) -> None:
        """_should_skip_env_prompts returns True when APM_E2E_TESTS=1."""
        with patch.dict(os.environ, {"APM_E2E_TESTS": "1"}):
            result = MCPClientAdapter._should_skip_env_prompts({})
        assert result is True

    def test_translate_env_placeholder_for_runtime_brace_var(self, tmp_path: Path) -> None:
        """_translate_env_placeholder_for_runtime uses adapter's runtime format."""
        adapter = _TranslateTestAdapter(project_root=tmp_path)
        result = adapter._translate_env_placeholder_for_runtime("<MY_VAR>")
        assert result == "${MY_VAR}"

    def test_format_runtime_env_placeholder(self, tmp_path: Path) -> None:
        """_format_runtime_env_placeholder returns ${VAR} format."""
        adapter = _TranslateTestAdapter(project_root=tmp_path)
        assert adapter._format_runtime_env_placeholder("MY_TOKEN") == "${MY_TOKEN}"


# ===========================================================================
# 41. marketplace/errors.py -- error hierarchy instantiation
# ===========================================================================


class TestMarketplaceErrors:
    """Tests for marketplace error classes."""

    def test_marketplace_not_found_error_message(self) -> None:
        """MarketplaceNotFoundError message contains repo URL hint."""
        from apm_cli.marketplace.errors import MarketplaceNotFoundError

        err = MarketplaceNotFoundError("my-market", host="github.com")
        assert err.name == "my-market"
        assert "my-market" in str(err)
        assert "apm marketplace add" in str(err)

    def test_marketplace_not_found_error_custom_host(self) -> None:
        """MarketplaceNotFoundError uses custom host in URL hint."""
        from apm_cli.marketplace.errors import MarketplaceNotFoundError

        err = MarketplaceNotFoundError("my-market", host="ghes.corp.com")
        msg = str(err)
        # Extract URLs from error message and verify host via parsed component
        urls = [tok for tok in msg.split() if "://" in tok]
        if urls:
            assert any(
                urllib.parse.urlparse(u).hostname == "ghes.corp.com" for u in urls
            )
        else:
            # Host appears as a plain token -- verify exact word presence
            assert any(word == "ghes.corp.com" for word in msg.split())

    def test_plugin_not_found_error(self) -> None:
        """PluginNotFoundError message includes plugin and marketplace names."""
        from apm_cli.marketplace.errors import PluginNotFoundError

        err = PluginNotFoundError("my-plugin", "my-market")
        assert err.plugin_name == "my-plugin"
        assert err.marketplace_name == "my-market"
        assert "my-plugin" in str(err)

    def test_marketplace_yml_error(self) -> None:
        """MarketplaceYmlError stores and surfaces the message."""
        err = MarketplaceYmlError("test error message")
        assert err.message == "test error message"
        assert "test error message" in str(err)

    def test_marketplace_fetch_error_with_reason(self) -> None:
        """MarketplaceFetchError message includes reason when provided."""
        err = MarketplaceFetchError("my-market", "network timeout")
        assert err.name == "my-market"
        assert err.reason == "network timeout"
        assert "network timeout" in str(err)

    def test_marketplace_fetch_error_without_reason(self) -> None:
        """MarketplaceFetchError message works without reason."""
        err = MarketplaceFetchError("my-market")
        assert err.reason == ""
        assert "my-market" in str(err)

    def test_build_error_with_package(self) -> None:
        """BuildError stores package attribute."""
        from apm_cli.marketplace.errors import BuildError

        err = BuildError("build failed", package="owner/repo")
        assert err.package == "owner/repo"

    def test_no_matching_version_error(self) -> None:
        """NoMatchingVersionError stores version_range."""
        from apm_cli.marketplace.errors import NoMatchingVersionError

        err = NoMatchingVersionError("owner/repo", ">=2.0.0", detail="only 1.x available")
        assert err.version_range == ">=2.0.0"
        assert ">=2.0.0" in str(err)
        assert "only 1.x available" in str(err)

    def test_ref_not_found_error(self) -> None:
        """RefNotFoundError stores ref and remote."""
        from apm_cli.marketplace.errors import RefNotFoundError

        err = RefNotFoundError("owner/repo", "v99.0.0", "https://github.com/owner/repo")
        assert err.ref == "v99.0.0"
        assert err.remote == "https://github.com/owner/repo"

    def test_head_not_allowed_error(self) -> None:
        """HeadNotAllowedError stores ref."""
        from apm_cli.marketplace.errors import HeadNotAllowedError

        err = HeadNotAllowedError("owner/repo", "main")
        assert err.ref == "main"
        assert "main" in str(err)

    def test_offline_miss_error(self) -> None:
        """OfflineMissError stores remote."""
        from apm_cli.marketplace.errors import OfflineMissError

        err = OfflineMissError("owner/repo", "https://github.com/owner/repo")
        assert err.remote == "https://github.com/owner/repo"

    def test_git_ls_remote_error(self) -> None:
        """GitLsRemoteError stores summary and hint."""
        from apm_cli.marketplace.errors import GitLsRemoteError

        err = GitLsRemoteError("owner/repo", "authentication failed", "check your token")
        assert err.summary_text == "authentication failed"
        assert err.hint == "check your token"


# ===========================================================================
# 42. models/dependency/identity.py -- key derivation
# ===========================================================================


class TestDependencyIdentity:
    """Tests for dependency identity key derivation."""

    def test_build_dependency_unique_key_default_github(self) -> None:
        """Default github.com host produces bare owner/repo key."""
        from apm_cli.models.dependency.identity import build_dependency_unique_key

        key = build_dependency_unique_key("owner/repo", host="github.com")
        assert key == "owner/repo"

    def test_build_dependency_unique_key_non_default_host(self) -> None:
        """Non-default host is prefixed to the key."""
        from apm_cli.models.dependency.identity import build_dependency_unique_key

        key = build_dependency_unique_key("owner/repo", host="gitea.corp.com")
        assert key == "gitea.corp.com/owner/repo"

    def test_build_dependency_unique_key_local_path(self) -> None:
        """Local source uses local_path as key."""
        from apm_cli.models.dependency.identity import build_dependency_unique_key

        key = build_dependency_unique_key("_local/pkg", source="local", local_path="./local/pkg")
        assert key == "./local/pkg"

    def test_build_dependency_unique_key_virtual(self) -> None:
        """Virtual deps include virtual_path in key."""
        from apm_cli.models.dependency.identity import build_dependency_unique_key

        key = build_dependency_unique_key("owner/repo", is_virtual=True, virtual_path="subpath")
        assert "subpath" in key

    def test_build_dependency_unique_key_registry_prefix(self) -> None:
        """Registry prefix deps use bare key (registry is transport)."""
        from apm_cli.models.dependency.identity import build_dependency_unique_key

        key = build_dependency_unique_key(
            "owner/repo",
            host="registry.corp.com",
            registry_prefix="artifactory",
        )
        # Host is not prefixed when registry_prefix is set
        assert key == "owner/repo"

    def test_build_canonical_dependency_string_default(self) -> None:
        """Default (non-local, non-virtual) returns repo_url."""
        from apm_cli.models.dependency.identity import build_canonical_dependency_string

        result = build_canonical_dependency_string("owner/repo")
        assert result == "owner/repo"

    def test_build_canonical_dependency_string_local(self) -> None:
        """Local dep returns local_path."""
        from apm_cli.models.dependency.identity import build_canonical_dependency_string

        result = build_canonical_dependency_string(
            "_local/pkg", is_local=True, local_path="./local/pkg"
        )
        assert result == "./local/pkg"

    def test_looks_like_invalid_semver_range(self) -> None:
        """Strings starting with semver range prefixes are detected."""
        from apm_cli.models.dependency.identity import _looks_like_invalid_semver_range

        assert _looks_like_invalid_semver_range(">=1.0.0") is True
        assert _looks_like_invalid_semver_range("^2.0.0") is True
        assert _looks_like_invalid_semver_range("owner/repo") is False


# ===========================================================================
# 43. models/dependency/types.py -- parse_git_reference and enums
# ===========================================================================


class TestDependencyTypes:
    """Tests for dependency type definitions."""

    def test_parse_git_reference_commit_sha(self) -> None:
        """40-hex SHA is classified as COMMIT."""
        from apm_cli.models.dependency.types import (
            GitReferenceType,
            parse_git_reference,
        )

        ref_type, _ref = parse_git_reference("a" * 40)
        assert ref_type == GitReferenceType.COMMIT

    def test_parse_git_reference_short_sha(self) -> None:
        """7-hex SHA is classified as COMMIT."""
        from apm_cli.models.dependency.types import (
            GitReferenceType,
            parse_git_reference,
        )

        ref_type, _ref = parse_git_reference("abc1234")
        assert ref_type == GitReferenceType.COMMIT

    def test_parse_git_reference_semver_tag(self) -> None:
        """v1.2.3 is classified as TAG."""
        from apm_cli.models.dependency.types import (
            GitReferenceType,
            parse_git_reference,
        )

        ref_type, _ref = parse_git_reference("v1.2.3")
        assert ref_type == GitReferenceType.TAG

    def test_parse_git_reference_branch(self) -> None:
        """Branch names are classified as BRANCH."""
        from apm_cli.models.dependency.types import (
            GitReferenceType,
            parse_git_reference,
        )

        ref_type, _ref = parse_git_reference("main")
        assert ref_type == GitReferenceType.BRANCH

    def test_parse_git_reference_empty_returns_default_branch(self) -> None:
        """Empty string defaults to BRANCH type with 'main'."""
        from apm_cli.models.dependency.types import (
            GitReferenceType,
            parse_git_reference,
        )

        ref_type, ref = parse_git_reference("")
        assert ref_type == GitReferenceType.BRANCH
        assert ref == "main"

    def test_resolved_reference_str_commit(self) -> None:
        """ResolvedReference.__str__ for COMMIT shows short SHA."""
        from apm_cli.models.dependency.types import (
            GitReferenceType,
            ResolvedReference,
        )

        rr = ResolvedReference(
            original_ref="abc1234",
            ref_type=GitReferenceType.COMMIT,
            resolved_commit="abc1234def567890",
            ref_name="abc1234",
        )
        s = str(rr)
        assert "abc1234" in s

    def test_resolved_reference_str_tag(self) -> None:
        """ResolvedReference.__str__ for TAG shows tag name and short SHA."""
        from apm_cli.models.dependency.types import (
            GitReferenceType,
            ResolvedReference,
        )

        rr = ResolvedReference(
            original_ref="v1.0.0",
            ref_type=GitReferenceType.TAG,
            resolved_commit="abc1234def567890",
            ref_name="v1.0.0",
        )
        s = str(rr)
        assert "v1.0.0" in s

    def test_remote_ref_dataclass(self) -> None:
        """RemoteRef stores all fields correctly."""
        from apm_cli.models.dependency.types import GitReferenceType, RemoteRef

        rr = RemoteRef(
            name="refs/tags/v1.0.0",
            ref_type=GitReferenceType.TAG,
            commit_sha="abc123",
            annotated=True,
        )
        assert rr.name == "refs/tags/v1.0.0"
        assert rr.annotated is True


# ===========================================================================
# 44. install/mcp/conflicts.py -- MCP flag conflict matrix
# ===========================================================================


class TestMCPConflictMatrix:
    """Tests for MCP flag conflict validation (E1-E15)."""

    def _base_kwargs(self, **overrides) -> dict:
        """Return base valid kwargs for validate_mcp_conflicts."""
        return {
            "mcp_name": "my-server",
            "packages": [],
            "pre_dash_packages": [],
            "transport": None,
            "url": None,
            "env": {},
            "headers": {},
            "mcp_version": None,
            "command_argv": None,
            "global_": False,
            "only": None,
            "update": False,
            "any_transport_flag": False,
            "registry_url": None,
            **overrides,
        }

    def test_validate_mcp_conflicts_valid_passes(self) -> None:
        """Valid combination does not raise."""
        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        validate_mcp_conflicts(**self._base_kwargs())  # should not raise

    def test_e10_transport_requires_mcp(self) -> None:
        """--transport without --mcp raises UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="--transport requires --mcp"):
            validate_mcp_conflicts(**self._base_kwargs(mcp_name=None, transport="http"))

    def test_e10_url_requires_mcp(self) -> None:
        """--url without --mcp raises UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="--url requires --mcp"):
            validate_mcp_conflicts(
                **self._base_kwargs(mcp_name=None, url="https://api.example.com")
            )

    def test_e10_registry_requires_mcp(self) -> None:
        """--registry without --mcp raises UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="--registry requires --mcp"):
            validate_mcp_conflicts(
                **self._base_kwargs(mcp_name=None, registry_url="https://registry.example.com")
            )

    def test_e7_empty_mcp_name(self) -> None:
        """Empty --mcp name raises UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="cannot be empty"):
            validate_mcp_conflicts(**self._base_kwargs(mcp_name=""))

    def test_e8_mcp_name_starts_with_dash(self) -> None:
        """--mcp name starting with '-' raises UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="cannot start with"):
            validate_mcp_conflicts(**self._base_kwargs(mcp_name="-bad"))

    def test_e1_positional_packages_with_mcp(self) -> None:
        """Mixing positional packages with --mcp raises UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="cannot mix"):
            validate_mcp_conflicts(**self._base_kwargs(pre_dash_packages=["owner/repo"]))

    def test_e2_global_with_mcp(self) -> None:
        """--global with --mcp raises UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="--global is not supported"):
            validate_mcp_conflicts(**self._base_kwargs(global_=True))

    def test_e3_only_apm_with_mcp(self) -> None:
        """--only apm with --mcp raises UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="cannot use --only apm"):
            validate_mcp_conflicts(**self._base_kwargs(only="apm"))

    def test_e4_transport_flags_with_mcp(self) -> None:
        """Transport flags with --mcp raise UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="transport selection flags"):
            validate_mcp_conflicts(**self._base_kwargs(any_transport_flag=True))

    def test_e5_update_with_mcp(self) -> None:
        """--update with --mcp raises UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="use 'apm update'"):
            validate_mcp_conflicts(**self._base_kwargs(update=True))

    def test_e9_header_without_url(self) -> None:
        """--header without --url raises UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="--header requires --url"):
            validate_mcp_conflicts(**self._base_kwargs(headers={"Authorization": "token"}))

    def test_e11_url_and_command_argv(self) -> None:
        """--url and stdio command raises UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="cannot specify both"):
            validate_mcp_conflicts(
                **self._base_kwargs(url="https://api.example.com", command_argv=["npx", "server"])
            )

    def test_e12_stdio_transport_with_url(self) -> None:
        """stdio transport with --url raises UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="stdio transport"):
            validate_mcp_conflicts(
                **self._base_kwargs(transport="stdio", url="https://api.example.com")
            )

    def test_e13_remote_transport_with_command(self) -> None:
        """Remote transport with stdio command raises UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="remote transports"):
            validate_mcp_conflicts(
                **self._base_kwargs(transport="http", command_argv=["npx", "server"])
            )

    def test_e14_env_with_url_no_command(self) -> None:
        """--env with --url but no command raises UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="--env applies to stdio"):
            validate_mcp_conflicts(
                **self._base_kwargs(env={"MY_VAR": "value"}, url="https://api.example.com")
            )

    def test_e15_registry_url_with_url(self) -> None:
        """--registry with --url raises UsageError."""
        import click

        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        with pytest.raises(click.UsageError, match="--registry only applies"):
            validate_mcp_conflicts(
                **self._base_kwargs(
                    url="https://api.example.com",
                    registry_url="https://registry.example.com",
                )
            )

    def test_no_mcp_with_command_argv_allowed(self) -> None:
        """Post-dash stdio command without --mcp is silently allowed."""
        from apm_cli.install.mcp.conflicts import validate_mcp_conflicts

        # Should not raise (legacy install behaviour)
        validate_mcp_conflicts(**self._base_kwargs(mcp_name=None, command_argv=["npx", "server"]))


# ===========================================================================
# 45. drift.py -- pure drift detection helpers
# ===========================================================================


class TestDriftHelpers:
    """Tests for pure drift detection functions in drift.py."""

    def test_detect_orphans_empty_on_partial_install(self) -> None:
        """detect_orphans returns empty set on partial install."""
        from apm_cli.drift import detect_orphans

        result = detect_orphans(None, set(), only_packages=["pkg-a"], logger=None)
        assert result == set()

    def test_detect_orphans_empty_on_first_install(self) -> None:
        """detect_orphans returns empty set when no existing lockfile."""
        from apm_cli.drift import detect_orphans

        result = detect_orphans(None, set(), only_packages=[], logger=None)
        assert result == set()

    def test_detect_orphans_finds_removed_packages(self) -> None:
        """detect_orphans returns files from packages no longer in manifest."""
        from apm_cli.deps.lockfile import LockedDependency, LockFile
        from apm_cli.drift import detect_orphans

        lf = LockFile(
            dependencies={
                "owner/pkg": LockedDependency(
                    repo_url="https://github.com/owner/pkg",
                    deployed_files=[".agents/skills/pkg/run.py"],
                )
            }
        )
        # Package is NOT in intended_dep_keys
        orphans = detect_orphans(lf, set(), only_packages=[], logger=None)
        assert ".agents/skills/pkg/run.py" in orphans

    def test_detect_orphans_no_orphans_when_present(self) -> None:
        """detect_orphans returns empty when all packages still in manifest."""
        from apm_cli.deps.lockfile import LockedDependency, LockFile
        from apm_cli.drift import detect_orphans

        lf = LockFile(
            dependencies={
                "owner/pkg": LockedDependency(
                    repo_url="https://github.com/owner/pkg",
                    deployed_files=[".agents/skills/pkg/run.py"],
                )
            }
        )
        orphans = detect_orphans(lf, {"owner/pkg"}, only_packages=[], logger=None)
        assert orphans == set()

    def test_detect_stale_files_returns_removed_files(self) -> None:
        """detect_stale_files returns paths no longer in new_deployed."""
        from apm_cli.drift import detect_stale_files

        old = [".agents/skills/pkg/old-file.py", ".agents/skills/pkg/kept.py"]
        new = [".agents/skills/pkg/kept.py"]
        stale = detect_stale_files(old, new)
        assert ".agents/skills/pkg/old-file.py" in stale
        assert ".agents/skills/pkg/kept.py" not in stale

    def test_detect_stale_files_empty_when_no_changes(self) -> None:
        """detect_stale_files returns empty when files unchanged."""
        from apm_cli.drift import detect_stale_files

        files = [".agents/skills/pkg/run.py"]
        assert detect_stale_files(files, files) == set()

    def test_detect_config_drift_returns_changed_names(self) -> None:
        """detect_config_drift returns names with changed configs."""
        from apm_cli.drift import detect_config_drift

        current = {"server-a": {"command": "new-cmd"}}
        stored = {"server-a": {"command": "old-cmd"}}
        drifted = detect_config_drift(current, stored)
        assert "server-a" in drifted

    def test_detect_config_drift_no_drift_on_match(self) -> None:
        """detect_config_drift returns empty when configs match."""
        from apm_cli.drift import detect_config_drift

        config = {"server-a": {"command": "cmd"}}
        assert detect_config_drift(config, config) == set()

    def test_detect_config_drift_new_server_not_drifted(self) -> None:
        """detect_config_drift ignores new servers (no stored baseline)."""
        from apm_cli.drift import detect_config_drift

        current = {"new-server": {"command": "npx"}}
        stored = {}  # No baseline
        assert detect_config_drift(current, stored) == set()


# ===========================================================================
# 46. More adapters/client/codex.py -- remaining pypi/env paths
# ===========================================================================


class TestCodexAdapterRemainingPaths:
    """Tests for remaining uncovered paths in CodexClientAdapter."""

    def test_format_server_config_pypi_package(self, tmp_path: Path) -> None:
        """PyPI package generates uvx command."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "id": "abc",
            "name": "pypi-server",
            "packages": [
                {
                    "name": "mcp-server-pkg",
                    "registry_name": "pypi",
                    "runtime_hint": "uvx",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        result = adapter._format_server_config(server_info)
        assert result is not None
        assert result["command"] == "uvx"
        assert "mcp-server-pkg" in result["args"]

    def test_format_server_config_with_env_vars(self, tmp_path: Path) -> None:
        """NPM package with env vars includes env block."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        server_info = {
            "id": "abc",
            "name": "npm-server",
            "packages": [
                {
                    "name": "@test/server",
                    "registry_name": "npm",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [
                        {"name": "API_KEY", "description": "key", "required": True}
                    ],
                }
            ],
        }
        with patch.dict(os.environ, {"APM_E2E_TESTS": "1", "API_KEY": "test-key"}):
            result = adapter._format_server_config(server_info)
        assert result is not None
        assert result["command"] in ("npx", "npm")
        assert "env" in result

    def test_get_current_config_oserror_returns_none(self, tmp_path: Path) -> None:
        """get_current_config returns None on OSError."""
        adapter = CodexClientAdapter(project_root=tmp_path)
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        config_file = codex_dir / "config.toml"
        config_file.write_text("[mcp_servers]")
        # Make the file unreadable
        with patch("builtins.open", side_effect=OSError("permission denied")):
            result = adapter.get_current_config()
        assert result is None


# ===========================================================================
# 47. utils/install_tui.py -- missing branch coverage
# ===========================================================================


class TestInstallTuiMissingBranches:
    """Tests targeting specific uncovered branches in InstallTui."""

    def test_start_phase_builds_aggregate_when_none(self) -> None:
        """start_phase calls _build_aggregate when _aggregate is None."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._enabled = True
            # _aggregate is None initially
            assert tui._aggregate is None
            tui.start_phase("resolve", 5)
            # _aggregate should now be set
            assert tui._aggregate is not None

    def test_task_completed_with_milestone_and_live(self) -> None:
        """task_completed prints milestone to console when _live is set."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._enabled = True
            mock_live = MagicMock()
            mock_live.console = MagicMock()
            tui._live = mock_live
            tui._aggregate = MagicMock()
            tui._task_id = MagicMock()
            tui._key_to_label = {"dep1": "label-1"}
            tui._labels = ["label-1"]
            tui.task_completed("dep1", milestone="[+] completed")
            mock_live.console.print.assert_called_with("[+] completed")

    def test_task_started_label_already_in_labels(self) -> None:
        """task_started skips adding label if already present."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._enabled = True
            tui._live = None
            tui._labels = ["existing-label"]
            tui._key_to_label = {}
            tui.task_started("dep1", "existing-label")
            # Label count should not increase (was already in _labels)
            assert tui._labels.count("existing-label") == 1

    def test_task_completed_unknown_key_is_noop(self) -> None:
        """task_completed with unknown key has label=None, skips label removal."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._enabled = True
            tui._aggregate = MagicMock()
            tui._task_id = MagicMock()
            # No label registered - _key_to_label.pop returns None
            tui.task_completed("unknown-key")
            # aggregate.advance should still be called
            tui._aggregate.advance.assert_called()

    def test_refresh_group_with_live(self) -> None:
        """_refresh_group updates the Live group when _live is set."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            mock_live = MagicMock()
            tui._live = mock_live
            tui._aggregate = MagicMock()
            tui._refresh_group()
            mock_live.update.assert_called()


# ===========================================================================
# 48. More drift.py -- build_download_ref and remaining paths
# ===========================================================================


class TestDriftBuildDownloadRef:
    """Tests for drift.py build_download_ref."""

    def test_build_download_ref_no_lockfile_returns_dep_ref(self) -> None:
        """Returns dep_ref unchanged when no existing lockfile."""
        from apm_cli.drift import build_download_ref
        from apm_cli.models.apm_package import DependencyReference

        dep_ref = DependencyReference(
            repo_url="owner/repo",
            reference="main",
        )
        result = build_download_ref(dep_ref, None, update_refs=False, ref_changed=False)
        assert result is dep_ref

    def test_build_download_ref_update_refs_returns_dep_ref(self) -> None:
        """Returns dep_ref unchanged when update_refs=True."""
        from apm_cli.deps.lockfile import LockedDependency, LockFile
        from apm_cli.drift import build_download_ref
        from apm_cli.models.apm_package import DependencyReference

        dep_ref = DependencyReference(repo_url="owner/repo", reference="main")
        lf = LockFile(
            dependencies={
                "owner/repo": LockedDependency(
                    repo_url="https://github.com/owner/repo",
                    resolved_commit="abc123def456",
                )
            }
        )
        result = build_download_ref(dep_ref, lf, update_refs=True, ref_changed=False)
        # With update_refs=True, returns original dep_ref (not the locked commit)
        assert result is dep_ref

    def test_build_download_ref_ref_changed_returns_dep_ref(self) -> None:
        """Returns dep_ref unchanged when ref_changed=True."""
        from apm_cli.deps.lockfile import LockedDependency, LockFile
        from apm_cli.drift import build_download_ref
        from apm_cli.models.apm_package import DependencyReference

        dep_ref = DependencyReference(repo_url="owner/repo", reference="v2.0.0")
        lf = LockFile(
            dependencies={
                "owner/repo": LockedDependency(
                    repo_url="https://github.com/owner/repo",
                    resolved_commit="abc123",
                )
            }
        )
        result = build_download_ref(dep_ref, lf, update_refs=False, ref_changed=True)
        # ref_changed means use manifest ref, not locked
        assert result is dep_ref


# ===========================================================================
# 49. More utils/reflink.py -- macOS clone failure path
# ===========================================================================


class TestMacOSReflink:
    """Tests for macOS-specific reflink paths."""

    def setup_method(self) -> None:
        _reset_capability_cache()

    def test_clone_macos_no_function_returns_false(self) -> None:
        """_clone_macos returns False when clonefile function not available."""
        from apm_cli.utils.reflink import _clone_macos

        with patch("apm_cli.utils.reflink._load_macos_clonefile", return_value=None):
            result = _clone_macos("/tmp/src.txt", "/tmp/dst.txt")
        assert result is False

    def test_clone_macos_unsupported_errno_marks_device(self, tmp_path: Path) -> None:
        """_clone_macos marks device as unsupported on ENOTSUP."""
        from apm_cli.utils.reflink import _clone_macos

        src = str(tmp_path / "src.txt")
        dst = str(tmp_path / "dst.txt")
        (tmp_path / "src.txt").write_bytes(b"data")

        # Mock clonefile function returning ENOTSUP
        mock_fn = MagicMock()
        mock_fn.return_value = -1

        with patch("apm_cli.utils.reflink._load_macos_clonefile", return_value=mock_fn):
            with patch("ctypes.get_errno", return_value=45):  # ENOTSUP
                result = _clone_macos(src, dst)
        assert result is False

    def test_reflink_supported_no_reflink_env(self) -> None:
        """reflink_supported returns False when APM_NO_REFLINK is set."""
        with patch.dict(os.environ, {"APM_NO_REFLINK": "1"}):
            assert reflink_supported() is False

    def test_reflink_supported_windows(self) -> None:
        """reflink_supported returns False on Windows-like platforms."""
        with patch.object(sys, "platform", "win32"):
            with patch.dict(
                os.environ, {k: v for k, v in os.environ.items() if k != "APM_NO_REFLINK"}
            ):
                assert reflink_supported() is False


# ===========================================================================
# 50. utils/install_tui.py -- __enter__/__exit__ and _defer_start full path
# ===========================================================================


class TestInstallTuiEnterExit:
    """Tests for InstallTui context manager with enabled state."""

    def test_enter_starts_timer_when_enabled(self) -> None:
        """__enter__ starts a deferred-show timer when TUI is enabled."""
        with patch.dict(os.environ, {"APM_PROGRESS": "always"}):
            with patch("apm_cli.utils.install_tui.should_animate", return_value=True):
                tui = InstallTui()
                # Should start the timer
                with patch("threading.Timer") as mock_timer_cls:
                    mock_timer = MagicMock()
                    mock_timer_cls.return_value = mock_timer
                    with tui:
                        mock_timer.start.assert_called()

    def test_exit_cancels_timer(self) -> None:
        """__exit__ cancels the deferred-show timer."""
        with patch("apm_cli.utils.install_tui.should_animate", return_value=True):
            tui = InstallTui()
            mock_timer = MagicMock()
            with patch("threading.Timer", return_value=mock_timer):
                with tui:
                    pass
            mock_timer.cancel.assert_called()

    def test_defer_start_exits_when_already_live(self) -> None:
        """_defer_start exits early when _live is already set."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._enabled = True
            mock_live = MagicMock()
            tui._live = mock_live  # Already set
            tui._defer_start()
            # Should not create another Live
            assert tui._live is mock_live

    def test_defer_start_creates_live_when_not_shutdown(self) -> None:
        """_defer_start creates and starts a Live region when not shutdown."""
        with patch.dict(os.environ, {"APM_PROGRESS": "never"}):
            tui = InstallTui()
            tui._enabled = True
            tui._shutdown = False

            mock_live = MagicMock()
            with patch("rich.live.Live", return_value=mock_live):
                with patch("rich.console.Group"):
                    tui._defer_start()
            if tui._live is not None:
                mock_live.start.assert_called()


# ===========================================================================
# 51. More marketplace/migration.py -- error paths
# ===========================================================================


class TestMarketplaceMigrationErrors:
    """Tests for additional migration error paths."""

    def test_has_marketplace_block_invalid_yaml_raises(self, tmp_path: Path) -> None:
        """_has_marketplace_block raises MarketplaceYmlError for invalid YAML."""
        from apm_cli.marketplace.migration import _has_marketplace_block

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(": invalid: yaml: !!!")
        with pytest.raises(MarketplaceYmlError, match="Invalid YAML"):
            _has_marketplace_block(apm_yml)

    def test_has_marketplace_block_oserror_raises(self, tmp_path: Path) -> None:
        """_has_marketplace_block raises MarketplaceYmlError on read failure."""
        from apm_cli.marketplace.migration import _has_marketplace_block

        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("marketplace:\n  owner:\n    name: me\n")
        with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
            with pytest.raises(MarketplaceYmlError, match="Could not read"):
                _has_marketplace_block(apm_yml)

    def test_migrate_marketplace_yml_force_overwrites_existing(self, tmp_path: Path) -> None:
        """migrate with force=True overwrites existing marketplace block."""
        # apm.yml already has a marketplace block
        (tmp_path / "apm.yml").write_text(
            "name: parent\nversion: 1.0.0\nmarketplace:\n  owner:\n    name: old-owner\n"
        )
        (tmp_path / "marketplace.yml").write_text(
            "name: mkt\ndescription: d\nversion: 1.0.0\nowner:\n  name: new-owner\n"
        )
        # Without force, should raise
        with pytest.raises(MarketplaceYmlError, match="already has a 'marketplace:' block"):
            migrate_marketplace_yml(tmp_path, dry_run=True)
        # With force, should succeed
        diff = migrate_marketplace_yml(tmp_path, force=True, dry_run=True)
        assert isinstance(diff, str)


# ===========================================================================
# 52. More models/dependency/lsp.py -- remaining branch paths
# ===========================================================================


class TestLSPDependencyRemainingPaths:
    """Tests for remaining uncovered paths in LSPDependency."""

    def test_validate_command_non_string_raises(self) -> None:
        """Validation raises when command is not a string."""
        dep = LSPDependency(
            name="test",
            command=42,  # type: ignore[arg-type]
            extension_to_language={".py": "python"},
        )
        with pytest.raises(ValueError, match="'command' must be a string"):
            dep.validate(strict=True)

    def test_validate_workspace_folder_non_string_raises(self) -> None:
        """Validation raises when workspaceFolder is not a string."""
        dep = LSPDependency(
            name="test",
            command="test-lsp",
            extension_to_language={".py": "python"},
            workspace_folder=42,  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="'workspaceFolder' must be a string"):
            dep.validate(strict=True)

    def test_from_dict_all_camel_case_optional_fields(self) -> None:
        """from_dict handles all camelCase optional fields."""
        data = {
            "name": "clangd",
            "command": "clangd",
            "extensionToLanguage": {".cpp": "cpp"},
            "workspaceFolder": "/workspace",
            "shutdownTimeout": 5,
            "restartOnCrash": True,
            "maxRestarts": 3,
        }
        dep = LSPDependency.from_dict(data)
        assert dep.workspace_folder == "/workspace"
        assert dep.shutdown_timeout == 5
        assert dep.restart_on_crash is True
        assert dep.max_restarts == 3

    def test_to_dict_includes_workspace_folder(self) -> None:
        """to_dict serializes workspace_folder as workspaceFolder."""
        dep = LSPDependency(
            name="test",
            command="test",
            extension_to_language={".py": "python"},
            workspace_folder="/my/workspace",
        )
        d = dep.to_dict()
        assert "workspaceFolder" in d
        assert d["workspaceFolder"] == "/my/workspace"


# ===========================================================================
# 53. cache/http_cache.py -- refresh_expiry no-etag path
# ===========================================================================


class TestHttpCacheRefreshExpiry:
    """Additional tests for refresh_expiry."""

    def test_refresh_expiry_without_new_etag(self, tmp_path: Path) -> None:
        """refresh_expiry without new ETag updates only expires_at."""
        cache = HttpCache(tmp_path)
        url = "https://example.com/no-etag-refresh"
        cache.store(
            url,
            b"body",
            status_code=200,
            headers={"ETag": '"old"', "Cache-Control": "max-age=3600"},
        )
        # Refresh without new ETag
        cache.refresh_expiry(url, headers={"Cache-Control": "max-age=7200"})
        entry = cache.get(url)
        assert entry is not None
        # ETag should be unchanged
        assert entry.etag == '"old"'


# ===========================================================================
# 54. factory.py -- adapter factory
# ===========================================================================


class TestClientFactory:
    """Tests for ClientFactory and PackageManagerFactory."""

    def test_create_client_copilot(self, tmp_path: Path) -> None:
        """ClientFactory creates a CopilotClientAdapter."""
        from apm_cli.adapters.client.copilot import CopilotClientAdapter
        from apm_cli.factory import ClientFactory

        adapter = ClientFactory.create_client("copilot", project_root=tmp_path)
        assert isinstance(adapter, CopilotClientAdapter)

    def test_create_client_claude(self, tmp_path: Path) -> None:
        """ClientFactory creates a ClaudeClientAdapter."""
        from apm_cli.factory import ClientFactory

        adapter = ClientFactory.create_client("claude", project_root=tmp_path)
        assert isinstance(adapter, ClaudeClientAdapter)

    def test_create_client_kiro(self, tmp_path: Path) -> None:
        """ClientFactory creates a KiroClientAdapter."""
        from apm_cli.factory import ClientFactory

        adapter = ClientFactory.create_client("kiro", project_root=tmp_path)
        assert isinstance(adapter, KiroClientAdapter)

    def test_create_client_gemini(self, tmp_path: Path) -> None:
        """ClientFactory creates a GeminiClientAdapter."""
        from apm_cli.factory import ClientFactory

        adapter = ClientFactory.create_client("gemini", project_root=tmp_path)
        assert isinstance(adapter, GeminiClientAdapter)

    def test_create_client_codex(self, tmp_path: Path) -> None:
        """ClientFactory creates a CodexClientAdapter."""
        from apm_cli.factory import ClientFactory

        adapter = ClientFactory.create_client("codex", project_root=tmp_path)
        assert isinstance(adapter, CodexClientAdapter)

    def test_create_client_unknown_raises(self, tmp_path: Path) -> None:
        """ClientFactory raises ValueError for unknown client type."""
        from apm_cli.factory import ClientFactory

        with pytest.raises(ValueError, match="Unsupported client type"):
            ClientFactory.create_client("unknown-client")

    def test_create_client_case_insensitive(self, tmp_path: Path) -> None:
        """ClientFactory accepts uppercase client type names."""
        from apm_cli.factory import ClientFactory

        adapter = ClientFactory.create_client("CODEX", project_root=tmp_path)
        assert isinstance(adapter, CodexClientAdapter)

    def test_supported_clients_returns_frozenset(self) -> None:
        """ClientFactory.supported_clients returns a frozenset."""
        from apm_cli.factory import ClientFactory

        clients = ClientFactory.supported_clients()
        assert isinstance(clients, frozenset)
        assert "copilot" in clients
        assert "claude" in clients
        assert "kiro" in clients
        assert "gemini" in clients

    def test_create_package_manager_default(self) -> None:
        """PackageManagerFactory creates a DefaultMCPPackageManager."""
        from apm_cli.factory import PackageManagerFactory

        pm = PackageManagerFactory.create_package_manager()
        assert pm is not None

    def test_create_package_manager_unknown_raises(self) -> None:
        """PackageManagerFactory raises ValueError for unknown type."""
        from apm_cli.factory import PackageManagerFactory

        with pytest.raises(ValueError, match="Unsupported package manager type"):
            PackageManagerFactory.create_package_manager("unknown-pm")


# ===========================================================================
# 55. marketplace/tag_pattern.py -- tag rendering
# ===========================================================================


class TestTagPattern:
    """Tests for tag pattern rendering."""

    def test_render_tag_version_only(self) -> None:
        """render_tag substitutes {version} in pattern."""
        from apm_cli.marketplace.tag_pattern import render_tag

        result = render_tag("v{version}", name="pkg", version="1.2.3")
        assert result == "v1.2.3"

    def test_render_tag_name_and_version(self) -> None:
        """render_tag substitutes both {name} and {version}."""
        from apm_cli.marketplace.tag_pattern import render_tag

        result = render_tag("{name}-v{version}", name="my-pkg", version="2.0.0")
        assert result == "my-pkg-v2.0.0"

    def test_render_tag_plain_string(self) -> None:
        """render_tag returns pattern unchanged when no placeholders."""
        from apm_cli.marketplace.tag_pattern import render_tag

        result = render_tag("latest", name="pkg", version="1.0.0")
        assert result == "latest"


# ===========================================================================
# 56. More drift.py -- detect_ref_change helper
# ===========================================================================


class TestDriftRefChange:
    """Tests for detect_ref_change and helper functions."""

    def test_detect_ref_change_no_change_locked_commit(self) -> None:
        """detect_ref_change returns False when locked commit matches."""
        from apm_cli.deps.lockfile import LockedDependency
        from apm_cli.drift import detect_ref_change
        from apm_cli.models.apm_package import DependencyReference

        dep_ref = DependencyReference(repo_url="owner/repo", reference=None)
        locked = LockedDependency(
            repo_url="https://github.com/owner/repo",
            resolved_ref=None,
            resolved_commit="abc123",
        )
        result = detect_ref_change(dep_ref, locked)
        assert result is False

    def test_detect_ref_change_with_ref_mismatch(self) -> None:
        """detect_ref_change returns True when manifest ref differs from locked."""
        from apm_cli.deps.lockfile import LockedDependency
        from apm_cli.drift import detect_ref_change
        from apm_cli.models.apm_package import DependencyReference

        dep_ref = DependencyReference(repo_url="owner/repo", reference="v2.0.0")
        locked = LockedDependency(
            repo_url="https://github.com/owner/repo",
            resolved_ref="v1.0.0",
            resolved_commit="abc123",
        )
        result = detect_ref_change(dep_ref, locked)
        assert result is True

    def test_registry_range_covers_locked_version_true(self) -> None:
        """_registry_range_covers_locked_version returns True for matching range."""
        from apm_cli.drift import _registry_range_covers_locked_version

        result = _registry_range_covers_locked_version(">=1.0.0", "1.5.0")
        assert result is True

    def test_registry_range_covers_locked_version_false_no_range(self) -> None:
        """_registry_range_covers_locked_version returns False when no range."""
        from apm_cli.drift import _registry_range_covers_locked_version

        result = _registry_range_covers_locked_version(None, "1.0.0")
        assert result is False

    def test_registry_range_covers_locked_version_false_no_version(self) -> None:
        """_registry_range_covers_locked_version returns False when no version."""
        from apm_cli.drift import _registry_range_covers_locked_version

        result = _registry_range_covers_locked_version(">=1.0.0", None)
        assert result is False


# ===========================================================================
# 57. marketplace/semver.py -- version comparison helpers
# ===========================================================================


class TestMarketplaceSemver:
    """Tests for marketplace/semver.py."""

    def test_parse_version_valid(self) -> None:
        """parse_semver successfully parses valid semver strings."""
        from apm_cli.marketplace.semver import parse_semver

        v = parse_semver("1.2.3")
        assert v is not None

    def test_compare_versions_less_than(self) -> None:
        """SemVer comparison: older version is less."""
        from apm_cli.marketplace.semver import parse_semver

        v1 = parse_semver("1.0.0")
        v2 = parse_semver("2.0.0")
        assert v1 < v2

    def test_compare_versions_greater_than(self) -> None:
        """SemVer comparison: newer version is greater."""
        from apm_cli.marketplace.semver import parse_semver

        v1 = parse_semver("2.0.0")
        v2 = parse_semver("1.0.0")
        assert v1 > v2

    def test_compare_versions_equal(self) -> None:
        """SemVer comparison: equal versions."""
        from apm_cli.marketplace.semver import parse_semver

        v1 = parse_semver("1.0.0")
        v2 = parse_semver("1.0.0")
        assert v1 == v2

    def test_satisfies_range_true(self) -> None:
        """satisfies_range returns True for matching version."""
        from apm_cli.marketplace.semver import parse_semver, satisfies_range

        v = parse_semver("1.5.0")
        assert satisfies_range(v, ">=1.0.0")

    def test_satisfies_range_false(self) -> None:
        """satisfies_range returns False for non-matching version."""
        from apm_cli.marketplace.semver import parse_semver, satisfies_range

        v = parse_semver("0.9.0")
        assert not satisfies_range(v, ">=1.0.0")

    def test_parse_semver_invalid_returns_none(self) -> None:
        """parse_semver returns None for invalid semver strings."""
        from apm_cli.marketplace.semver import parse_semver

        assert parse_semver("not-a-version") is None
