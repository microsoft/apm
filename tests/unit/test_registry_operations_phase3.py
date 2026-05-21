"""Comprehensive unit tests for MCPServerOperations (phase 3).

Covers:
- MCPServerOperations.__init__
- check_servers_needing_installation: empty list, all installed, some missing,
    registry returns None, exception
- _get_installed_server_ids: copilot, codex, vscode (servers/mcpServers keys),
    claude, import error, exception handling
- validate_servers_exist: valid, invalid, network error with/without custom URL
- batch_fetch_server_info: success, exception
- collect_runtime_variables: with cache, without cache, variables present/absent
- collect_environment_variables: docker args, packages env vars,
    camelCase/snake_case, existing env, no vars
- _prompt_for_environment_variables: E2E mode, CI mode, rich prompts,
    click fallback, existing env vars, token vars, ADO/copilot tokens
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.registry.operations import MCPServerOperations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ops(registry_url: str | None = None) -> MCPServerOperations:
    return MCPServerOperations(registry_url=registry_url)


# ---------------------------------------------------------------------------
# MCPServerOperations.__init__
# ---------------------------------------------------------------------------


class TestMCPServerOperationsInit:
    """Tests for MCPServerOperations.__init__."""

    def test_creates_registry_client(self) -> None:
        ops = _make_ops()
        assert ops.registry_client is not None

    def test_custom_url_passed_to_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_REGISTRY_ALLOW_HTTP", "1")
        ops = _make_ops("http://custom-registry.test")
        assert "custom-registry.test" in ops.registry_client.registry_url


# ---------------------------------------------------------------------------
# MCPServerOperations.check_servers_needing_installation
# ---------------------------------------------------------------------------


class TestCheckServersNeedingInstallation:
    """Tests for check_servers_needing_installation."""

    def _ops_with_no_installed_ids(self) -> MCPServerOperations:
        ops = _make_ops()
        ops._get_installed_server_ids = MagicMock(return_value=set())
        return ops

    def test_empty_server_list_returns_empty(self) -> None:
        ops = self._ops_with_no_installed_ids()
        result = ops.check_servers_needing_installation(["copilot"], [])
        assert result == []

    def test_all_installed_returns_empty(self) -> None:
        ops = _make_ops()
        ops._get_installed_server_ids = MagicMock(return_value={"uuid-1", "uuid-2"})
        ops.registry_client.find_server_by_reference = MagicMock(
            side_effect=[
                {"id": "uuid-1"},
                {"id": "uuid-2"},
            ]
        )
        result = ops.check_servers_needing_installation(["copilot"], ["server-a", "server-b"])
        assert result == []

    def test_some_missing_returns_those(self) -> None:
        ops = _make_ops()
        ops._get_installed_server_ids = MagicMock(return_value={"uuid-1"})
        ops.registry_client.find_server_by_reference = MagicMock(
            side_effect=[
                {"id": "uuid-1"},  # installed
                {"id": "uuid-99"},  # NOT installed
            ]
        )
        result = ops.check_servers_needing_installation(["copilot"], ["server-a", "server-b"])
        assert "server-b" in result
        assert "server-a" not in result

    def test_registry_returns_none_marks_as_needing_install(self) -> None:
        ops = self._ops_with_no_installed_ids()
        ops.registry_client.find_server_by_reference = MagicMock(return_value=None)
        result = ops.check_servers_needing_installation(["copilot"], ["server-x"])
        assert "server-x" in result

    def test_registry_exception_marks_as_needing_install(self) -> None:
        ops = self._ops_with_no_installed_ids()
        ops.registry_client.find_server_by_reference = MagicMock(side_effect=RuntimeError("boom"))
        result = ops.check_servers_needing_installation(["copilot"], ["server-x"])
        assert "server-x" in result

    def test_server_info_no_id_marks_as_needing_install(self) -> None:
        ops = self._ops_with_no_installed_ids()
        ops.registry_client.find_server_by_reference = MagicMock(return_value={"name": "noid"})
        result = ops.check_servers_needing_installation(["copilot"], ["server-x"])
        assert "server-x" in result


# ---------------------------------------------------------------------------
# MCPServerOperations._get_installed_server_ids
# ---------------------------------------------------------------------------


class TestGetInstalledServerIds:
    """Tests for _get_installed_server_ids."""

    def _mock_client_factory(self, runtime: str, config: dict) -> MagicMock:
        """Return a patched ClientFactory that produces a client with given config."""
        mock_client = MagicMock()
        mock_client.get_current_config.return_value = config

        mock_factory = MagicMock()
        mock_factory.create_client.return_value = mock_client
        return mock_factory

    def test_copilot_extracts_ids(self) -> None:
        ops = _make_ops()
        config = {
            "mcpServers": {
                "my-server": {"id": "copilot-uuid-1"},
            }
        }
        factory = self._mock_client_factory("copilot", config)
        with patch("apm_cli.factory.ClientFactory", factory):
            ids = ops._get_installed_server_ids(["copilot"])
        assert "copilot-uuid-1" in ids

    def test_codex_extracts_ids(self) -> None:
        ops = _make_ops()
        config = {
            "mcp_servers": {
                "my-server": {"id": "codex-uuid-1"},
            }
        }
        factory = self._mock_client_factory("codex", config)
        with patch("apm_cli.factory.ClientFactory", factory):
            ids = ops._get_installed_server_ids(["codex"])
        assert "codex-uuid-1" in ids

    def test_vscode_servers_key(self) -> None:
        ops = _make_ops()
        config = {
            "servers": {
                "my-server": {"id": "vscode-uuid-1"},
            }
        }
        factory = self._mock_client_factory("vscode", config)
        with patch("apm_cli.factory.ClientFactory", factory):
            ids = ops._get_installed_server_ids(["vscode"])
        assert "vscode-uuid-1" in ids

    def test_vscode_mcp_servers_key(self) -> None:
        ops = _make_ops()
        config = {
            "mcpServers": {
                "my-server": {"serverId": "vscode-uuid-2"},
            }
        }
        factory = self._mock_client_factory("vscode", config)
        with patch("apm_cli.factory.ClientFactory", factory):
            ids = ops._get_installed_server_ids(["vscode"])
        assert "vscode-uuid-2" in ids

    def test_claude_extracts_ids(self) -> None:
        ops = _make_ops()
        config = {
            "mcpServers": {
                "my-server": {"id": "claude-uuid-1"},
            }
        }
        factory = self._mock_client_factory("claude", config)
        with patch("apm_cli.factory.ClientFactory", factory):
            ids = ops._get_installed_server_ids(["claude"])
        assert "claude-uuid-1" in ids

    def test_import_error_returns_empty(self) -> None:
        ops = _make_ops()
        with patch.dict("sys.modules", {"apm_cli.factory": None}):
            ids = ops._get_installed_server_ids(["copilot"])
        assert ids == set()

    def test_client_exception_skips_runtime(self) -> None:
        ops = _make_ops()
        mock_factory = MagicMock()
        mock_factory.create_client.side_effect = RuntimeError("client error")
        with patch("apm_cli.factory.ClientFactory", mock_factory):
            # Should not raise, just return empty
            ids = ops._get_installed_server_ids(["copilot"])
        assert isinstance(ids, set)

    def test_config_is_not_dict_skipped(self) -> None:
        ops = _make_ops()
        mock_client = MagicMock()
        mock_client.get_current_config.return_value = None  # not a dict

        mock_factory = MagicMock()
        mock_factory.create_client.return_value = mock_client
        with patch("apm_cli.factory.ClientFactory", mock_factory):
            ids = ops._get_installed_server_ids(["copilot"])
        assert ids == set()

    def test_server_without_id_not_included(self) -> None:
        ops = _make_ops()
        config = {
            "mcpServers": {
                "no-id-server": {"name": "no id here"},
            }
        }
        factory = self._mock_client_factory("copilot", config)
        with patch("apm_cli.factory.ClientFactory", factory):
            ids = ops._get_installed_server_ids(["copilot"])
        assert ids == set()


# ---------------------------------------------------------------------------
# MCPServerOperations.validate_servers_exist
# ---------------------------------------------------------------------------


class TestValidateServersExist:
    """Tests for validate_servers_exist."""

    def test_all_valid(self) -> None:
        ops = _make_ops()
        ops.registry_client.find_server_by_reference = MagicMock(return_value={"id": "uuid"})
        valid, invalid = ops.validate_servers_exist(["server-a"])
        assert "server-a" in valid
        assert invalid == []

    def test_not_found_returned_as_invalid(self) -> None:
        ops = _make_ops()
        ops.registry_client.find_server_by_reference = MagicMock(return_value=None)
        valid, invalid = ops.validate_servers_exist(["server-a"])
        assert "server-a" in invalid
        assert valid == []

    def test_network_error_without_custom_url_assumes_valid(self) -> None:
        ops = _make_ops()
        ops.registry_client._is_custom_url = False
        ops.registry_client.find_server_by_reference = MagicMock(
            side_effect=requests.RequestException("timeout")
        )
        valid, invalid = ops.validate_servers_exist(["server-a"])
        assert "server-a" in valid
        assert invalid == []

    def test_network_error_with_custom_url_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_REGISTRY_ALLOW_HTTP", "1")
        ops = _make_ops("http://custom.test")
        ops.registry_client._is_custom_url = True
        ops.registry_client.find_server_by_reference = MagicMock(
            side_effect=requests.RequestException("timeout")
        )
        with pytest.raises(RuntimeError, match="MCP_REGISTRY_URL"):
            ops.validate_servers_exist(["server-a"])

    def test_empty_list_returns_empty(self) -> None:
        ops = _make_ops()
        valid, invalid = ops.validate_servers_exist([])
        assert valid == []
        assert invalid == []


# ---------------------------------------------------------------------------
# MCPServerOperations.batch_fetch_server_info
# ---------------------------------------------------------------------------


class TestBatchFetchServerInfo:
    """Tests for batch_fetch_server_info."""

    def test_returns_server_info_for_all_refs(self) -> None:
        ops = _make_ops()
        ops.registry_client.find_server_by_reference = MagicMock(return_value={"id": "x"})
        result = ops.batch_fetch_server_info(["a", "b"])
        assert result["a"] == {"id": "x"}
        assert result["b"] == {"id": "x"}

    def test_returns_none_on_exception(self) -> None:
        ops = _make_ops()
        ops.registry_client.find_server_by_reference = MagicMock(side_effect=RuntimeError("fail"))
        result = ops.batch_fetch_server_info(["a"])
        assert result["a"] is None

    def test_returns_empty_for_empty_list(self) -> None:
        ops = _make_ops()
        result = ops.batch_fetch_server_info([])
        assert result == {}


# ---------------------------------------------------------------------------
# MCPServerOperations.collect_runtime_variables
# ---------------------------------------------------------------------------


class TestCollectRuntimeVariables:
    """Tests for collect_runtime_variables."""

    def test_returns_empty_when_no_packages(self) -> None:
        ops = _make_ops()
        cache = {"server-a": {"packages": []}}
        with patch.object(ops, "_prompt_for_environment_variables") as mock_prompt:
            result = ops.collect_runtime_variables(["server-a"], server_info_cache=cache)
        mock_prompt.assert_not_called()
        assert result == {}

    def test_collects_variables_from_runtime_arguments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APM_E2E_TESTS", "1")
        ops = _make_ops()
        cache = {
            "server-a": {
                "packages": [
                    {
                        "runtime_arguments": [
                            {
                                "variables": {
                                    "MY_VAR": {
                                        "description": "my var desc",
                                        "is_required": True,
                                    }
                                }
                            }
                        ]
                    }
                ]
            }
        }
        result = ops.collect_runtime_variables(["server-a"], server_info_cache=cache)
        assert "MY_VAR" in result

    def test_uses_batch_fetch_when_no_cache(self) -> None:
        ops = _make_ops()
        ops.batch_fetch_server_info = MagicMock(return_value={"server-a": None})
        result = ops.collect_runtime_variables(["server-a"])
        ops.batch_fetch_server_info.assert_called_once_with(["server-a"])
        assert result == {}

    def test_skips_exception_servers(self) -> None:
        ops = _make_ops()
        # Cache has a server whose info raises on get
        # Simulate bad data that causes exception
        bad_info = MagicMock()
        bad_info.get = MagicMock(side_effect=RuntimeError("boom"))
        cache = {"server-a": bad_info}
        result = ops.collect_runtime_variables(["server-a"], server_info_cache=cache)
        assert result == {}


# ---------------------------------------------------------------------------
# MCPServerOperations.collect_environment_variables
# ---------------------------------------------------------------------------


class TestCollectEnvironmentVariables:
    """Tests for collect_environment_variables."""

    def test_returns_empty_when_no_packages(self) -> None:
        ops = _make_ops()
        cache = {"server-a": {"packages": []}}
        result = ops.collect_environment_variables(["server-a"], server_info_cache=cache)
        assert result == {}

    def test_extracts_docker_args_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_E2E_TESTS", "1")
        ops = _make_ops()
        cache = {
            "server-a": {
                "name": "test-server",
                "docker": {
                    "args": ["${MY_DOCKER_VAR}"],
                },
                "packages": [],
            }
        }
        result = ops.collect_environment_variables(["server-a"], server_info_cache=cache)
        assert "MY_DOCKER_VAR" in result

    def test_extracts_camel_case_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_E2E_TESTS", "1")
        ops = _make_ops()
        cache = {
            "server-a": {
                "packages": [
                    {
                        "environmentVariables": [
                            {"name": "MY_TOKEN", "description": "a token", "required": True}
                        ]
                    }
                ]
            }
        }
        result = ops.collect_environment_variables(["server-a"], server_info_cache=cache)
        assert "MY_TOKEN" in result

    def test_extracts_snake_case_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_E2E_TESTS", "1")
        ops = _make_ops()
        cache = {
            "server-a": {
                "packages": [
                    {
                        "environment_variables": [
                            {"name": "MY_SECRET", "description": "a secret", "required": True}
                        ]
                    }
                ]
            }
        }
        result = ops.collect_environment_variables(["server-a"], server_info_cache=cache)
        assert "MY_SECRET" in result

    def test_no_duplicate_vars_across_servers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_E2E_TESTS", "1")
        ops = _make_ops()
        pkg = {"environmentVariables": [{"name": "SHARED_VAR", "required": True}]}
        cache = {
            "server-a": {"packages": [pkg]},
            "server-b": {"packages": [pkg]},
        }
        result = ops.collect_environment_variables(
            ["server-a", "server-b"], server_info_cache=cache
        )
        # SHARED_VAR should appear exactly once (dict)
        assert "SHARED_VAR" in result

    def test_uses_batch_fetch_when_no_cache(self) -> None:
        ops = _make_ops()
        ops.batch_fetch_server_info = MagicMock(return_value={"server-a": None})
        result = ops.collect_environment_variables(["server-a"])
        ops.batch_fetch_server_info.assert_called_once_with(["server-a"])
        assert result == {}

    def test_returns_empty_for_none_server_info(self) -> None:
        ops = _make_ops()
        cache = {"server-a": None}
        result = ops.collect_environment_variables(["server-a"], server_info_cache=cache)
        assert result == {}


# ---------------------------------------------------------------------------
# MCPServerOperations._prompt_for_environment_variables
# ---------------------------------------------------------------------------


class TestPromptForEnvironmentVariables:
    """Tests for _prompt_for_environment_variables."""

    def test_e2e_mode_uses_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_E2E_TESTS", "1")
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("TRAVIS", raising=False)
        monkeypatch.delenv("JENKINS_URL", raising=False)
        monkeypatch.delenv("BUILDKITE", raising=False)
        ops = _make_ops()
        result = ops._prompt_for_environment_variables(
            {"MY_VAR": {"description": "d", "required": True}}
        )
        assert "MY_VAR" in result

    def test_ci_mode_uses_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APM_E2E_TESTS", raising=False)
        monkeypatch.setenv("CI", "true")
        ops = _make_ops()
        result = ops._prompt_for_environment_variables(
            {"MY_VAR": {"description": "d", "required": True}}
        )
        assert "MY_VAR" in result

    def test_e2e_mode_uses_existing_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_E2E_TESTS", "1")
        monkeypatch.setenv("MY_EXISTING_VAR", "from_env")
        ops = _make_ops()
        result = ops._prompt_for_environment_variables(
            {"MY_EXISTING_VAR": {"description": "d", "required": True}}
        )
        assert result["MY_EXISTING_VAR"] == "from_env"

    def test_e2e_mode_github_dynamic_toolsets_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APM_E2E_TESTS", "1")
        monkeypatch.delenv("GITHUB_DYNAMIC_TOOLSETS", raising=False)
        ops = _make_ops()
        result = ops._prompt_for_environment_variables(
            {"GITHUB_DYNAMIC_TOOLSETS": {"description": "toolsets", "required": True}}
        )
        assert result["GITHUB_DYNAMIC_TOOLSETS"] == "1"

    def test_e2e_mode_ado_token_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_E2E_TESTS", "1")
        monkeypatch.delenv("ADO_MY_TOKEN", raising=False)
        ops = _make_ops()
        mock_tm = MagicMock()
        mock_tm.get_token_for_purpose.return_value = "ado-token-value"
        with patch("apm_cli.registry.operations.GitHubTokenManager", return_value=mock_tm):
            result = ops._prompt_for_environment_variables(
                {"ADO_MY_TOKEN": {"description": "ado token", "required": True}}
            )
        assert "ADO_MY_TOKEN" in result

    def test_e2e_mode_copilot_token_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_E2E_TESTS", "1")
        monkeypatch.delenv("COPILOT_API_KEY", raising=False)
        ops = _make_ops()
        mock_tm = MagicMock()
        mock_tm.get_token_for_purpose.return_value = "copilot-token-value"
        with patch("apm_cli.registry.operations.GitHubTokenManager", return_value=mock_tm):
            result = ops._prompt_for_environment_variables(
                {"COPILOT_API_KEY": {"description": "copilot key", "required": True}}
            )
        assert "COPILOT_API_KEY" in result

    def test_e2e_mode_generic_token_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_E2E_TESTS", "1")
        monkeypatch.delenv("MY_API_KEY", raising=False)
        ops = _make_ops()
        mock_tm = MagicMock()
        mock_tm.get_token_for_purpose.return_value = "generic-token"
        with patch("apm_cli.registry.operations.GitHubTokenManager", return_value=mock_tm):
            result = ops._prompt_for_environment_variables(
                {"MY_API_KEY": {"description": "api key", "required": True}}
            )
        assert "MY_API_KEY" in result

    def test_e2e_mode_other_var_defaults_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_E2E_TESTS", "1")
        monkeypatch.delenv("RANDOM_CONFIG_VAR", raising=False)
        ops = _make_ops()
        result = ops._prompt_for_environment_variables(
            {"RANDOM_CONFIG_VAR": {"description": "misc", "required": False}}
        )
        assert "RANDOM_CONFIG_VAR" in result
        assert result["RANDOM_CONFIG_VAR"] == ""

    def test_rich_prompt_used_in_interactive_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APM_E2E_TESTS", raising=False)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("TRAVIS", raising=False)
        monkeypatch.delenv("JENKINS_URL", raising=False)
        monkeypatch.delenv("BUILDKITE", raising=False)
        monkeypatch.delenv("MY_PROMPT_VAR", raising=False)

        ops = _make_ops()

        mock_prompt = MagicMock(return_value="user-value")
        mock_console = MagicMock()

        with (
            patch("rich.console.Console", return_value=mock_console),
            patch("rich.prompt.Prompt.ask", mock_prompt),
        ):
            result = ops._prompt_for_environment_variables(
                {"MY_PROMPT_VAR": {"description": "desc", "required": True}}
            )
        assert result.get("MY_PROMPT_VAR") == "user-value"

    def test_click_fallback_when_no_rich(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APM_E2E_TESTS", raising=False)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("TRAVIS", raising=False)
        monkeypatch.delenv("JENKINS_URL", raising=False)
        monkeypatch.delenv("BUILDKITE", raising=False)
        monkeypatch.delenv("CLICK_VAR", raising=False)

        ops = _make_ops()

        with (
            patch.dict("sys.modules", {"rich": None, "rich.console": None, "rich.prompt": None}),
            patch("click.prompt", return_value="click-value"),
            patch("click.echo"),
        ):
            result = ops._prompt_for_environment_variables(
                {"CLICK_VAR": {"description": "desc", "required": True}}
            )
        assert result.get("CLICK_VAR") == "click-value"

    def test_rich_uses_existing_env_var_without_prompting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("APM_E2E_TESTS", raising=False)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("TRAVIS", raising=False)
        monkeypatch.delenv("JENKINS_URL", raising=False)
        monkeypatch.delenv("BUILDKITE", raising=False)
        monkeypatch.setenv("ALREADY_SET_VAR", "pre-existing")

        ops = _make_ops()

        mock_ask = MagicMock()
        mock_console = MagicMock()

        with (
            patch("rich.console.Console", return_value=mock_console),
            patch("rich.prompt.Prompt.ask", mock_ask),
        ):
            result = ops._prompt_for_environment_variables(
                {"ALREADY_SET_VAR": {"description": "d", "required": True}}
            )

        mock_ask.assert_not_called()
        assert result["ALREADY_SET_VAR"] == "pre-existing"

    def test_github_actions_env_triggers_ci_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APM_E2E_TESTS", raising=False)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        ops = _make_ops()
        result = ops._prompt_for_environment_variables(
            {"SOME_VAR": {"description": "d", "required": False}}
        )
        assert "SOME_VAR" in result

    def test_buildkite_env_triggers_ci_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APM_E2E_TESTS", raising=False)
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("TRAVIS", raising=False)
        monkeypatch.delenv("JENKINS_URL", raising=False)
        monkeypatch.setenv("BUILDKITE", "true")
        ops = _make_ops()
        result = ops._prompt_for_environment_variables(
            {"ANOTHER_VAR": {"description": "d", "required": False}}
        )
        assert "ANOTHER_VAR" in result
