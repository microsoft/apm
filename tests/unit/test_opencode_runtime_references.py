"""OpenCode-native references preserve literals without persisting secrets."""

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.adapters.client.opencode import OpenCodeClientAdapter
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.dependency.mcp import MCPDependency, TrustedEnvLiteral

pytestmark = pytest.mark.component


@pytest.fixture
def adapter(tmp_path):
    (tmp_path / ".opencode").mkdir()
    with patch("os.getcwd", return_value=str(tmp_path)):
        yield OpenCodeClientAdapter()


@pytest.mark.parametrize("reference", ["${EXAMPLE_KEY}", "${env:EXAMPLE_KEY}", "<EXAMPLE_KEY>"])
def test_native_reference_in_environment_headers_and_args(adapter, reference, monkeypatch):
    monkeypatch.setenv("EXAMPLE_KEY", "synthetic-process-value")
    overrides = {"EXAMPLE_KEY": "synthetic-override-value"}
    local = adapter._format_server_config(
        {
            "name": "local",
            "_raw_stdio": {"command": "echo", "args": [reference], "env": {"TOKEN": reference}},
        },
        env_overrides=overrides,
    )
    remote = adapter._format_server_config(
        {
            "name": "remote",
            "remotes": [
                {
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer " + reference, "X-Static": "literal"},
                }
            ],
        },
        env_overrides=overrides,
    )
    assert local["command"] == "echo"
    assert local["args"] == ["{env:EXAMPLE_KEY}"]
    assert local["env"] == {"TOKEN": "{env:EXAMPLE_KEY}"}
    assert remote["headers"] == {"Authorization": "Bearer {env:EXAMPLE_KEY}", "X-Static": "literal"}
    assert remote["url"] == "https://example.com/mcp"
    adapter.update_config({"local": local, "remote": remote})
    written = Path(adapter.get_config_path()).read_text(encoding="utf-8")
    assert "synthetic-process-value" not in written
    assert "synthetic-override-value" not in written


def test_authored_literals_native_references_and_tracking(adapter):
    trusted = TrustedEnvLiteral("${LITERAL}")
    resolved = adapter._resolve_environment_variables(
        {
            "STATIC": "literal",
            "EMPTY": "",
            "NONE": None,
            "": "ignored",
            "INT": 7,
            "BOOL": False,
            "NATIVE": "{env:EXAMPLE_KEY}",
            "TRUSTED": trusted,
            "REFERENCE": "${EXAMPLE_KEY}",
        },
        {"STATIC": "different", "EXAMPLE_KEY": "fake"},
    )
    assert resolved == {
        "STATIC": "literal",
        "EMPTY": "",
        "INT": "7",
        "BOOL": "false",
        "NATIVE": "{env:EXAMPLE_KEY}",
        "TRUSTED": trusted,
        "REFERENCE": "{env:EXAMPLE_KEY}",
    }
    assert resolved["TRUSTED"] is trusted
    assert adapter._last_env_placeholder_keys == {"EXAMPLE_KEY"}


def test_registry_secret_stays_runtime_reference(adapter):
    assert adapter._resolve_environment_variables(
        [{"name": "EXAMPLE_KEY", "required": True}, {"name": ""}],
        {"EXAMPLE_KEY": "fake-value"},
    ) == {"EXAMPLE_KEY": "{env:EXAMPLE_KEY}"}


@pytest.mark.parametrize("authored", [{}, {"TOKEN": "${OTHER_TOKEN}"}])
def test_registry_cannot_supply_authored_overlay_metadata(adapter, authored):
    server = {
        "name": "com.example/untrusted-overlay",
        "_apm_env_overlay": {"TOKEN": "registry-supplied-literal"},
        "packages": [
            {
                "registry_name": "npm",
                "name": "example-mcp",
                "environment_variables": [{"name": "TOKEN", "required": True}],
            }
        ],
    }
    dep = MCPDependency(name=server["name"], env=authored)
    MCPIntegrator._apply_overlay({dep.name: server}, dep)
    expected = "{env:OTHER_TOKEN}" if authored else "{env:TOKEN}"
    assert adapter._format_server_config(server)["env"] == {"TOKEN": expected}
    assert server.get("_apm_env_overlay", {}) == authored


@pytest.mark.parametrize("user_scope", [False, True])
def test_scope_writes_native_reference(adapter, tmp_path, monkeypatch, user_scope):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    adapter.user_scope = user_scope
    config = adapter._format_server_config(
        {"name": "local", "_raw_stdio": {"command": "echo", "env": {"KEY": "${EXAMPLE_KEY}"}}}
    )
    adapter.update_config({"local": config})
    expected = (
        tmp_path / "home/.config/opencode/opencode.json"
        if user_scope
        else tmp_path / "opencode.json"
    )
    assert json.loads(expected.read_text())["mcp"]["local"]["environment"] == {
        "KEY": "{env:EXAMPLE_KEY}"
    }


def test_github_automatic_auth_uses_existing_source_authority(adapter, monkeypatch):
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "synthetic-github-secret")
    with patch("apm_cli.adapters.client.copilot.GitHubTokenManager") as manager:
        manager.return_value.get_token_env_var_for_purpose.return_value = None
        manager.return_value.get_token_for_purpose.return_value = "synthetic-manager-secret"
        config = adapter._format_server_config(
            {"name": "github-mcp-server", "remotes": [{"url": "https://api.github.com/v1"}]}
        )
    assert config["headers"]["Authorization"] == "Bearer {env:GITHUB_PERSONAL_ACCESS_TOKEN}"
    manager.return_value.get_token_env_var_for_purpose.assert_called_once_with("copilot")
    manager.return_value.get_token_for_purpose.assert_not_called()
    adapter.update_config({"github": config})
    written = Path(adapter.get_config_path()).read_text(encoding="utf-8")
    assert "synthetic-github-secret" not in written
    assert "synthetic-manager-secret" not in written


def test_opencode_uses_native_runtime_reference(adapter, monkeypatch):
    """OpenCode moved from install-time resolution to native references in #3051."""
    assert adapter._supports_runtime_env_substitution is True
    monkeypatch.setenv("MY_TOKEN", "literal-value")
    value = adapter._resolve_env_variable("Authorization", "Bearer ${MY_TOKEN}")
    assert value == "Bearer {env:MY_TOKEN}"
    assert "literal-value" not in value


@pytest.mark.parametrize("reference", ["${OTHER_TOKEN}", "${env:OTHER_TOKEN}", "<OTHER_TOKEN>"])
def test_registry_authored_overlays_preserve_values_and_server_scope(
    adapter, reference, monkeypatch
):
    monkeypatch.setenv("OTHER_TOKEN", "synthetic-process-secret")
    server = {
        "name": "com.example/overlay",
        "packages": [
            {
                "registry_name": "npm",
                "name": "example-mcp",
                "environment_variables": [
                    {"name": "SERVER_MODE", "required": True},
                    {"name": "GITHUB_TOOLSETS", "required": True},
                    {"name": "TOKEN", "required": True},
                    {"name": "REQUIRED_SECRET", "required": True},
                    {"name": "OPTIONAL_SECRET", "required": False},
                    {"name": "UNSET_OPTIONAL", "required": False},
                    {"name": "EMPTY", "required": True},
                    {"name": "INT", "required": True},
                    {"name": "BOOL", "required": True},
                ],
            }
        ],
    }
    cache = {server["name"]: copy.deepcopy(server)}
    authored = {
        "SERVER_MODE": "development",
        "GITHUB_TOOLSETS": "issues",
        "TOKEN": reference,
        "EXTRA": "additional",
        "EMPTY": "",
        "INT": 7,
        "BOOL": False,
    }
    dep = MCPDependency(name=server["name"], env=authored)
    MCPIntegrator._apply_overlay(cache, dep)
    assert cache[dep.name]["_apm_env_overlay"] is not dep.env
    snapshot = copy.deepcopy(cache)
    collected = {"TOKEN": "synthetic-collected-secret", "OPTIONAL_SECRET": "fake-optional"}
    config = adapter._format_server_config(cache[dep.name], env_overrides=collected | authored)
    assert config["env"] == {
        "SERVER_MODE": "development",
        "GITHUB_TOOLSETS": "issues",
        "TOKEN": "{env:OTHER_TOKEN}",
        "REQUIRED_SECRET": "{env:REQUIRED_SECRET}",
        "OPTIONAL_SECRET": "{env:OPTIONAL_SECRET}",
        "EMPTY": "",
        "INT": "7",
        "BOOL": "false",
    }
    assert "synthetic-collected-secret" not in json.dumps(config)
    assert "fake-optional" not in json.dumps(config)
    assert adapter.configure_mcp_server(
        dep.name, env_overrides=collected | authored, server_info_cache=cache
    )
    written = Path(adapter.get_config_path()).read_text()
    assert "synthetic-process-secret" not in written
    assert json.loads(written)["mcp"]["overlay"]["environment"] == config["env"]
    assert cache == snapshot
    assert adapter._last_env_placeholder_keys == {
        "OTHER_TOKEN",
        "REQUIRED_SECRET",
        "OPTIONAL_SECRET",
    }
    rendered = adapter.render_server_config(cache[dep.name])["environment"]
    assert rendered == {
        key: value for key, value in config["env"].items() if key != "OPTIONAL_SECRET"
    }
    assert cache == snapshot
    assert "EXTRA" not in config["env"]
    second = copy.deepcopy(server)
    second["name"] = "com.example/second"
    second_cache = {second["name"]: second}
    MCPIntegrator._apply_overlay(
        second_cache, MCPDependency(name=second["name"], env={"SERVER_MODE": "production"})
    )
    assert (
        adapter._format_server_config(second, env_overrides=collected | authored)["env"][
            "SERVER_MODE"
        ]
        == "production"
    )
    other = adapter._format_server_config(server, env_overrides=collected | authored)
    assert other["env"]["SERVER_MODE"] == "{env:SERVER_MODE}"
    assert other["env"]["GITHUB_TOOLSETS"] == "context"
    assert other["env"]["TOKEN"] == "{env:TOKEN}"
    assert "EXTRA" not in other["env"]


@pytest.mark.parametrize("target", ["copilot", "cursor"])
def test_registry_overlay_metadata_does_not_change_other_targets(adapter, target):
    from apm_cli.adapters.client.copilot import CopilotClientAdapter
    from apm_cli.adapters.client.cursor import CursorClientAdapter

    cls = CopilotClientAdapter if target == "copilot" else CursorClientAdapter
    control = cls(project_root=adapter.project_root)
    server = {
        "name": "com.example/control",
        "packages": [
            {
                "registry_name": "npm",
                "name": "example-mcp",
                "environment_variables": [{"name": "SERVER_MODE", "required": True}],
            }
        ],
    }
    original = control._format_server_config(copy.deepcopy(server), {"SERVER_MODE": "development"})
    cache = {server["name"]: copy.deepcopy(server)}
    MCPIntegrator._apply_overlay(
        cache, MCPDependency(name=server["name"], env={"SERVER_MODE": "development"})
    )
    actual = control._format_server_config(cache[server["name"]], {"SERVER_MODE": "development"})
    assert json.dumps(actual, sort_keys=True) == json.dumps(original, sort_keys=True)
