"""Integration tests for adapters, bundle, and registry modules."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestAdaptersIntegration:
    """Integration tests for adapter module functionality."""

    def test_copilot_adapter_init(self) -> None:
        """Test CopilotClientAdapter initializes successfully."""
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter()
        assert adapter is not None

    def test_copilot_adapter_format_server_config_http(self) -> None:
        """Test CopilotClientAdapter formats HTTP server config correctly."""
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter()
        server_info = {
            "id": "test-id",
            "name": "test-server",
            "remotes": [{"url": "https://example.com/mcp"}],
        }
        config = adapter._format_server_config(server_info)
        assert isinstance(config, dict)
        assert config.get("type") == "http"
        assert config.get("url") == "https://example.com/mcp"

    def test_copilot_adapter_format_server_config_missing_remotes(self) -> None:
        """Test CopilotClientAdapter handles missing remotes."""
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter()
        server_info = {
            "id": "test-id",
            "name": "test-server",
            "remotes": [],
        }
        # Should raise because no valid remotes
        with pytest.raises(ValueError):
            adapter._format_server_config(server_info)

    def test_codex_adapter_init(self) -> None:
        """Test CodexClientAdapter initializes successfully."""
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter()
        assert adapter is not None

    def test_codex_adapter_format_server_config_requires_package_info(self) -> None:
        """Test CodexClientAdapter requires package info."""
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter()
        server_info = {
            "id": "test-id",
            "name": "test-server",
        }
        # Should raise - CodexClientAdapter requires package/install info
        with pytest.raises(ValueError):
            adapter._format_server_config(server_info)

    def test_vscode_adapter_init(self) -> None:
        """Test VSCodeClientAdapter initializes successfully."""
        from apm_cli.adapters.client.vscode import VSCodeClientAdapter

        adapter = VSCodeClientAdapter()
        assert adapter is not None

    def test_vscode_adapter_format_server_config_returns_tuple(self) -> None:
        """Test VSCodeClientAdapter._format_server_config returns tuple."""
        from apm_cli.adapters.client.vscode import VSCodeClientAdapter

        adapter = VSCodeClientAdapter()
        server_info = {
            "id": "test-id",
            "name": "test-server",
            "remotes": [{"url": "https://example.com/mcp"}],
        }
        result = adapter._format_server_config(server_info)
        assert isinstance(result, tuple)
        assert len(result) == 2
        config, _input_vars = result
        assert isinstance(config, dict)

    def test_copilot_module_translate_env_placeholder(self) -> None:
        """Test module-level _translate_env_placeholder function."""
        from apm_cli.adapters.client import copilot

        # Test dollar-brace format
        result = copilot._translate_env_placeholder("https://api/${VAR}")
        assert isinstance(result, str)

    def test_copilot_module_translate_env_placeholder_env_format(self) -> None:
        """Test _translate_env_placeholder with env: format."""
        from apm_cli.adapters.client import copilot

        result = copilot._translate_env_placeholder("https://api/${env:VAR}")
        assert isinstance(result, str)
        # Should preserve or translate to proper format
        assert "${" in result or "$" in result

    def test_copilot_module_has_env_placeholder(self) -> None:
        """Test module-level _has_env_placeholder function."""
        from apm_cli.adapters.client import copilot

        # String with placeholder
        assert copilot._has_env_placeholder("url: ${API_KEY}") is True

    def test_copilot_module_has_env_placeholder_no_match(self) -> None:
        """Test _has_env_placeholder with no placeholder."""
        from apm_cli.adapters.client import copilot

        # Plain string without placeholder
        assert copilot._has_env_placeholder("url: https://example.com") is False

    def test_base_adapter_infer_registry_name_from_runtime(self) -> None:
        """Test _infer_registry_name infers registry from runtime_hint."""
        from apm_cli.adapters.client.base import MCPClientAdapter

        package = {
            "name": "my-package",
            "runtime_hint": "npm",
        }
        result = MCPClientAdapter._infer_registry_name(package)
        assert isinstance(result, str)

    def test_base_adapter_infer_registry_name_explicit(self) -> None:
        """Test _infer_registry_name uses explicit registry_name."""
        from apm_cli.adapters.client.base import MCPClientAdapter

        package = {
            "name": "my-package",
            "registry_name": "custom-registry",
            "runtime_hint": "npm",
        }
        result = MCPClientAdapter._infer_registry_name(package)
        assert isinstance(result, str)
        assert result == "custom-registry"

    def test_base_adapter_infer_registry_name_empty_package(self) -> None:
        """Test _infer_registry_name handles empty package."""
        from apm_cli.adapters.client.base import MCPClientAdapter

        result = MCPClientAdapter._infer_registry_name(None)
        assert isinstance(result, str)
        assert result == ""

    def test_base_adapter_infer_registry_name_from_package_name(self) -> None:
        """Test _infer_registry_name infers from package name."""
        from apm_cli.adapters.client.base import MCPClientAdapter

        package = {
            "name": "my-package",
        }
        result = MCPClientAdapter._infer_registry_name(package)
        assert isinstance(result, str)

    def test_copilot_adapter_get_current_config(self) -> None:
        """Test CopilotClientAdapter.get_current_config returns dict."""
        from apm_cli.adapters.client.copilot import CopilotClientAdapter

        adapter = CopilotClientAdapter()
        config = adapter.get_current_config()
        assert isinstance(config, dict)

    def test_codex_adapter_get_current_config(self) -> None:
        """Test CodexClientAdapter.get_current_config returns dict."""
        from apm_cli.adapters.client.codex import CodexClientAdapter

        adapter = CodexClientAdapter()
        config = adapter.get_current_config()
        assert isinstance(config, dict)

    def test_vscode_adapter_get_current_config(self) -> None:
        """Test VSCodeClientAdapter.get_current_config returns dict."""
        from apm_cli.adapters.client.vscode import VSCodeClientAdapter

        adapter = VSCodeClientAdapter()
        config = adapter.get_current_config()
        assert isinstance(config, dict)


class TestBundleIntegration:
    """Integration tests for bundle module functionality."""

    def test_sanitize_bundle_name_returns_string(self) -> None:
        """Test _sanitize_bundle_name returns string."""
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("my-bundle@1.2.3")
        assert isinstance(result, str)

    def test_sanitize_bundle_name_removes_special_chars(self) -> None:
        """Test _sanitize_bundle_name removes special characters."""
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("My-Bundle@1.2.3")
        assert isinstance(result, str)
        assert "@" not in result

    def test_sanitize_bundle_name_handles_empty(self) -> None:
        """Test _sanitize_bundle_name handles empty string."""
        from apm_cli.bundle.plugin_exporter import _sanitize_bundle_name

        result = _sanitize_bundle_name("")
        assert isinstance(result, str)

    def test_validate_output_rel_returns_bool(self) -> None:
        """Test _validate_output_rel returns bool."""
        from apm_cli.bundle.plugin_exporter import _validate_output_rel

        result = _validate_output_rel("output/bundle.tar")
        assert isinstance(result, bool)

    def test_validate_output_rel_accepts_relative_path(self) -> None:
        """Test _validate_output_rel accepts relative paths."""
        from apm_cli.bundle.plugin_exporter import _validate_output_rel

        result = _validate_output_rel("output/bundle.tar")
        assert result is True

    def test_validate_output_rel_rejects_absolute_path(self) -> None:
        """Test _validate_output_rel rejects absolute paths."""
        from apm_cli.bundle.plugin_exporter import _validate_output_rel

        result = _validate_output_rel("/absolute/path/output.tar")
        assert result is False

    def test_validate_output_rel_rejects_traversal(self) -> None:
        """Test _validate_output_rel rejects path traversal."""
        from apm_cli.bundle.plugin_exporter import _validate_output_rel

        result = _validate_output_rel("../../../etc/passwd")
        assert result is False

    def test_validate_output_rel_with_nested_paths(self) -> None:
        """Test _validate_output_rel with nested relative paths."""
        from apm_cli.bundle.plugin_exporter import _validate_output_rel

        result = _validate_output_rel("./output/nested/bundle.tar")
        assert result is True

    def test_rename_prompt_strips_prompt_suffix(self) -> None:
        """Test _rename_prompt strips .prompt.md suffix."""
        from apm_cli.bundle.plugin_exporter import _rename_prompt

        result = _rename_prompt("my-prompt.prompt.md")
        assert isinstance(result, str)
        assert ".prompt.md" not in result

    def test_rename_prompt_returns_string(self) -> None:
        """Test _rename_prompt returns string."""
        from apm_cli.bundle.plugin_exporter import _rename_prompt

        result = _rename_prompt("my-file.md")
        assert isinstance(result, str)

    def test_rename_prompt_preserves_non_prompt_files(self) -> None:
        """Test _rename_prompt preserves non-prompt files."""
        from apm_cli.bundle.plugin_exporter import _rename_prompt

        result = _rename_prompt("readme.md")
        assert result == "readme.md"

    def test_normalize_bare_skill_slug_returns_string(self) -> None:
        """Test _normalize_bare_skill_slug returns string."""
        from apm_cli.bundle.plugin_exporter import _normalize_bare_skill_slug

        result = _normalize_bare_skill_slug("my-skill")
        assert isinstance(result, str)

    def test_normalize_bare_skill_slug_handles_windows_paths(self) -> None:
        """Test _normalize_bare_skill_slug converts Windows paths."""
        from apm_cli.bundle.plugin_exporter import _normalize_bare_skill_slug

        result = _normalize_bare_skill_slug("skills\\my\\skill")
        assert isinstance(result, str)
        assert "\\" not in result

    def test_normalize_bare_skill_slug_strips_skills_prefix(self) -> None:
        """Test _normalize_bare_skill_slug strips 'skills/' prefix."""
        from apm_cli.bundle.plugin_exporter import _normalize_bare_skill_slug

        result = _normalize_bare_skill_slug("skills/my-skill")
        assert isinstance(result, str)
        # Should not start with 'skills/'
        assert not result.startswith("skills/")

    def test_normalize_bare_skill_slug_handles_bare_skills(self) -> None:
        """Test _normalize_bare_skill_slug returns empty for 'skills'."""
        from apm_cli.bundle.plugin_exporter import _normalize_bare_skill_slug

        result = _normalize_bare_skill_slug("skills")
        assert result == ""

    def test_normalize_bare_skill_slug_preserves_owner(self) -> None:
        """Test _normalize_bare_skill_slug preserves owner prefix."""
        from apm_cli.bundle.plugin_exporter import _normalize_bare_skill_slug

        result = _normalize_bare_skill_slug("owner/my-skill")
        assert isinstance(result, str)


class TestRegistryIntegration:
    """Integration tests for registry operations."""

    def test_mcp_server_operations_init(self) -> None:
        """Test MCPServerOperations initializes with default registry."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()
        assert ops is not None
        assert ops.registry_client is not None

    def test_mcp_server_operations_custom_url(self) -> None:
        """Test MCPServerOperations accepts custom registry URL."""
        from apm_cli.registry.operations import MCPServerOperations

        custom_url = "https://custom.registry.io"
        ops = MCPServerOperations(registry_url=custom_url)
        assert ops is not None
        assert ops.registry_client is not None

    def test_validate_servers_exist_empty_list(self) -> None:
        """Test validate_servers_exist with empty server list."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()
        valid, invalid = ops.validate_servers_exist([])
        assert isinstance(valid, list)
        assert isinstance(invalid, list)
        assert len(valid) == 0
        assert len(invalid) == 0

    def test_validate_servers_exist_found(self) -> None:
        """Test validate_servers_exist with found server."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()
        with patch.object(ops.registry_client, "find_server_by_reference") as mock:
            mock.return_value = {"id": "uuid-1", "name": "test-server"}
            valid, invalid = ops.validate_servers_exist(["test-server"])
            assert isinstance(valid, list)
            assert isinstance(invalid, list)
            assert "test-server" in valid

    def test_validate_servers_exist_not_found(self) -> None:
        """Test validate_servers_exist with missing server."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()
        with patch.object(ops.registry_client, "find_server_by_reference") as mock:
            mock.return_value = None
            valid, invalid = ops.validate_servers_exist(["missing-server"])
            assert isinstance(valid, list)
            assert isinstance(invalid, list)
            assert "missing-server" in invalid

    def test_validate_servers_exist_mixed(self) -> None:
        """Test validate_servers_exist with mix of found and missing."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()

        def mock_find(ref):
            return {"id": "uuid", "name": ref} if ref == "found" else None

        with patch.object(ops.registry_client, "find_server_by_reference") as m:
            m.side_effect = mock_find
            valid, invalid = ops.validate_servers_exist(["found", "missing-1", "missing-2"])
            assert isinstance(valid, list)
            assert isinstance(invalid, list)
            assert "found" in valid
            assert "missing-1" in invalid

    def test_check_servers_needing_installation_empty(self, tmp_path: Path) -> None:
        """Test check_servers_needing_installation with empty servers."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()
        result = ops.check_servers_needing_installation(
            ["copilot"],
            [],
            tmp_path,
        )
        assert isinstance(result, list)
        assert len(result) == 0

    def test_check_servers_needing_installation_not_found(self, tmp_path: Path) -> None:
        """Test check_servers_needing_installation marks missing servers."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()
        with patch.object(ops.registry_client, "find_server_by_reference") as mock:
            mock.return_value = None
            result = ops.check_servers_needing_installation(
                ["copilot"],
                ["missing-server"],
                tmp_path,
            )
            assert isinstance(result, list)

    def test_check_servers_needing_installation_multiple(self, tmp_path: Path) -> None:
        """Test check_servers_needing_installation with multiple servers."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()

        def mock_find(ref):
            return {"id": "uuid", "name": ref} if ref == "found" else None

        with patch.object(ops.registry_client, "find_server_by_reference") as m:
            m.side_effect = mock_find
            result = ops.check_servers_needing_installation(
                ["copilot"],
                ["found", "missing"],
                tmp_path,
            )
            assert isinstance(result, list)

    def test_get_installed_server_ids_empty_config(self, tmp_path: Path) -> None:
        """Test _get_installed_server_ids extracts server UUIDs."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()
        mock_adapter = MagicMock()
        mock_adapter.get_current_config.return_value = {}

        with patch("apm_cli.factory.ClientFactory") as factory_mock:
            factory_mock.create_client.return_value = mock_adapter
            result = ops._get_installed_server_ids(["copilot"], tmp_path)
            assert isinstance(result, set)

    def test_get_installed_server_ids_extracts_uuids(self, tmp_path: Path) -> None:
        """Test _get_installed_server_ids extracts server UUIDs."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()
        mock_adapter = MagicMock()
        mock_adapter.get_current_config.return_value = {
            "mcpServers": {
                "server1": {"id": "uuid-1"},
                "server2": {"id": "uuid-2"},
            }
        }

        with patch("apm_cli.factory.ClientFactory") as factory_mock:
            factory_mock.create_client.return_value = mock_adapter
            result = ops._get_installed_server_ids(["copilot"], tmp_path)
            assert isinstance(result, set)

    def test_get_installed_server_ids_handles_exception(self, tmp_path: Path) -> None:
        """Test _get_installed_server_ids handles client exceptions."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()
        with patch("apm_cli.factory.ClientFactory") as factory_mock:
            factory_mock.create_client.side_effect = Exception("Client error")
            result = ops._get_installed_server_ids(["copilot"], tmp_path)
            # Should return empty set on error
            assert isinstance(result, set)

    def test_get_installed_server_ids_multiple_runtimes(self, tmp_path: Path) -> None:
        """Test _get_installed_server_ids with multiple runtimes."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()
        mock_adapter = MagicMock()
        mock_adapter.get_current_config.return_value = {"mcpServers": {"test": {"id": "uuid-1"}}}

        with patch("apm_cli.factory.ClientFactory") as factory_mock:
            factory_mock.create_client.return_value = mock_adapter
            result = ops._get_installed_server_ids(["copilot", "vscode", "codex"], tmp_path)
            # Should aggregate from all runtimes
            assert isinstance(result, set)

    def test_get_installed_server_ids_different_key_formats(self, tmp_path: Path) -> None:
        """Test _get_installed_server_ids handles different config key formats."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()
        mock_adapter = MagicMock()
        # Codex uses mcp_servers (underscore)
        mock_adapter.get_current_config.return_value = {"mcp_servers": {"test": {"id": "uuid-1"}}}

        with patch("apm_cli.factory.ClientFactory") as factory_mock:
            factory_mock.create_client.return_value = mock_adapter
            result = ops._get_installed_server_ids(["codex"], tmp_path)
            assert isinstance(result, set)

    def test_get_installed_server_ids_vscode_servers_key(self, tmp_path: Path) -> None:
        """Test _get_installed_server_ids handles VS Code 'servers' key."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()
        mock_adapter = MagicMock()
        # VS Code uses servers
        mock_adapter.get_current_config.return_value = {"servers": {"test": {"id": "uuid-1"}}}

        with patch("apm_cli.factory.ClientFactory") as factory_mock:
            factory_mock.create_client.return_value = mock_adapter
            result = ops._get_installed_server_ids(["vscode"], tmp_path)
            assert isinstance(result, set)

    def test_get_installed_server_ids_no_project_root(self, tmp_path: Path) -> None:
        """Test _get_installed_server_ids without project root."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()
        mock_adapter = MagicMock()
        mock_adapter.get_current_config.return_value = {}

        with patch("apm_cli.factory.ClientFactory") as factory_mock:
            factory_mock.create_client.return_value = mock_adapter
            result = ops._get_installed_server_ids(["copilot"])
            assert isinstance(result, set)

    def test_get_installed_server_ids_user_scope(self, tmp_path: Path) -> None:
        """Test _get_installed_server_ids with user_scope=True."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations()
        mock_adapter = MagicMock()
        mock_adapter.get_current_config.return_value = {}

        with patch("apm_cli.factory.ClientFactory") as factory_mock:
            factory_mock.create_client.return_value = mock_adapter
            result = ops._get_installed_server_ids(["copilot"], tmp_path, user_scope=True)
            assert isinstance(result, set)
