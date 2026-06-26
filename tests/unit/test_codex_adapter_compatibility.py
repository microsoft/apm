"""Comprehensive unit tests for ``apm_cli.adapters.client.codex``.

Coverage tests targeting CodexClientAdapter and every code path.
All external I/O (filesystem, registry, rich console) is mocked so these
tests are fully hermetic.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.utils.path_security import PathTraversalError

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_adapter(
    *,
    project_root: Path | None = None,
    user_scope: bool = False,
    registry_url: str | None = None,
) -> CodexClientAdapter:
    """Create a CodexClientAdapter without hitting any live services."""
    with (
        patch("apm_cli.adapters.client.codex.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.codex.RegistryIntegration"),
    ):
        return CodexClientAdapter(
            registry_url=registry_url,
            project_root=project_root,
            user_scope=user_scope,
        )


# ---------------------------------------------------------------------------
# __init__ & class attributes
# ---------------------------------------------------------------------------


class TestInit:
    """Basic construction tests."""

    def test_default_attributes(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        assert adapter.target_name == "codex"
        assert adapter.mcp_servers_key == "mcp_servers"
        assert adapter.supports_user_scope is True

    def test_registry_client_created(self, tmp_path: Path) -> None:
        with (
            patch("apm_cli.adapters.client.codex.SimpleRegistryClient") as mock_rc,
            patch("apm_cli.adapters.client.codex.RegistryIntegration"),
        ):
            CodexClientAdapter(project_root=tmp_path)
        mock_rc.assert_called_once()

    def test_registry_integration_created(self, tmp_path: Path) -> None:
        with (
            patch("apm_cli.adapters.client.codex.SimpleRegistryClient"),
            patch("apm_cli.adapters.client.codex.RegistryIntegration") as mock_ri,
        ):
            CodexClientAdapter(project_root=tmp_path)
        mock_ri.assert_called_once()

    def test_user_scope_stored(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path, user_scope=True)
        assert adapter.user_scope is True


# ---------------------------------------------------------------------------
# _get_codex_dir & get_config_path
# ---------------------------------------------------------------------------


class TestGetConfigPath:
    """Tests for scope-aware config path resolution."""

    def test_project_scope_uses_project_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ignored-codex-home"))
        adapter = _make_adapter(project_root=tmp_path, user_scope=False)
        expected = str(tmp_path / ".codex" / "config.toml")
        assert adapter.get_config_path() == expected

    def test_user_scope_uses_home_dir(self) -> None:
        adapter = _make_adapter(user_scope=True)
        home = Path.home()
        expected = str(home / ".codex" / "config.toml")
        assert adapter.get_config_path() == expected

    def test_user_scope_honors_codex_home_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        custom_codex_home = tmp_path / "custom-codex"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CODEX_HOME", str(custom_codex_home))

        adapter = _make_adapter(user_scope=True)

        assert adapter._get_codex_dir() == custom_codex_home
        assert adapter.get_config_path() == str(custom_codex_home / "config.toml")

        monkeypatch.setenv("CODEX_HOME", "     ")
        assert adapter._get_codex_dir() == home / ".codex"
        assert adapter.get_config_path() == str(home / ".codex" / "config.toml")

        monkeypatch.delenv("CODEX_HOME", raising=False)
        assert adapter._get_codex_dir() == home / ".codex"
        assert adapter.get_config_path() == str(home / ".codex" / "config.toml")

    def test_user_scope_expands_codex_home_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        # ``Path.expanduser`` resolves ``~`` from USERPROFILE (or
        # HOMEDRIVE+HOMEPATH) on Windows rather than HOME, so set every var the
        # platform may consult to keep the expansion deterministic everywhere.
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("HOMEDRIVE", home.drive)
        monkeypatch.setenv("HOMEPATH", str(home)[len(home.drive) :])
        monkeypatch.setenv("CODEX_HOME", "~/codex-config")

        adapter = _make_adapter(user_scope=True)

        expected = home / "codex-config" / "config.toml"
        assert adapter.get_config_path() == str(expected)

    def test_user_scope_rejects_relative_codex_home_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEX_HOME", "relative-codex")

        adapter = _make_adapter(user_scope=True)

        with pytest.raises(PathTraversalError, match=r"CODEX_HOME.*non-absolute path"):
            adapter.get_config_path()

    def test_config_path_ends_with_config_toml(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        assert adapter.get_config_path().endswith("config.toml")


# ---------------------------------------------------------------------------
# get_current_config
# ---------------------------------------------------------------------------


class TestGetCurrentConfig:
    """Tests for reading the TOML config file."""

    def test_returns_empty_dict_when_file_missing(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        # config.toml doesn't exist
        result = adapter.get_current_config()
        assert result == {}

    def test_returns_parsed_toml_when_file_exists(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        config_file = codex_dir / "config.toml"
        config_file.write_text(
            '[mcp_servers.github]\ncommand = "npx"\nargs = ["-y", "gh-mcp"]\n',
            encoding="utf-8",
        )
        adapter = _make_adapter(project_root=tmp_path)
        result = adapter.get_current_config()
        assert result is not None
        assert "mcp_servers" in result

    def test_returns_none_on_toml_decode_error(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        config_file = codex_dir / "config.toml"
        config_file.write_text("this is ::: not valid toml !!!", encoding="utf-8")
        adapter = _make_adapter(project_root=tmp_path)
        with patch("apm_cli.adapters.client.codex._rich_warning"):
            result = adapter.get_current_config()
        assert result is None

    def test_returns_none_on_os_error(self, tmp_path: Path) -> None:
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("", encoding="utf-8")
        adapter = _make_adapter(project_root=tmp_path)
        with patch("builtins.open", side_effect=OSError("permission denied")):
            with patch("os.path.exists", return_value=True):
                result = adapter.get_current_config()
        assert result is None


# ---------------------------------------------------------------------------
# update_config
# ---------------------------------------------------------------------------


class TestUpdateConfig:
    """Tests for writing the TOML config file."""

    def test_update_creates_directory_and_writes(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        with patch.object(adapter, "get_current_config", return_value={}):
            result = adapter.update_config({"my-server": {"command": "npx", "args": []}})
        assert result is True
        config_path = tmp_path / ".codex" / "config.toml"
        assert config_path.exists()
        content = config_path.read_text(encoding="utf-8")
        assert "my-server" in content

    def test_update_returns_false_when_current_config_none(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        with patch.object(adapter, "get_current_config", return_value=None):
            result = adapter.update_config({"key": {}})
        assert result is False

    def test_update_creates_mcp_servers_section_if_missing(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        with patch.object(adapter, "get_current_config", return_value={"other": "value"}):
            result = adapter.update_config({"srv": {"command": "docker"}})
        assert result is True

    def test_update_merges_into_existing_mcp_servers(self, tmp_path: Path) -> None:
        existing = {"mcp_servers": {"old-server": {"command": "old"}}}
        adapter = _make_adapter(project_root=tmp_path)
        with patch.object(adapter, "get_current_config", return_value=existing):
            result = adapter.update_config({"new-server": {"command": "new"}})
        assert result is True
        config_path = tmp_path / ".codex" / "config.toml"
        content = config_path.read_text(encoding="utf-8")
        assert "old-server" in content
        assert "new-server" in content


# ---------------------------------------------------------------------------
# configure_mcp_server
# ---------------------------------------------------------------------------


class TestConfigureMcpServer:
    """Tests for the high-level server configuration entry point."""

    def test_returns_false_for_empty_server_url(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        result = adapter.configure_mcp_server("")
        assert result is False

    def test_returns_false_when_server_not_found(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        with patch.object(adapter, "_fetch_server_info", return_value=None):
            result = adapter.configure_mcp_server("unknown/server")
        assert result is False

    def test_returns_false_for_remote_only_server(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        # SSE transport is rejected by Codex (streamable-http only).
        server_info = {
            "remotes": [{"url": "https://example.com/sse", "transport_type": "sse"}],
            "packages": [],
        }
        with patch.object(adapter, "_fetch_server_info", return_value=server_info):
            result = adapter.configure_mcp_server("owner/remote-server")
        assert result is False

    def test_uses_explicit_server_name_as_key(self, tmp_path: Path, capsys) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        npm_pkg = {
            "registry_name": "npm",
            "name": "@scope/pkg",
            "runtime_arguments": [],
            "package_arguments": [],
            "environment_variables": [],
        }
        server_info = {"packages": [npm_pkg], "remotes": []}
        with (
            patch.object(adapter, "_fetch_server_info", return_value=server_info),
            patch.object(adapter, "update_config", return_value=True) as mock_update,
        ):
            result = adapter.configure_mcp_server("owner/repo", server_name="my-custom-key")
        assert result is True
        call_key = next(iter(mock_update.call_args[0][0].keys()))
        assert call_key == "my-custom-key"

    def test_derives_key_from_url_last_segment(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        npm_pkg = {
            "registry_name": "npm",
            "name": "some-pkg",
            "runtime_arguments": [],
            "package_arguments": [],
            "environment_variables": [],
        }
        server_info = {"packages": [npm_pkg], "remotes": []}
        with (
            patch.object(adapter, "_fetch_server_info", return_value=server_info),
            patch.object(adapter, "update_config", return_value=True) as mock_update,
        ):
            adapter.configure_mcp_server("owner/my-mcp-server")
        call_key = next(iter(mock_update.call_args[0][0].keys()))
        assert call_key == "my-mcp-server"

    def test_uses_full_url_when_no_slash(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        npm_pkg = {
            "registry_name": "npm",
            "name": "plain",
            "runtime_arguments": [],
            "package_arguments": [],
            "environment_variables": [],
        }
        server_info = {"packages": [npm_pkg], "remotes": []}
        with (
            patch.object(adapter, "_fetch_server_info", return_value=server_info),
            patch.object(adapter, "update_config", return_value=True) as mock_update,
        ):
            adapter.configure_mcp_server("plain-server")
        call_key = next(iter(mock_update.call_args[0][0].keys()))
        assert call_key == "plain-server"

    def test_returns_false_when_update_config_fails(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        npm_pkg = {
            "registry_name": "npm",
            "name": "@s/p",
            "runtime_arguments": [],
            "package_arguments": [],
            "environment_variables": [],
        }
        server_info = {"packages": [npm_pkg], "remotes": []}
        with (
            patch.object(adapter, "_fetch_server_info", return_value=server_info),
            patch.object(adapter, "update_config", return_value=False),
        ):
            result = adapter.configure_mcp_server("owner/pkg")
        assert result is False

    def test_returns_false_on_exception(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        with patch.object(adapter, "_fetch_server_info", side_effect=RuntimeError("boom")):
            result = adapter.configure_mcp_server("owner/pkg")
        assert result is False

    def test_uses_server_info_cache_when_provided(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        npm_pkg = {
            "registry_name": "npm",
            "name": "pkg",
            "runtime_arguments": [],
            "package_arguments": [],
            "environment_variables": [],
        }
        server_info = {"packages": [npm_pkg], "remotes": []}
        cache = {"owner/pkg": server_info}
        with patch.object(adapter, "update_config", return_value=True):
            result = adapter.configure_mcp_server("owner/pkg", server_info_cache=cache)
        assert result is True


# ---------------------------------------------------------------------------
# _format_server_config
# ---------------------------------------------------------------------------


class TestFormatServerConfig:
    """Tests for the server config formatter."""

    def test_uses_raw_stdio_when_present(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        server_info = {
            "_raw_stdio": {"command": "my-cmd", "args": ["--flag"], "env": {}},
            "name": "test",
        }
        cfg = adapter._format_server_config(server_info)
        assert cfg["command"] == "my-cmd"
        assert cfg["args"] == ["--flag"]

    def test_raw_stdio_normalizes_workspace_placeholder(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path, user_scope=False)
        server_info = {
            "_raw_stdio": {"command": "cmd", "args": ["${workspaceFolder}"], "env": {}},
            "name": "test",
        }
        cfg = adapter._format_server_config(server_info)
        assert cfg["args"] == ["."]

    def test_raises_when_no_packages(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        server_info = {"packages": [], "name": "empty-server"}
        with pytest.raises(ValueError, match="no package information"):
            adapter._format_server_config(server_info)

    def test_npm_package_basic(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        pkg = {
            "registry_name": "npm",
            "name": "@scope/mcp-tool",
            "runtime_hint": "",
            "runtime_arguments": [],
            "package_arguments": [],
            "environment_variables": [],
        }
        cfg = adapter._format_server_config({"packages": [pkg], "id": "uuid-1"})
        assert cfg["command"] == "npx"
        assert "-y" in cfg["args"]
        assert "@scope/mcp-tool" in cfg["args"]

    def test_npm_package_uses_runtime_hint(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        pkg = {
            "registry_name": "npm",
            "name": "mcp-tool",
            "runtime_hint": "npm",
            "runtime_arguments": [],
            "package_arguments": [],
            "environment_variables": [],
        }
        cfg = adapter._format_server_config({"packages": [pkg]})
        assert cfg["command"] == "npm"

    def test_npm_package_with_all_args_already_including_pkg(self, tmp_path: Path) -> None:
        """When runtime_arguments already mention the package, use them as-is."""
        adapter = _make_adapter(project_root=tmp_path)
        pkg = {
            "registry_name": "npm",
            "name": "my-mcp",
            "runtime_hint": "npx",
            "runtime_arguments": ["-y", "my-mcp"],
            "package_arguments": [],
            "environment_variables": [],
        }
        cfg = adapter._format_server_config({"packages": [pkg]})
        # Should not duplicate -y my-mcp
        assert cfg["args"].count("my-mcp") == 1

    def test_docker_package_command(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        pkg = {
            "registry_name": "docker",
            "name": "ghcr.io/owner/img",
            "runtime_hint": "docker",
            "runtime_arguments": [
                {"type": "positional", "value": "run"},
                {"type": "named", "value": "-i"},
            ],
            "package_arguments": [{"type": "positional", "value": "ghcr.io/owner/img"}],
            "environment_variables": [],
        }
        cfg = adapter._format_server_config({"packages": [pkg]})
        assert cfg["command"] == "docker"

    def test_pypi_package_uses_uvx_by_default(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        pkg = {
            "registry_name": "pypi",
            "name": "mcp-server",
            "runtime_hint": "",
            "runtime_arguments": [],
            "package_arguments": [],
            "environment_variables": [],
        }
        cfg = adapter._format_server_config({"packages": [pkg]})
        assert cfg["command"] == "uvx"

    def test_pypi_package_uses_runtime_hint(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        pkg = {
            "registry_name": "pypi",
            "name": "mcp-server",
            "runtime_hint": "pipx",
            "runtime_arguments": [],
            "package_arguments": [],
            "environment_variables": [],
        }
        cfg = adapter._format_server_config({"packages": [pkg]})
        assert cfg["command"] == "pipx"

    def test_env_vars_added_to_config(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        pkg = {
            "registry_name": "npm",
            "name": "mcp-tool",
            "runtime_hint": "npx",
            "runtime_arguments": [],
            "package_arguments": [],
            "environment_variables": [
                {"name": "MY_TOKEN", "required": False, "value": "default-val"}
            ],
        }
        with patch.dict(os.environ, {"MY_TOKEN": "env-value"}):
            cfg = adapter._format_server_config({"packages": [pkg]})
        assert cfg["env"].get("MY_TOKEN") == "env-value"

    def test_id_added_from_server_info(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        pkg = {
            "registry_name": "npm",
            "name": "tool",
            "runtime_hint": "",
            "runtime_arguments": [],
            "package_arguments": [],
            "environment_variables": [],
        }
        cfg = adapter._format_server_config({"packages": [pkg], "id": "my-uuid-123"})
        assert cfg["id"] == "my-uuid-123"


# ---------------------------------------------------------------------------
# _process_arguments
# ---------------------------------------------------------------------------


class TestProcessArguments:
    """Tests for argument processing helper."""

    def test_empty_list_returns_empty(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        assert adapter._process_arguments([]) == []

    def test_string_arg_passes_through(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        result = adapter._process_arguments(["--flag", "value"])
        assert result == ["--flag", "value"]

    def test_positional_dict_arg_extracted(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        arg = {"type": "positional", "value": "run"}
        result = adapter._process_arguments([arg])
        assert result == ["run"]

    def test_positional_uses_default_when_value_missing(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        arg = {"type": "positional", "default": "fallback"}
        result = adapter._process_arguments([arg])
        assert result == ["fallback"]

    def test_positional_skipped_when_no_value_or_default(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        arg = {"type": "positional"}
        result = adapter._process_arguments([arg])
        assert result == []

    def test_named_dict_arg_extracted(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        arg = {"type": "named", "value": "-i"}
        result = adapter._process_arguments([arg])
        assert "-i" in result

    def test_named_arg_with_additional_value(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        arg = {"type": "named", "value": "--port", "name": "8080"}
        result = adapter._process_arguments([arg])
        assert "--port" in result
        assert "8080" in result

    def test_env_placeholder_resolved_in_positional(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        arg = {"type": "positional", "value": "<MY_VAR>"}
        result = adapter._process_arguments([arg], resolved_env={"MY_VAR": "resolved"})
        assert result == ["resolved"]

    def test_runtime_placeholder_resolved_in_string(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        result = adapter._process_arguments(["{myvar}"], runtime_vars={"myvar": "hello"})
        assert result == ["hello"]


# ---------------------------------------------------------------------------
# _resolve_variable_placeholders
# ---------------------------------------------------------------------------


class TestResolveVariablePlaceholders:
    """Tests for the placeholder resolution helper."""

    def test_empty_string_returned_unchanged(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        assert adapter._resolve_variable_placeholders("", {}, {}) == ""

    def test_none_returned_unchanged(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        assert adapter._resolve_variable_placeholders(None, {}, {}) is None  # type: ignore[arg-type]

    def test_env_placeholder_replaced(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        result = adapter._resolve_variable_placeholders("<API_KEY>", {"API_KEY": "secret"}, {})
        assert result == "secret"

    def test_env_placeholder_kept_when_not_in_env(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        result = adapter._resolve_variable_placeholders("<MISSING_VAR>", {}, {})
        assert result == "<MISSING_VAR>"

    def test_runtime_placeholder_replaced(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        result = adapter._resolve_variable_placeholders("{my_var}", {}, {"my_var": "world"})
        assert result == "world"

    def test_runtime_placeholder_kept_when_not_in_vars(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        result = adapter._resolve_variable_placeholders("{unknown}", {}, {})
        assert result == "{unknown}"

    def test_multiple_placeholders_replaced(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        result = adapter._resolve_variable_placeholders(
            "<TOKEN>:{port}", {"TOKEN": "tok"}, {"port": "3000"}
        )
        assert result == "tok:3000"

    def test_plain_string_returned_unchanged(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        result = adapter._resolve_variable_placeholders("no-placeholders", {}, {})
        assert result == "no-placeholders"


# ---------------------------------------------------------------------------
# _resolve_env_placeholders  (legacy wrapper)
# ---------------------------------------------------------------------------


class TestResolveEnvPlaceholders:
    """Tests for the legacy backwards-compat wrapper."""

    def test_delegates_to_resolve_variable_placeholders(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        result = adapter._resolve_env_placeholders("<KEY>", {"KEY": "val"})
        assert result == "val"


# ---------------------------------------------------------------------------
# _ensure_docker_env_flags
# ---------------------------------------------------------------------------


class TestEnsureDockerEnvFlags:
    """Tests for Docker -e flag injection."""

    def test_no_op_when_env_vars_empty(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        base = ["run", "-i", "myimage"]
        result = adapter._ensure_docker_env_flags(base, {})
        assert result == base

    def test_adds_missing_env_flag_before_image(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        base = ["run", "-i", "myimage"]
        result = adapter._ensure_docker_env_flags(base, {"NEW_VAR": "value"})
        assert "-e" in result
        assert "NEW_VAR" in result
        # Image should still be last
        assert result[-1] == "myimage"

    def test_does_not_duplicate_existing_env_flag(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        base = ["run", "-e", "EXISTING_VAR", "myimage"]
        result = adapter._ensure_docker_env_flags(base, {"EXISTING_VAR": "v"})
        # Should not add a duplicate -e EXISTING_VAR
        assert result.count("EXISTING_VAR") == 1

    def test_adds_to_end_when_image_not_identifiable(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        base = ["run", "--some-flag"]
        # Last arg starts with "-", so the adapter falls to the else branch
        result = adapter._ensure_docker_env_flags(base, {"A": "1"})
        assert "A" in result

    def test_multiple_missing_vars_all_added(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        base = ["run", "myimage"]
        result = adapter._ensure_docker_env_flags(base, {"AAA": "1", "BBB": "2"})
        assert "AAA" in result
        assert "BBB" in result


# ---------------------------------------------------------------------------
# _inject_docker_env_vars
# ---------------------------------------------------------------------------


class TestInjectDockerEnvVars:
    """Tests for the Docker run env injection helper."""

    def test_no_op_when_env_vars_empty(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        args = ["run", "myimage"]
        result = adapter._inject_docker_env_vars(args, {})
        assert result == args

    def test_injects_after_run(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        args = ["run", "myimage"]
        result = adapter._inject_docker_env_vars(args, {"MY_VAR": "v"})
        run_idx = result.index("run")
        e_idx = result.index("-e")
        assert e_idx > run_idx

    def test_does_not_duplicate_existing_var(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        args = ["run", "-e", "EXISTING", "myimage"]
        result = adapter._inject_docker_env_vars(args, {"EXISTING": "val"})
        assert result.count("EXISTING") == 1


# ---------------------------------------------------------------------------
# _select_best_package
# ---------------------------------------------------------------------------


class TestSelectBestPackage:
    """Tests for package priority selection."""

    def test_selects_npm_over_docker(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        packages = [
            {"registry_name": "docker", "name": "img"},
            {"registry_name": "npm", "name": "pkg"},
        ]
        result = adapter._select_best_package(packages)
        assert result is not None
        assert result["registry_name"] == "npm"

    def test_selects_docker_when_no_npm(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        packages = [
            {"registry_name": "pypi", "name": "p"},
            {"registry_name": "docker", "name": "img"},
        ]
        result = adapter._select_best_package(packages)
        assert result is not None
        assert result["registry_name"] == "docker"

    def test_falls_back_to_first_package(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        packages = [{"registry_name": "nuget", "name": "Azure.Mcp"}]
        result = adapter._select_best_package(packages)
        assert result == packages[0]

    def test_returns_none_for_empty_list(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        assert adapter._select_best_package([]) is None

    def test_selects_pypi_over_homebrew(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        packages = [
            {"registry_name": "homebrew", "name": "brew-pkg"},
            {"registry_name": "pypi", "name": "py-pkg"},
        ]
        result = adapter._select_best_package(packages)
        assert result is not None
        assert result["registry_name"] == "pypi"


# ---------------------------------------------------------------------------
# _process_environment_variables
# ---------------------------------------------------------------------------


class TestProcessEnvironmentVariables:
    """Tests for environment variable resolution."""

    def test_returns_dict(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        with patch.dict(os.environ, {}, clear=False):
            result = adapter._process_environment_variables([])
        assert isinstance(result, dict)

    def test_override_takes_priority(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        env_vars = [{"name": "MY_VAR", "required": False, "value": "default"}]
        result = adapter._process_environment_variables(env_vars, {"MY_VAR": "override"})
        assert result.get("MY_VAR") == "override"

    def test_env_var_resolved_from_os_environ(self, tmp_path: Path) -> None:
        adapter = _make_adapter(project_root=tmp_path)
        env_vars = [{"name": "MY_TOKEN", "required": False, "value": ""}]
        with patch.dict(os.environ, {"MY_TOKEN": "from-env", "CI": "true"}):
            result = adapter._process_environment_variables(env_vars)
        assert result.get("MY_TOKEN") == "from-env"
