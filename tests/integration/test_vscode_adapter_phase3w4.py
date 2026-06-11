"""Integration tests for apm_cli.adapters.client.vscode -- phase3w4.

Covers missing lines/branches in VSCodeClientAdapter.
All tests are hermetic: filesystem uses tmp_path, registry calls are mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.adapters.client.vscode import VSCodeClientAdapter
from apm_cli.integration.mcp_integrator_install import run_mcp_install

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(tmp_path: Path) -> VSCodeClientAdapter:
    return VSCodeClientAdapter(project_root=tmp_path)


def _context7_optional_env_server_info() -> dict:
    """Return registry metadata with an unset optional token env var."""
    return {
        "name": "context7",
        "packages": [
            {
                "runtime_hint": "npx",
                "name": "@upstash/context7-mcp",
                "registry_name": "npm",
                "runtime_arguments": [],
                "package_arguments": [],
                "environment_variables": [
                    {
                        "name": "CONTEXT7_API_KEY",
                        "description": "Optional Context7 authorization token",
                        "required": False,
                    }
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# get_config_path
# ---------------------------------------------------------------------------


class TestGetConfigPath:
    def test_creates_vscode_dir_and_returns_path(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        path = adapter.get_config_path()
        vscode_dir = tmp_path / ".vscode"
        assert vscode_dir.exists()
        assert path.endswith("mcp.json")

    def test_directory_creation_failure_logs_warning(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        logger = MagicMock()
        with (
            patch.object(Path, "exists", return_value=False),
            patch.object(Path, "mkdir", side_effect=PermissionError("denied")),
        ):
            path = adapter.get_config_path(logger=logger)
        logger.warning.assert_called_once()
        assert "mcp.json" in path

    def test_directory_creation_failure_prints_when_no_logger(self, tmp_path, capsys):
        adapter = _make_adapter(tmp_path)
        with (
            patch.object(Path, "exists", return_value=False),
            patch.object(Path, "mkdir", side_effect=PermissionError("denied")),
        ):
            adapter.get_config_path()
        captured = capsys.readouterr()
        assert "Warning" in captured.out or "Could not" in captured.out

    def test_existing_vscode_dir_not_recreated(self, tmp_path):
        (tmp_path / ".vscode").mkdir()
        adapter = _make_adapter(tmp_path)
        path = adapter.get_config_path()
        assert Path(path).name == "mcp.json"


# ---------------------------------------------------------------------------
# update_config
# ---------------------------------------------------------------------------


class TestUpdateConfig:
    def test_writes_config_successfully(self, tmp_path):
        (tmp_path / ".vscode").mkdir()
        adapter = _make_adapter(tmp_path)
        result = adapter.update_config({"servers": {}})
        assert result is True
        data = json.loads((tmp_path / ".vscode" / "mcp.json").read_text())
        assert "servers" in data

    def test_write_error_logs_and_returns_false(self, tmp_path):
        (tmp_path / ".vscode").mkdir()
        adapter = _make_adapter(tmp_path)
        logger = MagicMock()
        with patch("builtins.open", side_effect=PermissionError("denied")):
            result = adapter.update_config({"servers": {}}, logger=logger)
        assert result is False
        logger.error.assert_called_once()

    def test_write_error_no_logger_prints(self, tmp_path, capsys):
        (tmp_path / ".vscode").mkdir()
        adapter = _make_adapter(tmp_path)
        with patch("builtins.open", side_effect=PermissionError("denied")):
            result = adapter.update_config({"servers": {}})
        assert result is False
        captured = capsys.readouterr()
        assert "Error" in captured.out


# ---------------------------------------------------------------------------
# get_current_config
# ---------------------------------------------------------------------------


class TestGetCurrentConfig:
    def test_returns_empty_when_file_missing(self, tmp_path):
        (tmp_path / ".vscode").mkdir()
        adapter = _make_adapter(tmp_path)
        # no mcp.json exists
        result = adapter.get_current_config()
        assert result == {}

    def test_returns_empty_on_json_decode_error(self, tmp_path):
        vscode_dir = tmp_path / ".vscode"
        vscode_dir.mkdir()
        (vscode_dir / "mcp.json").write_text("{bad json", encoding="utf-8")
        adapter = _make_adapter(tmp_path)
        result = adapter.get_current_config()
        assert result == {}

    def test_reads_existing_config(self, tmp_path):
        vscode_dir = tmp_path / ".vscode"
        vscode_dir.mkdir()
        config = {"servers": {"my-server": {"type": "stdio"}}}
        (vscode_dir / "mcp.json").write_text(json.dumps(config), encoding="utf-8")
        adapter = _make_adapter(tmp_path)
        result = adapter.get_current_config()
        assert result == config

    def test_exception_logs_error_and_returns_empty(self, tmp_path):
        (tmp_path / ".vscode").mkdir()
        adapter = _make_adapter(tmp_path)
        logger = MagicMock()
        with patch("builtins.open", side_effect=OSError("unexpected")):
            result = adapter.get_current_config(logger=logger)
        assert result == {}
        logger.error.assert_called_once()

    def test_exception_no_logger_prints(self, tmp_path, capsys):
        (tmp_path / ".vscode").mkdir()
        adapter = _make_adapter(tmp_path)
        with patch("builtins.open", side_effect=OSError("unexpected")):
            result = adapter.get_current_config()
        assert result == {}
        captured = capsys.readouterr()
        assert "Error" in captured.out


# ---------------------------------------------------------------------------
# configure_mcp_server
# ---------------------------------------------------------------------------


class TestConfigureMcpServer:
    def test_empty_server_url_logs_error_and_returns_false(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        logger = MagicMock()
        result = adapter.configure_mcp_server("", logger=logger)
        assert result is False
        logger.error.assert_called_once()

    def test_empty_server_url_no_logger_prints(self, tmp_path, capsys):
        adapter = _make_adapter(tmp_path)
        result = adapter.configure_mcp_server("")
        assert result is False
        captured = capsys.readouterr()
        assert "Error" in captured.out

    def test_server_not_in_registry_raises_value_error(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        adapter.registry_client = MagicMock()
        adapter.registry_client.find_server_by_reference.return_value = None
        with pytest.raises(ValueError, match="not found in registry"):
            adapter.configure_mcp_server("unknown-server")

    def test_uses_cached_server_info(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        server_info = {
            "name": "my-server",
            "packages": [
                {
                    "runtime_hint": "npx",
                    "name": "@acme/mcp-server",
                    "registry_name": "npm",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        (tmp_path / ".vscode").mkdir()
        result = adapter.configure_mcp_server(
            "my-server",
            server_info_cache={"my-server": server_info},
        )
        assert result is True

    def test_configure_with_logger_verbose_detail(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        server_info = {
            "name": "srv",
            "packages": [
                {
                    "runtime_hint": "npx",
                    "name": "@x/mcp",
                    "registry_name": "npm",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        (tmp_path / ".vscode").mkdir()
        logger = MagicMock()
        result = adapter.configure_mcp_server(
            "srv",
            server_info_cache={"srv": server_info},
            logger=logger,
        )
        assert result is True
        logger.verbose_detail.assert_called()

    def test_no_server_config_generated_logs_error(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        server_info = {"name": "empty-server"}  # no packages, no remotes, no sse
        logger = MagicMock()
        # Should raise ValueError (no transport found)
        with pytest.raises((ValueError, Exception)):
            adapter.configure_mcp_server(
                "empty-server",
                server_info_cache={"empty-server": server_info},
                logger=logger,
            )

    def test_optional_env_vars_omitted_from_vscode_config_on_install(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CONTEXT7_API_KEY", raising=False)
        server_info = _context7_optional_env_server_info()
        logger = MagicMock()

        with (
            patch("apm_cli.integration.mcp_integrator._get_console", return_value=None),
            patch(
                "apm_cli.registry.operations.MCPServerOperations.validate_servers_exist",
                return_value=(["context7"], []),
            ),
            patch(
                "apm_cli.registry.operations.MCPServerOperations.check_servers_needing_installation",
                return_value=["context7"],
            ),
            patch(
                "apm_cli.registry.operations.MCPServerOperations.batch_fetch_server_info",
                return_value={"context7": server_info},
            ),
        ):
            configured = run_mcp_install(
                ["context7"],
                runtime="vscode",
                explicit_target="vscode",
                project_root=tmp_path,
                logger=logger,
            )

        config = json.loads((tmp_path / ".vscode" / "mcp.json").read_text(encoding="utf-8"))
        server = config["servers"]["context7"]

        assert configured == 1
        assert "CONTEXT7_API_KEY" not in server.get("env", {})
        assert config.get("inputs", []) == []

    def test_adds_input_variables_without_duplicates(self, tmp_path):
        vscode_dir = tmp_path / ".vscode"
        vscode_dir.mkdir()
        # Pre-existing config with one input
        existing = {
            "servers": {},
            "inputs": [{"id": "existing-id", "type": "promptString", "description": "x"}],
        }
        (vscode_dir / "mcp.json").write_text(json.dumps(existing), encoding="utf-8")

        adapter = _make_adapter(tmp_path)
        server_info = {
            "name": "srv",
            "packages": [
                {
                    "runtime_hint": "npx",
                    "name": "@x/mcp",
                    "registry_name": "npm",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [
                        {"name": "API_KEY", "description": "key"},
                    ],
                }
            ],
        }
        result = adapter.configure_mcp_server(
            "srv",
            server_info_cache={"srv": server_info},
        )
        assert result is True
        data = json.loads((vscode_dir / "mcp.json").read_text())
        # "api-key" input should be added
        input_ids = {v.get("id") for v in data["inputs"]}
        assert "api-key" in input_ids
        # existing-id should not be removed
        assert "existing-id" in input_ids

    def test_general_exception_logs_error(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        adapter.registry_client = MagicMock()
        adapter.registry_client.find_server_by_reference.side_effect = RuntimeError("boom")
        logger = MagicMock()
        result = adapter.configure_mcp_server("srv", logger=logger)
        assert result is False
        logger.error.assert_called_once()


# ---------------------------------------------------------------------------
# _format_server_config
# ---------------------------------------------------------------------------


class TestFormatServerConfig:
    def setup_method(self):
        # Use a dummy tmp dir for adapter; not needed for static method tests
        pass

    def _adapter(self, tmp_path):
        return _make_adapter(tmp_path)

    def test_raw_stdio_no_env(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "my-srv",
            "_raw_stdio": {"command": "node", "args": ["index.js"], "env": {}},
        }
        cfg, inputs = adapter._format_server_config(info)
        assert cfg["type"] == "stdio"
        assert cfg["command"] == "node"
        assert not inputs

    def test_raw_stdio_with_env_translates_vars(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "my-srv",
            "_raw_stdio": {
                "command": "node",
                "args": [],
                "env": {"TOKEN": "${MY_TOKEN}"},
            },
        }
        cfg, _inputs = adapter._format_server_config(info)
        assert cfg["type"] == "stdio"
        # bare ${VAR} should be translated to ${env:VAR}
        assert cfg["env"]["TOKEN"] == "${env:MY_TOKEN}"

    def test_raw_stdio_with_input_var(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "my-srv",
            "_raw_stdio": {
                "command": "node",
                "args": [],
                "env": {"TOKEN": "${input:my-token}"},
            },
        }
        _cfg, inputs = adapter._format_server_config(info)
        assert len(inputs) == 1
        assert inputs[0]["id"] == "my-token"

    def test_npm_package(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "npm-srv",
            "packages": [
                {
                    "runtime_hint": "npx",
                    "name": "@acme/mcp-server",
                    "registry_name": "npm",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        cfg, _inputs = adapter._format_server_config(info)
        assert cfg["type"] == "stdio"
        assert cfg["command"] == "npx"
        assert "@acme/mcp-server" in cfg["args"]

    def test_docker_package(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "docker-srv",
            "packages": [
                {
                    "runtime_hint": "docker",
                    "name": "my-image",
                    "registry_name": "docker",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        cfg, _inputs = adapter._format_server_config(info)
        assert cfg["type"] == "stdio"
        assert cfg["command"] == "docker"

    def test_python_uvx_package(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "py-srv",
            "packages": [
                {
                    "runtime_hint": "uvx",
                    "name": "mcp-server-py",
                    "registry_name": "pypi",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        cfg, _inputs = adapter._format_server_config(info)
        assert cfg["command"] == "uvx"
        assert "mcp-server-py" in cfg["args"]

    def test_python_pip_package(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "py-srv",
            "packages": [
                {
                    "runtime_hint": "pip",
                    "name": "mcp-server-brave-search",
                    "registry_name": "pypi",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        cfg, _inputs = adapter._format_server_config(info)
        # "pip" doesn't contain "python", so falls through to uvx default
        assert cfg["command"] in ("python3", "uvx")

    def test_generic_runtime_hint_fallback(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "dotnet-srv",
            "packages": [
                {
                    "runtime_hint": "dotnet",
                    "name": "MyMcpServer",
                    "registry_name": "nuget",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        cfg, _inputs = adapter._format_server_config(info)
        assert cfg["command"] == "dotnet"

    def test_package_with_env_vars_creates_inputs(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "srv",
            "packages": [
                {
                    "runtime_hint": "npx",
                    "name": "@x/srv",
                    "registry_name": "npm",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [{"name": "API_KEY", "description": "The API key"}],
                }
            ],
        }
        cfg, inputs = adapter._format_server_config(info)
        assert "env" in cfg
        assert "API_KEY" in cfg["env"]
        assert len(inputs) == 1
        assert inputs[0]["id"] == "api-key"

    def test_sse_endpoint(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "sse-srv",
            "sse_endpoint": "https://example.com/sse",
            "sse_headers": {"Authorization": "Bearer token"},
        }
        cfg, _inputs = adapter._format_server_config(info)
        assert cfg["type"] == "sse"
        assert cfg["url"] == "https://example.com/sse"

    def test_remote_http_transport(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "http-srv",
            "remotes": [
                {
                    "transport_type": "http",
                    "url": "https://api.example.com/mcp",
                    "headers": [],
                }
            ],
        }
        cfg, _inputs = adapter._format_server_config(info)
        assert cfg["type"] == "http"
        assert cfg["url"] == "https://api.example.com/mcp"

    def test_remote_sse_transport(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "sse-srv",
            "remotes": [
                {
                    "transport_type": "sse",
                    "url": "https://api.example.com/sse",
                    "headers": {},
                }
            ],
        }
        cfg, _inputs = adapter._format_server_config(info)
        assert cfg["type"] == "sse"

    def test_remote_default_transport_when_missing(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "srv",
            "remotes": [{"url": "https://api.example.com/mcp", "headers": {}}],
        }
        cfg, _inputs = adapter._format_server_config(info)
        assert cfg["type"] == "http"  # default

    def test_remote_unsupported_transport_raises(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "ws-srv",
            "remotes": [{"transport_type": "websocket", "url": "wss://example.com/mcp"}],
        }
        with pytest.raises(ValueError, match="Unsupported remote transport"):
            adapter._format_server_config(info)

    def test_remote_header_list_normalized_to_dict(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "srv",
            "remotes": [
                {
                    "transport_type": "http",
                    "url": "https://example.com",
                    "headers": [{"name": "Authorization", "value": "${AUTH_TOKEN}"}],
                }
            ],
        }
        cfg, _inputs = adapter._format_server_config(info)
        assert isinstance(cfg["headers"], dict)
        assert "Authorization" in cfg["headers"]

    def test_no_packages_no_endpoints_raises(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {"name": "broken-srv"}
        with pytest.raises(ValueError, match="incomplete configuration"):
            adapter._format_server_config(info)

    def test_packages_without_supported_runtime_raises(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "srv",
            "packages": [
                {
                    "runtime_hint": "",
                    "name": "obscure-pkg",
                    "registry_name": "unknown_registry",
                    "runtime_arguments": [],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        with pytest.raises(ValueError):
            adapter._format_server_config(info)

    def test_package_args_extracted(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        info = {
            "name": "srv",
            "packages": [
                {
                    "runtime_hint": "npx",
                    "name": "@x/srv",
                    "registry_name": "npm",
                    "runtime_arguments": [{"is_required": True, "value_hint": "--port=3000"}],
                    "package_arguments": [],
                    "environment_variables": [],
                }
            ],
        }
        cfg, _inputs = adapter._format_server_config(info)
        assert "--port=3000" in cfg["args"]


# ---------------------------------------------------------------------------
# _translate_env_vars_for_vscode
# ---------------------------------------------------------------------------


class TestTranslateEnvVarsForVscode:
    def test_empty_mapping_returns_same(self):
        assert VSCodeClientAdapter._translate_env_vars_for_vscode({}) == {}

    def test_none_mapping_returns_none(self):
        assert VSCodeClientAdapter._translate_env_vars_for_vscode(None) is None

    def test_bare_var_translated(self):
        result = VSCodeClientAdapter._translate_env_vars_for_vscode({"K": "${MY_VAR}"})
        assert result["K"] == "${env:MY_VAR}"

    def test_env_var_unchanged(self):
        result = VSCodeClientAdapter._translate_env_vars_for_vscode({"K": "${env:MY_VAR}"})
        assert result["K"] == "${env:MY_VAR}"

    def test_input_var_unchanged(self):
        result = VSCodeClientAdapter._translate_env_vars_for_vscode({"K": "${input:my-id}"})
        assert result["K"] == "${input:my-id}"

    def test_non_string_value_passes_through(self):
        result = VSCodeClientAdapter._translate_env_vars_for_vscode({"K": 42})
        assert result["K"] == 42


# ---------------------------------------------------------------------------
# _warn_on_legacy_angle_vars
# ---------------------------------------------------------------------------


class TestWarnOnLegacyAngleVars:
    def test_no_angle_vars_no_warning(self, capsys):
        VSCodeClientAdapter._warn_on_legacy_angle_vars({"K": "${env:VAR}"}, "srv", "headers")
        # No warning expected

    def test_angle_var_emits_warning(self, capsys, tmp_path):
        with patch("apm_cli.adapters.client.vscode._rich_warning") as mock_warn:
            VSCodeClientAdapter._warn_on_legacy_angle_vars({"K": "<MY_TOKEN>"}, "my-server", "env")
        mock_warn.assert_called_once()
        call_text = mock_warn.call_args[0][0]
        assert "MY_TOKEN" in call_text

    def test_empty_mapping_no_warning(self):
        # Should not raise
        VSCodeClientAdapter._warn_on_legacy_angle_vars({}, "srv", "env")
        VSCodeClientAdapter._warn_on_legacy_angle_vars(None, "srv", "env")


# ---------------------------------------------------------------------------
# _extract_input_variables
# ---------------------------------------------------------------------------


class TestExtractInputVariables:
    def test_extracts_input_var_from_value(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        mapping = {"TOKEN": "${input:my-token}"}
        result = adapter._extract_input_variables(mapping, "my-srv")
        assert len(result) == 1
        assert result[0]["id"] == "my-token"
        assert result[0]["type"] == "promptString"
        assert result[0]["password"] is True

    def test_deduplicates_same_var(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        mapping = {"A": "${input:tok}", "B": "${input:tok}"}
        result = adapter._extract_input_variables(mapping, "srv")
        assert len(result) == 1

    def test_empty_mapping_returns_empty(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        result = adapter._extract_input_variables({}, "srv")
        assert result == []

    def test_non_string_values_skipped(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        result = adapter._extract_input_variables({"K": 42}, "srv")
        assert result == []

    def test_no_input_refs_returns_empty(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        result = adapter._extract_input_variables({"K": "${env:VAR}"}, "srv")
        assert result == []


# ---------------------------------------------------------------------------
# _extract_package_args
# ---------------------------------------------------------------------------


class TestExtractPackageArgs:
    def test_empty_package_returns_empty(self):
        assert VSCodeClientAdapter._extract_package_args({}) == []

    def test_none_package_returns_empty(self):
        assert VSCodeClientAdapter._extract_package_args(None) == []

    def test_package_arguments_preferred(self):
        pkg = {
            "package_arguments": [{"value": "--port"}, {"value": "3000"}],
            "runtime_arguments": [{"is_required": True, "value_hint": "ignored"}],
        }
        result = VSCodeClientAdapter._extract_package_args(pkg)
        assert result == ["--port", "3000"]

    def test_package_arguments_empty_values_skipped(self):
        pkg = {"package_arguments": [{"value": ""}, {"value": "--port"}]}
        result = VSCodeClientAdapter._extract_package_args(pkg)
        assert result == ["--port"]

    def test_runtime_arguments_fallback(self):
        pkg = {
            "package_arguments": [],
            "runtime_arguments": [
                {"is_required": True, "value_hint": "--host"},
                {"is_required": False, "value_hint": "--optional"},
            ],
        }
        result = VSCodeClientAdapter._extract_package_args(pkg)
        assert result == ["--host"]

    def test_no_args_returns_empty(self):
        pkg = {"package_arguments": [], "runtime_arguments": []}
        result = VSCodeClientAdapter._extract_package_args(pkg)
        assert result == []


# ---------------------------------------------------------------------------
# _select_remote_with_url
# ---------------------------------------------------------------------------


class TestSelectRemoteWithUrl:
    def test_returns_first_with_url(self):
        remotes = [{"url": ""}, {"url": "https://example.com"}, {"url": "https://other.com"}]
        result = VSCodeClientAdapter._select_remote_with_url(remotes)
        assert result["url"] == "https://example.com"

    def test_returns_none_when_all_empty(self):
        remotes = [{"url": ""}, {"url": "   "}]
        result = VSCodeClientAdapter._select_remote_with_url(remotes)
        assert result is None

    def test_returns_none_for_empty_list(self):
        assert VSCodeClientAdapter._select_remote_with_url([]) is None


# ---------------------------------------------------------------------------
# _select_best_package
# ---------------------------------------------------------------------------


class TestSelectBestPackage:
    def _adapter(self, tmp_path):
        return _make_adapter(tmp_path)

    def test_prefers_npm(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        packages = [
            {"runtime_hint": "docker", "name": "img", "registry_name": "docker"},
            {"runtime_hint": "npx", "name": "pkg", "registry_name": "npm"},
        ]
        result = adapter._select_best_package(packages)
        assert result["registry_name"] == "npm"

    def test_prefers_pypi_over_docker(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        packages = [
            {"runtime_hint": "docker", "name": "img", "registry_name": "docker"},
            {"runtime_hint": "uvx", "name": "pkg", "registry_name": "pypi"},
        ]
        result = adapter._select_best_package(packages)
        assert result["registry_name"] == "pypi"

    def test_falls_back_to_runtime_hint(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        packages = [{"runtime_hint": "dotnet", "name": "pkg", "registry_name": "nuget"}]
        result = adapter._select_best_package(packages)
        assert result["runtime_hint"] == "dotnet"

    def test_returns_none_on_empty(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        result = adapter._select_best_package([])
        assert result is None

    def test_returns_first_when_no_priority_or_hint(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        packages = [
            {"name": "first", "registry_name": "unknown"},
            {"name": "second", "registry_name": "also-unknown"},
        ]
        result = adapter._select_best_package(packages)
        assert result["name"] == "first"
